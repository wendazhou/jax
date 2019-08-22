# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Interface and utility functions to XLA.

This module wraps the XLA client(s) and builders to standardize their interfaces
and provide some automatic type mapping logic for converting between Numpy and
XLA. There are also a handful of related casting utilities.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import warnings
from distutils.util import strtobool

from ..config import flags
from .. import util
import numpy as onp  # 'onp' rather than 'np' to distinguish from autograd.numpy
import six
import threading

from . import xla_client
from . import xrt

FLAGS = flags.FLAGS
flags.DEFINE_bool('jax_enable_x64',
                  strtobool(os.getenv('JAX_ENABLE_X64', 'False')),
                  'Enable 64-bit types to be used.')
flags.DEFINE_string(
    'jax_xla_backend', 'xla',
    'Either "xla" for the XLA service directly, or "xrt" for an XRT backend.')
flags.DEFINE_string(
    'jax_backend_target', 'local',
    'Either "local" or "rpc:address" to connect to a remote service target.')
flags.DEFINE_string(
    'jax_platform_name',
    os.getenv('JAX_PLATFORM_NAME', ''),
    'Platform name for XLA. The default is to attempt to use a GPU if '
    'available, but fall back to CPU otherwise. To set the platform manually, '
    'pass "cpu" for CPU or "gpu" for GPU.')


def get_compile_options(num_replicas=None, device_assignment=None):
  """Returns the compile options to use, as derived from flag values.

  Args:
    num_replicas: Optional int indicating the number of replicas for which to
      compile (default inherited from xla_client.CompileOptions).
    device_assignment: Optional tuple of integers indicating the assignment of
      logical replicas to physical devices (default inherited from
      xla_client.CompileOptions). Must be consistent with `num_replicas`.
  """
  compile_options = None
  if num_replicas is not None:
    compile_options = compile_options or xla_client.CompileOptions()
    compile_options.num_replicas = num_replicas
  if device_assignment is not None:
    # NOTE(mattjj): xla_client.DeviceAssignment.create expects a 2D ndarray
    # indexed by replica number and computation per replica, respectively, while
    # here we currently assume only one computation per replica, hence the
    # second axis is always trivial.
    if num_replicas is not None and num_replicas != len(device_assignment):
      msg = "device_assignment does not match num_replicas: {} vs {}."
      raise ValueError(msg.format(device_assignment, num_replicas))
    compile_options = compile_options or xla_client.CompileOptions()
    device_assignment = onp.array(device_assignment)[:, None]
    device_assignment = xla_client.DeviceAssignment.create(device_assignment)
    assert num_replicas is None or device_assignment.replica_count() == num_replicas
    compile_options.device_assignment = device_assignment
  return compile_options

_backends = {}

def register_backend(name, factory):
  _backends[name] = factory

def _get_local_backend():
  platform = FLAGS.jax_platform_name

  # Canonicalize platform names.
  cpu = 'cpu'
  gpu = 'gpu'
  if platform == 'Host':
    platform = cpu
  elif platform == 'CUDA':
    platform = gpu
  elif platform == '':
    platform = None

  backend = xla_client.get_local_backend(platform)
  if backend is None:
    raise RuntimeError("No local XLA backends found.")

  if backend.platform == cpu and platform != cpu:
    warnings.warn('No GPU/TPU found, falling back to CPU.')

  return backend

def _get_xrt_backend():
  # TODO(phawkins): support non-TPU devices.
  tf_device_name = "TPU"
  worker = "tpu_worker"
  tf_context = xrt.get_tf_context(FLAGS.jax_backend_target, worker)
  backend = xrt.XrtBackend(tf_context, tf_device_name)
  return backend


register_backend('xla', _get_local_backend)
register_backend('xrt', _get_xrt_backend)

_backend_lock = threading.Lock()

@util.memoize
def get_backend():
  with _backend_lock:
    backend = _backends.get(FLAGS.jax_xla_backend)
    if backend is None:
      msg = 'Unknown jax_xla_backend value "{}".'
      raise ValueError(msg.format(FLAGS.jax_xla_backend))
    return backend()


def device_count():
  return int(get_backend().device_count())


### utility functions

@util.memoize
def dtype_to_etype(dtype):
  """Convert from dtype to canonical etype (reading FLAGS.jax_enable_x64)."""
  return xla_client.dtype_to_etype(canonicalize_dtype(dtype))


_dtype_to_32bit_dtype = {
    onp.dtype('int64'): onp.dtype('int32'),
    onp.dtype('uint64'): onp.dtype('uint32'),
    onp.dtype('float64'): onp.dtype('float32'),
    onp.dtype('complex128'): onp.dtype('complex64'),
}


@util.memoize
def canonicalize_dtype(dtype):
  """Convert from a dtype to a canonical dtype based on FLAGS.jax_enable_x64."""
  dtype = onp.dtype(dtype)

  if FLAGS.jax_enable_x64:
    return dtype
  else:
    return _dtype_to_32bit_dtype.get(dtype, dtype)


@util.memoize
def supported_numpy_dtypes():
  return {canonicalize_dtype(dtype)
          for dtype in xla_client.XLA_ELEMENT_TYPE_TO_DTYPE.values()}


# TODO(mattjj,frostig): try to remove this function
def normalize_to_xla_dtypes(val):
  """Normalize dtypes in a value."""
  if hasattr(val, '__array__') or onp.isscalar(val):
    return onp.asarray(val, dtype=canonicalize_dtype(onp.result_type(val)))
  elif isinstance(val, (tuple, list)):
    return tuple(normalize_to_xla_dtypes(x) for x in val)
  raise TypeError('Can\'t convert to XLA: {}'.format(val))


