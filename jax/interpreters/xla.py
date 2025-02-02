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

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from collections import namedtuple, defaultdict
from distutils.util import strtobool
import itertools as it
import operator as op
import os

import numpy as onp
import six
from six.moves import xrange

from ..config import flags
from .. import core
from .. import ad_util
from .. import tree_util
from .. import linear_util as lu
from ..abstract_arrays import (ConcreteArray, ShapedArray, make_shaped_array,
                               array_types)
from ..core import valid_jaxtype, Literal
from ..util import partial, partialmethod, cache, safe_map, prod, unzip2
from ..lib import xla_bridge as xb
from ..lib import xla_client as xc
from . import partial_eval as pe
from . import ad

FLAGS = flags.FLAGS
flags.DEFINE_bool('jax_debug_nans',
                  strtobool(os.getenv('JAX_DEBUG_NANS', "False")),
                  'Add nan checks to every operation.')

def _map(f, *xs): return tuple(map(f, *xs))
def identity(x): return x


### handlers

xb.register_constant_handler(core.Unit, lambda c, *_: c.Tuple())

def aval_to_xla_shape(aval):
  try:
    return xla_shape_handlers[type(aval)](aval)
  except KeyError:
    raise TypeError("No xla_shape_handler for type: {}".format(type(aval)))
xla_shape_handlers = {}
xla_shape_handlers[core.AbstractUnit] = lambda _: xc.Shape.tuple_shape(())
xla_shape_handlers[ShapedArray] = lambda a: xc.Shape.array_shape(a.dtype, a.shape)
xla_shape_handlers[ConcreteArray] = lambda a: xc.Shape.array_shape(a.dtype, a.shape)

def aval_to_result_handler(aval):
  try:
    return xla_result_handlers[type(aval)](aval)
  except KeyError:
    raise TypeError("No xla_result_handler for type: {}".format(type(aval)))
xla_result_handlers = {}
xla_result_handlers[core.AbstractUnit] = lambda _: lambda _: core.unit
def array_result_handler(aval): return partial(DeviceArray, aval)
xla_result_handlers[ShapedArray] = array_result_handler
xla_result_handlers[ConcreteArray] = array_result_handler

def device_put(x, device_num=0):
  x = canonicalize_dtype(x)
  try:
    return device_put_handlers[type(x)](x, device_num)
  except KeyError:
    raise TypeError("No device_put handler for type: {}".format(type(x)))
device_put_handlers = {}
device_put_handlers[core.Unit] = \
    lambda _, n: xc.Buffer.from_pyval((), n, backend=xb.get_backend())
def _device_put_array(x, n):
  return xc.Buffer.from_pyval(x, n, backend=xb.get_backend())
for _t in array_types:
  device_put_handlers[_t] = _device_put_array

# TODO(mattjj): try to remove this canonicalize_dtype stuff
def canonicalize_dtype(x):
  try:
    return canonicalize_dtype_handlers[type(x)](x)
  except KeyError:
    raise TypeError("No canonicalize_dtype handler for type: {}".format(type(x)))
canonicalize_dtype_handlers = {}
canonicalize_dtype_handlers[core.Unit] = identity
def _canonicalize_ndarray_dtype(x):
  return onp.asarray(x, xb.canonicalize_dtype(onp.result_type(x)))
for _t in array_types:
  canonicalize_dtype_handlers[_t] = _canonicalize_ndarray_dtype

def abstractify(x):
  try:
    return pytype_aval_mappings[type(x)](x)
  except KeyError:
    raise TypeError("No abstraction handler for type: {}".format(type(x)))
pytype_aval_mappings = {}
pytype_aval_mappings[core.Unit] = lambda _: core.abstract_unit
for _t in array_types:
  pytype_aval_mappings[_t] = make_shaped_array


### op-by-op execution

def apply_primitive(prim, *args, **params):
  """Impl rule that compiles and runs a single primitive 'prim' using XLA."""
  abstract_args = map(abstractify, args)
  compiled_fun = xla_primitive_callable(prim, *abstract_args, **params)
  return compiled_fun(*args)

@cache()
def xla_primitive_callable(prim, *abstract_args, **params):
  aval_out = prim.abstract_eval(*abstract_args, **params)
  if prim.multiple_results:
    handlers = tuple(map(aval_to_result_handler, aval_out))
    handle_result = lambda xs: tuple(h(x) for h, x in zip(handlers, xs.destructure()))
  else:
    handle_result = aval_to_result_handler(aval_out)
  xla_shapes = tuple(map(aval_to_xla_shape, abstract_args))
  built_c = primitive_computation(prim, *xla_shapes, **params)
  compiled = built_c.Compile(xla_shapes, xb.get_compile_options(),
                             backend=xb.get_backend())
  return partial(_execute_compiled_primitive, prim, compiled, handle_result)

@cache()
def primitive_computation(prim, *xla_shapes, **params):
  c = xb.make_computation_builder("primitive_computation")
  platform = xb.get_backend().platform
  xla_args = map(c.ParameterWithShape, xla_shapes)
  if prim in backend_specific_translations[platform]:
    rule = backend_specific_translations[platform][prim]
    rule(c, *xla_args, **params)  # return val set as a side-effect on c
  elif prim in translations:
    rule = translations[prim]
    rule(c, *xla_args, **params)  # return val set as a side-effect on c
  elif prim in initial_style_translations:
    rule = initial_style_translations[prim]
    rule(c, AxisEnv(1, [], []), *xla_args, **params)  # side-effect on c
  else:
    raise NotImplementedError("XLA translation rule for {} not found".format(prim))
  try:
    return c.Build()
  except RuntimeError as e:
    # try for a better error message by using the abstract_eval checks
    prim.abstract_eval(*map(aval_from_xla_shape, shapes), **params)
    raise e

def _execute_compiled_primitive(prim, compiled, result_handler, *args):
  device_num, = compiled.DeviceOrdinals()
  input_bufs = [device_put(x, device_num) for x in args]
  out_buf = compiled.Execute(input_bufs)
  if FLAGS.jax_debug_nans: check_nans(prim, out_buf)
  return result_handler(out_buf)

def check_nans(prim, buf):
  if prim.multiple_results:
    shapes = buf.shape().tuple_shapes()
    _map(partial(_check_nans, prim.name), shapes, buf.destructure())
  else:
    _check_nans(prim.name, buf.shape(), buf)

def _check_nans(name, xla_shape, buf):
  if xla_shape.is_tuple():
    assert not xla_shape.tuple_shapes()
  else:
    if onp.issubdtype(xla_shape.element_type(), onp.floating):
      if onp.any(onp.isnan(buf.to_py())):
        msg = "invalid value (nan) encountered in {}"
        raise FloatingPointError(msg.format(name))


### compiling jaxprs

def compile_jaxpr(jaxpr, device_assignment, axis_env, const_vals, *abstract_args):
  if axis_env.nreps > xb.device_count():
    msg = ("compiling computation that requires {} replicas, but only {} XLA "
           "devices are available")
    raise ValueErrr(msg.format(axis_env.nreps, xb.device_count()))
  arg_shapes = tuple(map(aval_to_xla_shape, abstract_args))
  built_c = jaxpr_computation(jaxpr, axis_env, const_vals, (), *arg_shapes)
  compile_opts = xb.get_compile_options(num_replicas=axis_env.nreps,
                                        device_assignment=device_assignment)
  return built_c.Compile(arg_shapes, compile_opts, backend=xb.get_backend())

def build_jaxpr(jaxpr, axis_env, const_vals, *abstract_args):
  arg_shapes = map(aval_to_xla_shape, abstract_args)
  return jaxpr_computation(jaxpr, axis_env, const_vals, (), *arg_shapes)

def prefetch(x):
  if isinstance(x, DeviceArray):
    x.copy_to_host_async()
  return x

def jaxpr_literals(jaxpr):
  return it.chain.from_iterable(eqn_literals(eqn) for eqn in jaxpr.eqns)

def eqn_literals(eqn):
  if eqn.bound_subjaxprs:
    (subjaxpr, _, _), = eqn.bound_subjaxprs
    for literal in jaxpr_literals(subjaxpr):
      yield literal
  if eqn.primitive in initial_style_translations:
    for param in eqn.params.values():
      if type(param) in (core.Jaxpr, core.TypedJaxpr):
        subjaxpr = param if type(param) is core.Jaxpr else param.jaxpr
        for literal in jaxpr_literals(subjaxpr):
          yield literal
  for v in eqn.invars:
    if type(v) is core.Literal:
      yield v.val