# TODO(mattjj,frostig): try to remove this function
def shape_of(value):
  """Given a Python or XLA value, return its canonicalized XLA Shape."""
  if hasattr(value, 'shape') and hasattr(value, 'dtype'):
    return Shape.array_shape(canonicalize_dtype(value.dtype), value.shape)
  elif onp.isscalar(value):
    return shape_of(onp.asarray(value))
  elif isinstance(value, (tuple, list)):
    return Shape.tuple_shape(tuple(shape_of(elt) for elt in value))
  else:
    raise TypeError('Unexpected type: {}'.format(type(value)))


class _JaxComputationBuilder(xla_client.ComputationBuilder):
  """Base class implementing all of JaxComputationBuilder.

  This class is intended to override and augment the interface of an XLA
  ComputationBuilder to form JaxComputationBuilder
  """

  # Method name case follows that of the XLA ComputationBuilder
  # pylint: disable=invalid-name

  def Build(self, *args, **kwargs):
    return super(_JaxComputationBuilder, self).Build(
        *args, **kwargs)

  def Parameter(self, value, name=None, parameter_num=None):
    return super(_JaxComputationBuilder, self).ParameterWithShape(
        shape_of(value), name=name, parameter_num=parameter_num)

  def NumpyArrayConstant(self, value, canonicalize_types=True):
    if canonicalize_types:
      value = normalize_to_xla_dtypes(value)
    return super(_JaxComputationBuilder, self).Constant(value)

  def ConstantLike(self, example_value, value, canonicalize_types=True):
    example_value = onp.asarray(example_value)
    return self.Constant(onp.array(value, dtype=example_value.dtype))

  def Constant(self, py_val, canonicalize_types=True):
    """Translate constant `py_val` to a constant for this ComputationBuilder.

    Args:
      py_val: a Python value to be translated to a constant.

    Returns:
      A representation of the constant, either a ComputationDataHandle or None
    """
    py_type = type(py_val)
    if py_type in _constant_handlers:
      return _constant_handlers[py_type](self, py_val, canonicalize_types)
    else:
      raise TypeError("No constant handler for type: {}".format(py_type))

  # TODO(mattjj): remove when CrossReplicaSum is added to XLA:CPU
  def CrossReplicaSum(self, operand, replica_groups):
    """Workaround for CrossReplicaSum not being implemented on some backends."""
    if len(replica_groups[0]) == 1:
      return operand
    else:
      return super(_JaxComputationBuilder, self).CrossReplicaSum(
          operand, replica_groups)

  # TODO(mattjj): remove when AllToAll is added to XLA:CPU
  def AllToAll(self, operand, split_axis, concat_axis, replica_groups):
    """Workaround for AllToAll not being implemented on some backends."""
    if len(replica_groups[0]) == 1:
      return operand
    else:
      return super(_JaxComputationBuilder, self).AllToAll(
          operand, split_axis, concat_axis, replica_groups)


def make_computation_builder(name):
  return _JaxComputationBuilder(name)


def register_constant_handler(type_, handler_fun):
  _constant_handlers[type_] = handler_fun
_constant_handlers = {}


def _ndarray_constant_handler(c, val, canonicalize_types=True):
  """Constant handler for ndarray literals, handling zero-size strides.

  This function essentially calls c.NumpyArrayConstant(val) except it has
  special handling of arrays with any strides of size zero: for those, it
  generates appropriate calls to NumpyArrayConstant, Broadcast, and Transpose
  to avoid staging in large literals that might arise from np.zeros or np.ones
  or the output of lax.broadcast (which uses onp.broadcast_to which in turn
  uses size-zero strides).

  Args:
    c: XLA client ComputationBuilder.
    val: an ndarray.

  Returns:
    An XLA ComputationDataHandle / XlaOp representing the constant ndarray
    staged into the XLA Computation.
  """
  # TODO(mattjj): revise this to use c.BroadcastInDim rather than Transpose
  if onp.any(onp.equal(0, val.strides)) and val.size > 0:
    zero_stride_axes, = onp.where(onp.equal(0, val.strides))
    other_axes, = onp.where(onp.not_equal(0, val.strides))
    collapsed_val = val[tuple(0 if ax in zero_stride_axes else slice(None)
                              for ax in range(val.ndim))]
    xla_val = c.Broadcast(
        c.NumpyArrayConstant(collapsed_val, canonicalize_types),
        onp.take(val.shape, zero_stride_axes))
    permutation = onp.argsort(tuple(zero_stride_axes) + tuple(other_axes))
    return c.Transpose(xla_val, permutation)
  else:
    return c.NumpyArrayConstant(val, canonicalize_types)
register_constant_handler(onp.ndarray, _ndarray_constant_handler)


def _scalar_constant_handler(c, val, canonicalize_types=True):
  return c.NumpyArrayConstant(val, canonicalize_types)

for scalar_type in [onp.int8, onp.int16, onp.int32, onp.int64,
                    onp.uint8, onp.uint16, onp.uint32, onp.uint64,
                    onp.float16, onp.float32, onp.float64,
                    float, int, bool, onp.bool_, onp.longlong]:
  register_constant_handler(scalar_type, _scalar_constant_handler)

if six.PY2:
  register_constant_handler(long, _scalar_constant_handler) # noqa: F821