def jaxpr_computation(jaxpr, axis_env, const_vals, freevar_shapes, *arg_shapes):
  c, out_nodes = _jaxpr_computation(jaxpr, axis_env, const_vals, freevar_shapes,
                                    *arg_shapes)
  return c.Build(c.Tuple(*out_nodes))

def _jaxpr_computation(jaxpr, axis_env, const_vals, freevar_shapes, *arg_shapes):
  c = xb.make_computation_builder("jaxpr_computation")
  platform = xb.get_backend().platform
  _map(prefetch, it.chain(jaxpr_literals(jaxpr), const_vals))

  def read(v):
    if type(v) is Literal:
      return c.Constant(canonicalize_dtype(v.val))
    else:
      return env[v]

  def write(v, node):
    assert node is not None
    env[v] = node

  env = {}
  write(core.unitvar, c.Tuple())
  if const_vals:
    _map(write, jaxpr.constvars, map(c.Constant, const_vals))
    _map(write, jaxpr.freevars, map(c.ParameterWithShape, freevar_shapes))
  else:
    all_freevars = it.chain(jaxpr.constvars, jaxpr.freevars)
    _map(write, all_freevars, map(c.ParameterWithShape, freevar_shapes))
  _map(write, jaxpr.invars, map(c.ParameterWithShape, arg_shapes))
  for eqn in jaxpr.eqns:
    in_nodes = list(map(read, eqn.invars))
    if eqn.primitive in backend_specific_translations[platform]:
      rule = backend_specific_translations[platform][eqn.primitive]
      ans = rule(c, *in_nodes, **eqn.params)
    elif eqn.primitive in translations:
      ans = translations[eqn.primitive](c, *in_nodes, **eqn.params)
    elif eqn.primitive in initial_style_translations:
      rule = initial_style_translations[eqn.primitive]
      ans = rule(c, axis_env, *in_nodes, **eqn.params)
    elif eqn.primitive in parallel_translations:
      replica_groups = axis_groups(axis_env, eqn.params['axis_name'])
      new_params = {k: eqn.params[k] for k in eqn.params if k != 'axis_name'}
      rule = parallel_translations[eqn.primitive]
      ans = rule(c, *in_nodes, replica_groups=replica_groups, **new_params)
    elif eqn.primitive in call_translations:
      (subjaxpr, const_bindings, freevar_bindings), = eqn.bound_subjaxprs
      env_nodes = list(map(read, const_bindings + freevar_bindings))
      rule = call_translations[eqn.primitive]
      ans = rule(c, subjaxpr, axis_env, env_nodes, in_nodes, **eqn.params)
    else:
      msg = "XLA translation rule for primitive '{}' not found"
      raise NotImplementedError(msg.format(eqn.primitive.name))

    c.GetShape(ans)  # force xla to do shape error checking
    out_nodes = xla_destructure(c, ans) if eqn.primitive.multiple_results else [ans]
    _map(write, eqn.outvars, out_nodes)
  return c, _map(read, jaxpr.outvars)

def xla_destructure(c, ans):
  num_elements = len(c.GetShape(ans).tuple_shapes())
  return [c.GetTupleElement(ans, i) for i in range(num_elements)]


AxisEnv = namedtuple("AxisEnv", ["nreps", "names", "sizes"])

def extend_axis_env(env, name, size):
  return AxisEnv(env.nreps, env.names + [name], env.sizes + [size])

def axis_read(axis_env, axis_name):
  return max(i for i, name in enumerate(axis_env.names) if name == axis_name)

def axis_groups(axis_env, name):
  if isinstance(name, (list, tuple)):
    mesh_axes = tuple(map(partial(axis_read, axis_env), name))
  else:
    mesh_axes = (axis_read(axis_env, name),)
  return _axis_groups(axis_env.nreps, axis_env.sizes, mesh_axes)

def _axis_groups(nrep, mesh_spec, mesh_axes):
  trailing_size, ragged = divmod(nrep, prod(mesh_spec))
  assert not ragged
  full_spec = list(mesh_spec) + [trailing_size]
  iota = onp.arange(prod(full_spec)).reshape(full_spec)
  groups = onp.reshape(
      onp.moveaxis(iota, mesh_axes, onp.arange(len(mesh_axes))),
      (prod(onp.take(full_spec, mesh_axes)), -1))
  return tuple(map(tuple, groups.T))

def jaxpr_replicas(jaxpr):
  return max(it.chain([1], (eqn_replicas(eqn) for eqn in jaxpr.eqns)))

def eqn_replicas(eqn):
  if eqn.bound_subjaxprs:
    (subjaxpr, _, _), = eqn.bound_subjaxprs
    return eqn.params.get('axis_size', 1) * jaxpr_replicas(subjaxpr)
  elif eqn.primitive in initial_style_translations:
    nums = (jaxpr_replicas(param if type(param) is core.Jaxpr else param.jaxpr)
            for param in eqn.params.values()
            if type(param) in (core.Jaxpr, core.TypedJaxpr))
    return max(it.chain([1], nums))
  else:
    return 1


### xla_call underlying jit

def _xla_call_impl(fun, *args, **params):
  device_assignment = params['device_assignment']
  compiled_fun = _xla_callable(fun, device_assignment, *map(abstractify, args))
  try:
    return compiled_fun(*args)
  except FloatingPointError:
    print("Invalid value encountered in the output of a jit function. "
          "Calling the de-optimized version.")
    return fun.call_wrapped(*args)  # probably won't return

@lu.cache
def _xla_callable(fun, device_assignment, *abstract_args):
  pvals = [pe.PartialVal((aval, core.unit)) for aval in abstract_args]
  with core.new_master(pe.JaxprTrace, True) as master:
    jaxpr, (pvals, consts, env) = pe.trace_to_subjaxpr(fun, master, False).call_wrapped(pvals)
    assert not env  # no subtraces here (though cond might eventually need them)
    axis_env = AxisEnv(jaxpr_replicas(jaxpr), [], [])
    compiled = compile_jaxpr(jaxpr, device_assignment, axis_env, consts,
                             *abstract_args)
    del master, consts, jaxpr, env
  result_handlers = tuple(map(_pval_to_result_handler, pvals))
  if axis_env.nreps == 1:
    return partial(_execute_compiled, compiled, result_handlers)
  else:
    return partial(_execute_replicated, compiled, result_handlers)

def _pval_to_result_handler(pval):
  pv, const = pval
  if pv is None:
    return lambda _: const
  else:
    return aval_to_result_handler(pv)

def _execute_compiled(compiled, handlers, *args):
  device_num, = compiled.DeviceOrdinals()
  input_bufs = [device_put(x, device_num) for x in args]
  out_bufs = compiled.Execute(input_bufs).destructure()
  if FLAGS.jax_debug_nans: check_nans(xla_call_p, out_buf)
  return [handler(out_buf) for handler, out_buf in zip(handlers, out_bufs)]

def _execute_replicated(compiled, handlers, *args):
  input_bufs = [[device_put(x, i) for x in args]
                for i in compiled.DeviceOrdinals()]
  out_bufs = compiled.ExecutePerReplica(input_bufs)[0].destructure()
  if FLAGS.jax_debug_nans: check_nans(xla_call_p, out_buf)
  return [handler(out_buf) for handler, out_buf in zip(handlers, out_bufs)]


xla_call_p = core.Primitive('xla_call')
xla_call_p.multiple_results = True
xla_call = partial(core.call_bind, xla_call_p)
xla_call_p.def_custom_bind(xla_call)
xla_call_p.def_impl(_xla_call_impl)

def _xla_call_translation_rule(c, jaxpr, axis_env, env_nodes, in_nodes,
                               device_assignment):
  del device_assignment  # Ignored.
  subc = jaxpr_computation(jaxpr, axis_env, (), _map(c.GetShape, env_nodes),
                           *map(c.GetShape, in_nodes))
  return c.Call(subc, env_nodes + in_nodes)
ad.primitive_transposes[xla_call_p] = partial(ad.call_transpose, xla_call_p)


### translation tables

translations = {}
parallel_translations = {}
initial_style_translations = {}
call_translations = {}
backend_specific_translations = defaultdict(dict)

translations[core.identity_p] = lambda c, x: x
call_translations[xla_call_p] = _xla_call_translation_rule

def zeros_like_translation_rule(c, x):
  shape = c.GetShape(x)
  if shape.is_tuple():
    assert not shape.tuple_shapes()
    return c.Tuple()
  else:
    zero = c.Constant(onp.array(0, shape.element_type()))
    return c.Broadcast(zero, shape.dimensions())
translations[ad_util.zeros_like_p] = zeros_like_translation_rule

def add_jaxvals_translation_rule(c, x, y):
  shape = c.GetShape(x)
  if shape.is_tuple():
    assert not shape.tuple_shapes()
    return x
  else:
    return c.Add(x, y)
translations[ad_util.add_jaxvals_p] = add_jaxvals_translation_rule

def lower_fun(fun, instantiate=False, initial_style=False):
  """Build a translation rule for a traceable function."""
  def f(c, *args, **params):
    if initial_style:
      axis_env, xla_args = args[0], args[1:]
    else:
      axis_env, xla_args = AxisEnv(1, [], []), args
    xla_shapes = tuple(map(c.GetShape, xla_args))
    avals = map(_aval_from_xla_shape, xla_shapes)
    pvals = [pe.PartialVal((a, core.unit)) for a in avals]
    jaxpr, _, consts = pe.trace_to_jaxpr(
        lu.wrap_init(fun, params), pvals, instantiate=True)
    built_c = jaxpr_computation(jaxpr, axis_env, consts, (), *xla_shapes)
    return c.Call(built_c, xla_args)
  return f

def _aval_from_xla_shape(xla_shape):
  if xla_shape.is_tuple() and not xla_shape.tuple_shapes():
    return core.abstract_unit
  else:
    return ShapedArray(xla_shape.dimensions(), xla_shape.element_type())


### device-persistent data

class DeviceValue(object):
  """A DeviceValue represents a value backed by device memory."""
  __slots__ = ["aval", "device_buffer", "__weakref__"]

  def __init__(self, aval, device_buffer):
    self.aval = aval
    self.device_buffer = device_buffer

  def _check_if_deleted(self):
    if self.device_buffer is None:
      raise ValueError("DeviceValue has been deleted.")

  def block_until_ready(self):
    """Blocks the caller until the buffer's value has been computed on device.

    This method is mostly useful for timing microbenchmarks that wish to
    time how long a computation takes, without transferring the result back
    to the host.
    """
    self._check_if_deleted()
    self.device_buffer.block_host_until_ready()

def _forward_method(attrname, self, fun, *args):
  return fun(getattr(self, attrname), *args)
_forward_to_value = partial(_forward_method, "_value")

class DeviceArray(DeviceValue):
  """A DeviceArray is an ndarray backed by a single device memory buffer."""
  # We don't subclass ndarray because that would open up a host of issues,
  # but lax_numpy.py overrides isinstance behavior and attaches ndarray methods.
  __slots__ = ["_npy_value"]
  __array_priority__ = 100

  def __init__(self, aval, device_buffer):
    self.aval = aval
    self.device_buffer = device_buffer
    self._npy_value = None
    if not core.skip_checks:
      npy_value = self._value
      assert npy_value.dtype == aval.dtype and npy_value.shape == aval.shape

  @property
  def _value(self):
    self._check_if_deleted()
    if self._npy_value is None:
      self._npy_value = self.device_buffer.to_py()
      self._npy_value.flags.writeable = False
    return self._npy_value

  @property
  def shape(self):
    return self.aval.shape

  @property
  def dtype(self):
    return self.aval.dtype

  @property
  def size(self):
    return prod(self.aval.shape)

  @property
  def ndim(self):
    return len(self.aval.shape)

  def copy(self):
    """Returns an ndarray (backed by host memory, not device memory)."""
    return onp.asarray(self)

  def copy_to_host_async(self):
    """Requests a copy of the buffer to the host."""
    self._check_if_deleted()
    if self._npy_value is None:
      self.device_buffer.copy_to_host_async()

  def delete(self):
    """Deletes the device array and any cached copy on the host.

    It is an error to access the contents of a `DeviceArray` after it has
    been deleted.

    Use of this method is optional; device buffers will be reclaimed
    automatically by Python when a DeviceArray object is garbage collected.
    However, it is sometimes useful to have more explicit control over the
    time of deletion.
    """
    self.device_buffer.delete()
    self.device_buffer = None
    self._npy_value = None

  def __repr__(self):
    return onp.array_repr(self)

  def item(self):
    if onp.issubdtype(self.dtype, onp.complexfloating):
      return complex(self)
    elif onp.issubdtype(self.dtype, onp.floating):
      return float(self)
    elif onp.issubdtype(self.dtype, onp.integer):
      return int(self)
    elif onp.issubdtype(self.dtype, onp.bool_):
      return bool(self)
    else:
      raise TypeError(self.dtype)

  def __len__(self):
    try:
      return self.aval.shape[0]
    except IndexError:
      raise TypeError("len() of unsized object")  # same as numpy error

  def __iter__(self):
    if self.ndim == 0:
      raise TypeError("iteration over a 0-d array")  # same as numpy error
    else:
      return self._value.__iter__()

  def __reversed__(self):
    if self.ndim == 0:
      raise TypeError("iteration over a 0-d array")
    else:
      return reversed(self._value)

  def __format__(self, format_spec):
    # Simulates behavior of https://github.com/numpy/numpy/pull/9883
    if self.ndim == 0:
      return format(self._value[()], format_spec)
    else:
      return format(self._value, format_spec)

  def __array__(self, dtype=None, context=None):
    return onp.asarray(self._value, dtype=dtype)

  __str__ = partialmethod(_forward_to_value, str)
  __bool__ = __nonzero__ = partialmethod(_forward_to_value, bool)
  __float__ = partialmethod(_forward_to_value, float)
  __int__ = partialmethod(_forward_to_value, int)
  if six.PY2:
    __long__ = partialmethod(_forward_to_value, long)  # noqa: F821
  __complex__ = partialmethod(_forward_to_value, complex)
  __hex__ = partialmethod(_forward_to_value, hex)
  __oct__ = partialmethod(_forward_to_value, oct)
  __index__ = partialmethod(_forward_to_value, op.index)

  # pickle saves and loads just like an ndarray
  __reduce__ = partialmethod(_forward_to_value, op.methodcaller("__reduce__"))

  # clobbered when jax.numpy is imported, but useful in tests
  def __eq__(self, other): return self._value == other

  def __hash__(self):
    raise TypeError("JAX DeviceArray, like numpy.ndarray, is not hashable.")

core.literalable_types.add(DeviceArray)
core.pytype_aval_mappings[DeviceArray] = ConcreteArray
pytype_aval_mappings[DeviceArray] = lambda x: x.aval
canonicalize_dtype_handlers[DeviceArray] = identity

def _device_array_constant_handler(c, val, canonicalize_types=True):
  return c.Constant(onp.asarray(val), canonicalize_types=canonicalize_types)
xb.register_constant_handler(DeviceArray, _device_array_constant_handler)
def _device_put_device_array(x, device_num):
  if x.device_buffer.device() == device_num:
    return x.device_buffer
  else:
    return x.device_buffer.copy_to_device(device_num)
device_put_handlers[DeviceArray] = _device_put_device_array


def _device_put_impl(x, device_num=0):
  try:
    a = abstractify(x)
  except TypeError:
    raise TypeError("Argument '{}' of type {} is not a valid JAX type"
                    .format(x, type(x)))
  handler = aval_to_result_handler(a)
  return handler(device_put(x, device_num))

device_put_p = core.Primitive('device_put')
device_put_p.def_impl(_device_put_impl)
device_put_p.def_abstract_eval(lambda x, **kwargs: x)
translations[device_put_p] = lambda c, x, **kwargs: x
ad.deflinear(device_put_p, lambda cotangent, **kwargs: [cotangent])


### lazy constants

class DeviceConstant(DeviceArray):
  def copy_to_host_async(self): pass

  @staticmethod
  def constant_handler(c, constant_instance, canonicalize_types=True):
    assert False

def _instantiate_device_constant(const, device_num=0, cutoff=1e6):
  # dispatch an XLA Computation to build the constant on the device if it's
  # large, or alternatively build it on the host and transfer it if it's small
  assert isinstance(const, DeviceConstant)
  if const.size > cutoff and device_num == 0:
    c = xb.make_computation_builder("constant_instantiating_computation")
    xla_const = const.constant_handler(c, const)
    opts = xb.get_compile_options(device_assignment=(device_num,))
    compiled = c.Build(xla_const).Compile((), opts, backend=xb.get_backend())
    return compiled.Execute(())
  else:
    return xc.Buffer.from_pyval(onp.asarray(const), device_num)
