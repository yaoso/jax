# Copyright 2020 Google LLC
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
"""Workarounds for jax2tf transforms when XLA is not linked in."""
import builtins
import dataclasses
from functools import partial, wraps
import string
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from jax import core
from jax import lax
from jax._src.lax import slicing as lax_slicing
from jax._src import dtypes
from jax._src import util

from jax.experimental.jax2tf import jax2tf

import numpy as np
import tensorflow as tf  # type: ignore[import]


# Implementation rules for primitives when XLA is not linked in. These
# implementations are workarounds, making use of TF ops that do work when XLA is
# not linked in. They are only used when the argument `enable_xla=False` when
# calling jax2tf.convert().
tf_impl_no_xla: Dict[core.Primitive, Callable[..., Any]] = {}


TfVal = Any
DType = Any
PrecisionType = Any


def _xla_disabled_error(primitive_name: str,
                        extra_msg: Optional[str] = None) -> Exception:
  msg = f"Call to {primitive_name} cannot be converted with enable_xla=False."
  if extra_msg:
    msg += f" {extra_msg}"
  return NotImplementedError(msg)


def _conv_error(msg):
  suffix = ("See source code for the precise conditions under which "
            "convolutions can be converted without XLA.")
  return _xla_disabled_error("conv_general_dilated", f"{msg} - {suffix}")


def _unimplemented(name):

  def op(*arg, **kwargs):
    raise _xla_disabled_error(name)

  return op


def _transpose_for_tf_conv(lhs, rhs, dimension_numbers):
  """Tranposes lhs and rhs to respectively NHWC and HWIO so they can be passed to TF functions."""
  # TODO(marcvanzee): Add tests for this ops for shape polymorphism.
  lhs_perm, rhs_perm, _ = dimension_numbers

  # TODO(marcvanzee): Consider merging tranposes if we want to optimize.
  # For `lhs_perm` / `output_perm`, perm (0, 1, 2, 3) corresponds to "NCHW".
  lhs = tf.transpose(lhs, lhs_perm)  # lhs --> "NCHW"
  if len(lhs_perm) == 3:
    # For 1D convolution, we add a trivial "W" dimension, so that 2D Convolution
    # logic can be applied downstream.
    lhs = lhs[:, :, :, np.newaxis]
  # However, the TF ops only support "NHWC" on CPU, so we transpose again.
  lhs = tf.transpose(lhs, (0, 2, 3, 1))  # "NCHW" --> "NHWC"

  # For `rhs_perm`, perm (0, 1, 2, 3) corresponds to "OIHW".
  rhs = tf.transpose(rhs, rhs_perm)  # rhs --> "OIHW"
  # Handle conv1d case.
  if len(rhs_perm) == 3:
    rhs = rhs[:, :, :, np.newaxis]
  # For the tf ops, rhs is expected to be "OIHW".
  rhs = tf.transpose(rhs, (2, 3, 1, 0))  # "OIHW" --> "HWIO"
  return lhs, rhs


def pads_to_padtype(in_shape, window_shape, window_strides, padding) -> str:
  for pad_str in ["VALID", "SAME"]:
    pads = lax.padtype_to_pads(in_shape, window_shape, window_strides, pad_str)
    if list(pads) == list(padding):
      return pad_str
  return "EXPLICIT"


def _pad_spatial_dims(in_shape, padding, is_conv1d):
  """Pads `in_shape` using `padding`, which specifies padding for the spatial dimensions."""
  # Add empty padding for batch and feature dimensions.
  no_pad = tf.constant([[0, 0]])
  if is_conv1d:
    padding = tf.concat([no_pad, padding, no_pad], 0)
    # Add empty padding for dummy dimension, too.
    padding = tf.concat([no_pad, padding, no_pad, no_pad], 0)
  else:
    padding = tf.concat([no_pad, padding, no_pad], 0)
    in_shape = tf.pad(in_shape, padding)
  return in_shape


def _conv_transpose_pads_to_padtype(kernel_sdims, lhs_dilation, padding):
  """Finds the padding type for a transpose convolution."""
  # This is simply checking agreement with lax._conv_transpose_padding.
  is_valid = True
  is_same = True
  if not len(kernel_sdims) == len(lhs_dilation) == len(padding):
    raise ValueError(f'Found different lengths for '
                     f'kernel_sdims ({kernel_sdims}), '
                     f'lhs_dilation ({lhs_dilation}), '
                     f'and padding ({padding}).')
  for k, s, (begin, end) in zip(kernel_sdims, lhs_dilation, padding):
    # Check for VALID padding.
    pad_len_valid = k + s - 2 + builtins.max(k - s, 0)
    pad_a = k - 1
    pad_b = pad_len_valid - pad_a
    if begin != pad_a or end != pad_b:
      is_valid = False

    # Check for SAME padding.
    pad_len_same = k + s - 2
    if s > k - 1:
      pad_a = k - 1
    else:
      pad_a = int(np.ceil(pad_len_same / 2))
    pad_b = pad_len_same - pad_a
    if begin != pad_a or end != pad_b:
      is_same = False

  if is_valid:
    return 'VALID'
  elif is_same:
    return 'SAME'
  raise ValueError('Transpose convolution padding mode must be '
                   '`SAME` or `VALID`.')

def _validate_spatial_dimensions(nr_spatial_dimensions):
  """Check spatial dimension support."""
  # Currently we only support 1D+2D convolutions because it keeps the code
  # relatively simple and covers most cases.
  if nr_spatial_dimensions > 2:
    raise _conv_error(
        "We only support 1D or 2D convolutions, but found "
        f"{nr_spatial_dimensions}.")


def _normalize_padding_and_dilations(
      padding, lhs_dilation, rhs_dilation, is_conv1d):
  if is_conv1d:
    lhs_dilation = list(lhs_dilation) + [1]
    rhs_dilation = list(rhs_dilation) + [1]
    # Empty padding in the dummy dimension.
    # Note that when kernel_size=stride=1, padding of (0, 0) is both 'VALID' and
    # 'SAME'. So the inferred padding type will still register according to the
    # first dimension padding.
    padding = list(padding) + [(0, 0)]
  return padding, lhs_dilation, rhs_dilation

def _normalize_window_strides(window_strides):
  """Ensure window_strides has length 4."""
  # Some TF ops require len(window_strides) == 4 while others do not. We simply
  # ensure it always has len(4).
  if len(window_strides) == 1:
    # This is the Conv1D case. We add a dummy dimension to allow using 2D ops,
    # and use stride=1 on the dummy dimension.
    window_strides = list(window_strides) + [1]
  if len(window_strides) == 2:
    window_strides = [1] + list(window_strides) + [1]
  return window_strides

def _normalize_output_perm(output_perm, is_conv1d):
  """Ensure that output_perm has length 4."""
  if is_conv1d:
    output_perm = list(output_perm) + [1]
  return output_perm

def _validate_conv_features(
      is_transpose, is_atrous, is_depthwise, feature_group_count,
      batch_group_count, preferred_element_type, lhs_dtype):
  if feature_group_count > 1 and not is_depthwise:
    raise _conv_error("Grouped convolutions are unsupported")
  if (is_depthwise and is_atrous) and not is_transpose:
    # We allow dilated depthwise convolutions.
    pass
  elif [is_depthwise, is_atrous, is_transpose].count(True) > 1:
    raise _conv_error(
        f"Can only do one of depthwise ({is_depthwise}), atrous ({is_atrous}) "
        f"and tranposed convolutions ({is_transpose})")

  # We can implement batch grouping when there is a need for it.
  if batch_group_count != 1:
    raise _conv_error("Unimplemented support for batch_group_count != 1 "
                f"(found {batch_group_count})")

  if (preferred_element_type is not None and
      preferred_element_type != lhs_dtype):
    raise _conv_error("Unimplemented support for preferred_element_type")


def _conv_general_dilated(
    lhs, rhs, *, window_strides, padding, lhs_dilation, rhs_dilation,
    dimension_numbers: lax.ConvDimensionNumbers, feature_group_count: int,
    batch_group_count: int, lhs_shape: Sequence[int], rhs_shape: Sequence[int],
    precision: Optional[Tuple[PrecisionType, PrecisionType]],
    preferred_element_type: Optional[DType],
    _in_avals: Sequence[core.ShapedArray], _out_aval: core.ShapedArray):
  """Implementation of lax.conv_general_dilated_p using XlaConv."""
  del lhs_shape, rhs_shape, precision  # Unused arguments.
  out_shape = jax2tf._aval_to_tf_shape(_out_aval)
  _validate_spatial_dimensions(len(lhs.shape) - 2)
  is_conv1d = len(lhs.shape) - 2 == 1

  tf_window_strides = _normalize_window_strides(window_strides)
  padding, lhs_dilation, rhs_dilation = _normalize_padding_and_dilations(
      padding, lhs_dilation, rhs_dilation, is_conv1d)

  lhs, rhs = _transpose_for_tf_conv(lhs, rhs, dimension_numbers)

  in_channels = lhs.shape[-1]
  *rhs_spatial_shapes, _, rhs_out_channel = rhs.shape

  is_transpose = any([d != 1 for d in lhs_dilation])
  is_atrous = any([d != 1 for d in rhs_dilation])
  is_depthwise = in_channels == feature_group_count and feature_group_count > 1
  _validate_conv_features(is_transpose, is_atrous, is_depthwise,
                          feature_group_count, batch_group_count,
                          preferred_element_type, lhs.dtype.as_numpy_dtype)

  rhs_dilated_shape = [
      (k - 1) * r + 1 for k, r in zip(rhs_spatial_shapes, rhs_dilation)
  ]
  output_perm = dimension_numbers[2]

  if is_transpose:
    padding_type = _conv_transpose_pads_to_padtype(
        rhs_spatial_shapes, lhs_dilation, padding)
  else:
    padding_type = pads_to_padtype(
      lhs.shape[1:3], rhs_dilated_shape, window_strides, padding)
    # We only manually pad if we aren't using a tranposed convolutions.
    if padding_type == "EXPLICIT":
      lhs = _pad_spatial_dims(lhs, padding, is_conv1d)
      padding_type = "VALID"

  if any(r > l for l, r in zip(lhs.shape[1:3], rhs_dilated_shape)
        ) and padding_type != "SAME":
    # If the filter shape is bigger than the input shape in a spatial dimension,
    # lax returns only zeros while tf.conv2d returns an error.
    # We thus return zeros to make sure the behavior is consistent.
    return tf.broadcast_to(tf.constant(0, dtype=tf.float32), out_shape)

  if is_depthwise:
    # Reshape filter from
    # [filter_height, filter_width, 1, in_channels * channel_multiplier] to
    # [filter_height, filter_width, in_channels, channel_multiplier].
    new_rhs_shape = tuple(rhs_spatial_shapes) + (in_channels,
                                                 rhs_out_channel // in_channels)
    output = tf.nn.depthwise_conv2d(
        input=lhs,
        filter=tf.reshape(rhs, new_rhs_shape),
        strides=tf_window_strides,
        padding=padding_type,
        dilations=rhs_dilation)

  elif is_transpose:
    # tf.nn.conv2d_transpose requires a transposed filter.
    rhs_t = tf.reverse(rhs, [0, 1])
    rhs_t = tf.transpose(rhs_t, (0, 1, 3, 2))

    # We should tranpose `out_shape` to "NHWC", which is what TF expects.
    # First transpose to "NCHW".
    if is_conv1d:
      tf_out_shape = tuple(out_shape[i] for i in output_perm) + (1,)
    else:
      tf_out_shape = tuple(out_shape[i] for i in output_perm)
    # Then transpose "NCHW" to "NHWC".
    tf_out_shape = tuple(tf_out_shape[i] for i in (0, 2, 3, 1))
    output = tf.nn.conv2d_transpose(
        input=lhs,
        filters=rhs_t,
        output_shape=tf_out_shape,
        strides=lhs_dilation,
        padding=padding_type)

  else:
    output = tf.nn.conv2d(
        input=lhs,
        filters=rhs,
        strides=tf_window_strides,
        padding=padding_type,
        dilations=rhs_dilation)

  # TF outputs in format "NHWC", so convert to "NCHW", which is lax's default
  # format.
  output = tf.transpose(output, (0, 3, 1, 2))  # "NHWC" --> "NCHW"
  if is_conv1d:
    output = output[:, :, :, 0]
  # To determine the right permutation, we compute the inverse permutation of
  # `output_perm`, so that when `output_perm` is applied to `output`, we obtain
  # the outpt in NCHW format.
  inverse_perm = tf.math.invert_permutation(output_perm)
  output = tf.transpose(output, inverse_perm)  # "NCHW" -> desired output shape.
  return output


tf_impl_no_xla[lax.conv_general_dilated_p] = _conv_general_dilated


def _dot_general(lhs, rhs, *, dimension_numbers,
                 precision: Optional[Tuple[PrecisionType, PrecisionType]],
                 preferred_element_type: Optional[DType],
                 _in_avals: Sequence[core.ShapedArray],
                 _out_aval: core.ShapedArray):
  """Implementation of lax.dot_general_p in terms of tf.linalg.einsum."""
  (lhs_contracting, rhs_contracting), (lhs_batch, rhs_batch) = dimension_numbers
  lhs_ndim, rhs_ndim = len(lhs.shape), len(rhs.shape)

  # This condition ensures that:
  # 1) the batch dimensions are ordered in the same way in lhs and rhs (this is
  #    not strictly necessary, but we would have to reshape the array if that
  #    were not the case;
  # 2) lhs and rhs have the same number of dimensions +/- 1
  # 3) the number of non-batch dimensions in both tensors is either 1 or 2
  # 4) the contracting dimensions are consistent with those of a classic
  #    matrix/matrix, vector/matrix or matrix/vector multiplication.
  if (lhs_batch == rhs_batch == tuple(range(len(lhs_batch))) and
      lhs_ndim - rhs_ndim in [-1, 0, 1] and
      1 <= lhs_ndim - len(lhs_batch) <= 2 and
      1 <= rhs_ndim - len(rhs_batch) <= 2 and
      lhs_contracting == (len(lhs.shape) - 1,) and
      rhs_contracting == (len(lhs_batch),)):
    # All the inputs to tf.linalg.matmul must have 2 inner dimensions,
    # after their batch dimensions, so we need to expand the dimensions
    # appropriately. We can get to this branch with three combinations of
    # inner shapes:
    # - lhs.inner_shape == [a, b], rhs.inner_shape == [b, c]
    #   - in this case, the resulting inner shape is [a, c];
    # - lhs.inner_shape == [b]   , rhs.inner_shape == [b, c]
    #   - in this case, we need to expand lhs to [1, b], and the resulting
    #     shape is [c]. We need to squeeze the result of tf.linalg.matmul
    #     as it will have shape [1, c];
    # - lhs.shape == [batch] + [a, b], rhs.shape == [batch] + [b]
    #   - in this case, we need to expand rhs to [b, 1], and the resulting
    #     shape is [a]. We need to squeeze the result of tf.linalg.matmul
    #     as it will have shape [a, 1];
    # - lhs.shape == [batch] + [b]   , rhs.shape == [batch] + [b]
    #   - in this case, we need to expand lhs to [1, b] and rhs to [b, 1],
    #     and the resulting shape is (). We need to squeeze the result of
    #     tf.linalg.matmul as it will have shape [1, 1].
    squeeze_idxs = []
    if lhs_ndim - len(lhs_batch) == 1:
      lhs = tf.expand_dims(lhs, lhs_ndim - 1)
      squeeze_idxs.append(len(lhs.shape) - 2)
    if rhs_ndim - len(rhs_batch) == 1:
      rhs = tf.expand_dims(rhs, rhs_ndim)
      squeeze_idxs.append(len(rhs.shape) - 1)
    result = tf.linalg.matmul(lhs, rhs)
    if len(squeeze_idxs) != 0:
      assert all([result.shape[i] == 1 for i in squeeze_idxs])
      result = tf.squeeze(result, squeeze_idxs)
    return result

  new_id = iter(string.ascii_letters)
  lhs_axis_ids = [next(new_id) for _ in lhs.shape]
  rhs_axis_ids = [next(new_id) for _ in rhs.shape]
  lhs_out_axis_ids = lhs_axis_ids[:]
  rhs_out_axis_ids = rhs_axis_ids[:]

  for lhs_axis, rhs_axis in zip(lhs_contracting, rhs_contracting):
    shared_id = next(new_id)
    lhs_axis_ids[lhs_axis] = shared_id
    rhs_axis_ids[rhs_axis] = shared_id
    lhs_out_axis_ids[lhs_axis] = None  # type: ignore[call-overload]
    rhs_out_axis_ids[rhs_axis] = None  # type: ignore[call-overload]

  batch_ids = []
  for lhs_axis, rhs_axis in zip(lhs_batch, rhs_batch):
    shared_id = next(new_id)
    lhs_axis_ids[lhs_axis] = shared_id
    rhs_axis_ids[rhs_axis] = shared_id
    lhs_out_axis_ids[lhs_axis] = None  # type: ignore[call-overload]
    rhs_out_axis_ids[rhs_axis] = None  # type: ignore[call-overload]
    batch_ids.append(shared_id)

  not_none = lambda x: x is not None
  out_axis_ids = list(
      filter(not_none, batch_ids + lhs_out_axis_ids + rhs_out_axis_ids))
  assert lhs.dtype == rhs.dtype
  spec = "{},{}->{}".format("".join(lhs_axis_ids), "".join(rhs_axis_ids),
                            "".join(out_axis_ids))
  return tf.linalg.einsum(spec, lhs, rhs)


tf_impl_no_xla[lax.dot_general_p] = _dot_general


def _interior_padding(operand, padding_value, padding_config, operand_shape):
  # Used only when enable_xla=False
  # Applies only the interior padding from the padding_config.
  # We do this somewhat inefficiently, as as a scatter.
  # For each dimension we compute the indices_by_dim as [0, f, 2f, 3f, ...] where
  # f is the dilation factor for the dimension, i.e., 1 + interior_padding.
  # Then we compute the cartesian production of the indices (using broadcast
  # and concat).

  # We could make this code more complex and do all the padding at once, but
  # we prefer to keep it simple.
  indices_by_dim = []
  indices_shape = operand_shape + (1,)
  output_shape = []  # considering only interior padding
  for d, (dsz, (_, _, i)) in enumerate(zip(operand_shape, padding_config)):
    dilation_factor = i + 1
    output_shape.append(dsz * dilation_factor - i)
    indices = tf.range(dsz) * dilation_factor
    expansion = [None] * (1 + len(operand_shape))
    expansion[d] = slice(None, None, None)
    indices_by_dim.append(tf.broadcast_to(indices[expansion], indices_shape))

  indices_cartesian = tf.concat(indices_by_dim, axis=len(operand_shape))
  scattered = tf.scatter_nd(indices_cartesian, operand, output_shape)
  # What elements from the output array we use from
  mask = tf.scatter_nd(indices_cartesian, tf.ones_like(operand, dtype=np.bool_),
                       output_shape)
  return tf.where(mask, scattered, padding_value)


def _pad(operand, padding_value, *, padding_config,
         _in_avals: Sequence[core.ShapedArray], _out_aval: core.ShapedArray):
  low, high, interior = util.unzip3(padding_config)

  # Do only the interior padding first. This is rarely needed.
  if any(i != 0 for _, _, i in padding_config):
    operand = _interior_padding(operand, padding_value, padding_config,
                                jax2tf._eval_shape(_in_avals[0].shape))

  # Now do the non-negative edge padding. This is the common case, use tf.pad.
  non_negative_padding = [((lo if lo >= 0 else 0), (hi if hi >= 0 else 0))
                          for lo, hi, _ in padding_config]
  operand = tf.pad(
      operand,
      non_negative_padding,
      mode="CONSTANT",
      constant_values=padding_value)
  # Now the negative edge padding (this is also rare)
  if any(lo < 0 or hi < 0 for lo, hi, _ in padding_config):
    output_shape = jax2tf._eval_shape(_out_aval.shape)
    begins = [(-lo if lo < 0 else 0) for lo, _, _ in padding_config]
    operand = tf.slice(operand, begins, output_shape)

  return operand


tf_impl_no_xla[lax.pad_p] = _pad


def _argminmax(is_min: bool, operand: TfVal, axes: Sequence[int],
               index_dtype: DType, _in_avals: Sequence[core.ShapedArray],
               _out_aval: core.ShapedArray):
  # The following is known to diverge from JAX behavior for NaN.
  axis, = axes
  output_type = tf.int32
  if dtypes.iinfo(index_dtype).bits > 32:
    output_type = tf.int64
  # TODO(phawkins): handle axes larger than 2^31.
  fn = tf.math.argmin if is_min else tf.math.argmax
  result = fn(operand, axis=axis, output_type=output_type)
  return tf.cast(result, jax2tf._to_tf_dtype(index_dtype))


tf_impl_no_xla[lax.argmin_p] = partial(_argminmax, True)
tf_impl_no_xla[lax.argmax_p] = partial(_argminmax, False)


# _reduce_windown currently only supports reduce_window_max and
# reduce_window_sum.
# TODO(bchetioui): this function is not exhaustive wrt which
# reduce_window_max or reduce_window_sum cases can be translated into a call to
# max_pool or avg_pool. Further investigation is needed to fully flesh it out.
def _reduce_window(operand, *, window_dimensions, window_strides, padding,
            base_dilation, window_dilation, _in_avals, _out_aval, name=None) -> TfVal:

  def error(msg):
    suffix = ("See source code for the precise conditions under which "
              "reduce_window can be converted without XLA.")
    return _xla_disabled_error(name, f"{msg} - {suffix}")

  dtype = operand.dtype
  # Contrarily to the main path, tf.int8 is actually a valid type for
  # tf.nn.max_pool.
  if name == "reduce_window_max" and dtype in [
      tf.bool, tf.uint32, tf.uint64, tf.complex64, tf.complex128
  ]:
    raise error(f"tf.nn.max_pool does not support operands of type {dtype}")
  if name == "reduce_window_sum" and operand.dtype not in [
      tf.float16, tf.float32, tf.float64
  ]:
    raise error(f"tf.nn.avg_pool does not support operands of type {dtype}")
  has_batch_dim = window_dimensions[0] == 1
  has_channel_dim = window_dimensions[-1] == 1
  nb_spatial_dimensions = len(operand.shape) - has_batch_dim - has_channel_dim
  if nb_spatial_dimensions < 1 or nb_spatial_dimensions > 3:
    raise error("TensorFlow can only handle pooling for arrays with 1, 2, or "
                "3 spatial dimensions")
  # TODO(bchetioui): does a simple conversion with another base dilation exist?
  if list(base_dilation) != [1] * len(operand.shape):
    raise error("Unimplemented support for base dilation")
  # TODO(bchetioui): does a simple conversion with another window_dilation
  # exist? The whole story seems similar to convolution.
  if list(window_dilation) != [1] * len(operand.shape):
    raise error("Unimplemented support for window dilation")

  tf_padding = pads_to_padtype(operand.shape, window_dimensions, window_strides, padding)
  if tf_padding == "EXPLICIT":
    raise error("Padding should either be 'VALID' or 'SAME'.")

  # ReduceWindow in XLA takes an array of rank N as a parameter, but
  # tf.nn.max_pool / tf.nn.avg_pool take an array of rank N+2, with a default
  # shape of the form [batch_size] + input_spatial_shape + [num_channels]
  tf_operand = operand
  tf_window_dimensions = list(window_dimensions)
  tf_window_strides = list(window_strides)
  if not has_batch_dim:
    tf_operand = tf.expand_dims(tf_operand, 0)
    tf_window_dimensions = [1] + tf_window_dimensions
    tf_window_strides = [1] + tf_window_strides
  if not has_channel_dim:
    tf_operand = tf.expand_dims(tf_operand, -1)
    tf_window_dimensions.append(1)
    tf_window_strides.append(1)
  tf_data_format = "N" + "DHW"[-nb_spatial_dimensions:] + "C"

  if name == "reduce_window_max":
    result = tf.nn.max_pool(tf_operand, tf_window_dimensions, tf_window_strides,
                            tf_padding, tf_data_format)
  elif name == "reduce_window_sum":
    avg = tf.nn.avg_pool(tf_operand, tf_window_dimensions, tf_window_strides,
                         tf_padding, tf_data_format)
    result = avg * np.prod(tf_window_dimensions)
  else:
    raise error("Only reduce_window_max and reduce_window_sum are supported.")

  if not has_batch_dim:
    result = tf.squeeze(result, 0)
  if not has_channel_dim:
    result = tf.squeeze(result, -1)
  return result


# pylint: disable=protected-access
tf_impl_no_xla[lax.reduce_window_sum_p] = (
    partial(_reduce_window, name="reduce_window_sum"))
tf_impl_no_xla[lax.reduce_window_max_p] = (
    partial(_reduce_window, name="reduce_window_max"))
# pylint: enable=protected-access

tf_impl_no_xla[lax.reduce_window_min_p] = _unimplemented("reduce_window_min")
tf_impl_no_xla[lax.reduce_window_p] = _unimplemented("reduce_window")

tf_impl_no_xla[lax.reduce_p] = _unimplemented("reduce")

tf_impl_no_xla[lax.select_and_scatter_add_p] = _unimplemented(
    "select_and_scatter_add")

tf_impl_no_xla[lax.rng_bit_generator_p] = _unimplemented("rng_bit_generator")


def _clip(max_indices: Sequence[TfVal], start_indices: Sequence[TfVal],
          slice_sizes: Sequence[TfVal]):
  """Simulates XLA clipping behavior with TF ops.

  Various TF ops have different clipping behavior than XLA:
  * If `start_indices` is out-of-bounds, then TF fails but XLA clips the indices
  to
    [0, max_len].
  * If `start_indices + slice_size` is out-of-bounds, then TF fails, but XLA
  adjust
    `start_indices` so that a full slice is returned.
  This function clips the start indices correctly.
  """
  # We cast both arguments to `tf.clip_by_value` to int32. Otherwise, this
  # function may return uint32 which is not always compatible with TF ops, so
  # this may result in type errors.
  max_start = tf.cast(tf.subtract(max_indices, slice_sizes), dtype=tf.int32)
  return tf.clip_by_value(tf.cast(start_indices, dtype=tf.int32), 0, max_start)


@dataclasses.dataclass
class GatherArgs:
  operand: TfVal
  start_indices: TfVal
  dnums: lax.GatherDimensionNumbers
  slice_sizes: TfVal
  op_shape: core.Shape
  start_indices_shape: core.Shape
  out_aval: core.ShapedArray

  def __post_init__(self):
    assert len(self.op_shape) == len(self.slice_sizes)

  def __repr__(self):
    return (f"operand shape={self.op_shape}, "
            f"start_indices={self.start_indices}, "
            f"dimension_numbes={self.dnums}, "
            f"slice_sizes={self.slice_sizes}")


def gather_precondition(precondition_fn: Callable[[GatherArgs], None]):
  """Decorator for specifying a precondition function.

  This decorator should be put on a function with argument `arg` of type
  `GatherArgs`. It will first call `precondition_fn` with `arg` (which may throw
  an exception), and then call the function it is decorating with `arg` as well.
  """

  def decorator(gather_fn: Callable[[GatherArgs], Any]):

    @wraps(gather_fn)
    def wrapper(args: GatherArgs):
      # Call `precondition_fn`; we assume it may throw an exception.
      precondition_fn(args)
      return gather_fn(args)

    return wrapper

  return decorator


def _pre_gather_for_scalar_indexing(args: GatherArgs):
  """Returns True if this call to gather represents scalar indexing into arrays.

  E.g., op[2], op[:, :5, :], jnp.take(op, 0, axis=0).
  """
  # TODO(marcvanzee): Add more assumptions here, because this is currently too
  # permissive.
  if len(args.start_indices_shape) != 1:
    raise ValueError("start_indices shape should be 1")


@gather_precondition(_pre_gather_for_scalar_indexing)
def _gather_for_scalar_indexing(args: GatherArgs):
  """Implements 'scalar indexing into arrays' cases of lax.gather using tf.slice.

  E.g., op[2], op[:, :5, :], jnp.take(op, 0, axis=0).
  """
  indices = tf.expand_dims(args.dnums.start_index_map, 1)
  # lax.gather uses an "index map" which maps `start_indices` to the right axes
  # in `operand`. Since tf.strided_slice uses a single array for specifying the
  # start indices, we use a scatter to map the start indices to the right axes.
  op_shape = jax2tf._eval_shape(args.op_shape)
  slice_sizes_tf = jax2tf._eval_shape(args.slice_sizes)
  # TODO(marcvanzee): Consider transposing `operand`, which is probably more
  # optimization friendly.
  begin = tf.scatter_nd(indices, args.start_indices, [len(op_shape)])
  begin = _clip(op_shape, begin, slice_sizes_tf)
  end = slice_sizes_tf + begin

  # `collapsed_slice_dims` is a tuple of dimensions to collapse, e.g. (0, 2).
  # `tf.strided_slice` expects a binary mask to specify the shrink axes, i.e.,
  # if we want to shrink axis 0 and 2, this corresponds to binary mask 101,
  # which is 5 in decimals. The following line converts the lax representation
  # to the one used by `tf.strided_slice`.
  shrink_mask = sum(2**x for x in args.dnums.collapsed_slice_dims)
  res = tf.strided_slice(args.operand, begin, end, shrink_axis_mask=shrink_mask)
  # Shape inference doesn't work for tf.strided_slice.
  res.set_shape(jax2tf._aval_to_tf_shape(args.out_aval))
  return res


def _pre_gather_for_multidim_indexing(args: GatherArgs):
  """Returns True if this call to gather represents multi-dimensional indexing.

  E.g., jnp.take(op, [[0], [1]], axis=0).
  Note we currently only support multi-dimensional indexing if the last
  dimension is 1.
  """
  # Handle only the case when tf.gather argument batch_dims=0.
  # Find axis to match the tf.gather semantics
  # Let I = len(start_indices_shape)
  # let O = len(op_shape)
  # slice_sizes == op_shape[:axis] + (1,) + op_shape[axis+1:]
  # collapsed_slice_dims == (axis,)
  # start_index_map == (axis,)
  # offset_dims == (0, 1, ..., axis - 1, axis + I, ..., O + I - 1)
  # We added a trailing dimension of size 1
  op_shape = args.op_shape
  start_index_map = args.dnums.start_index_map
  collapsed_slice_dims = args.dnums.collapsed_slice_dims
  offset_dims = args.dnums.offset_dims
  if not (len(op_shape) >= 1 and len(start_index_map) == 1 and
          len(collapsed_slice_dims) == 1 and collapsed_slice_dims[0]
          == start_index_map[0] and len(offset_dims) == len(op_shape) - 1):
    raise ValueError("unsupported dimension numbers")
  # We added a trailing dimension of size 1
  if not core.symbolic_equal_dim(args.start_indices_shape[-1], 1):
    raise ValueError("start_indices shape[-1] should be 1")
  # Guess the axis
  axis = collapsed_slice_dims[0]
  index_dims = len(args.start_indices_shape) - 1
  expected_offset_dims = tuple(
      list(range(axis)) +
      list(range(axis + index_dims,
                 len(op_shape) + index_dims - 1)))
  if offset_dims != expected_offset_dims:
    raise ValueError("unsupported offset_dims")
  expected_slice_sizes = op_shape[:axis] + (1,) + op_shape[axis + 1:]  # type: ignore
  if not core.symbolic_equal_shape(args.slice_sizes, expected_slice_sizes):
    raise ValueError("unsupported slice_sizes")


@gather_precondition(_pre_gather_for_multidim_indexing)
def _gather_for_multidim_indexing(args: GatherArgs):
  """Implements 'multi-dimensional indexing into arrays' cases of lax.gather using tf.gather.

  E.g., jnp.take(op, [[0], [1]], axis=0).
  """
  # Guess the axis.
  axis = args.dnums.collapsed_slice_dims[0]
  squeezed_indices = tf.squeeze(args.start_indices, -1)
  op_shape = jax2tf._eval_shape(args.op_shape)
  start_indices = _clip((op_shape[axis],), squeezed_indices, (1,))
  return tf.gather(args.operand, start_indices, axis=axis, batch_dims=0)


def _pre_gather_with_batch_dims(args: GatherArgs):
  """Returns True if this call to gather has non-empty batch dimensions.

  This is for instance triggered when doing jax.vmap(lax.dynamic_slice).
  """
  # All dimensions in the output array and not in offset_dims are batch_dims.
  batch_dims = tuple(
      x for x in range(len(args.out_aval.shape))
      if x not in args.dnums.offset_dims)

  # We assume exactly one batch (and one or more non-batch dimensions).
  if len(batch_dims) != 1:
    raise ValueError(f"batch_dims is {len(batch_dims)} but should be 1")

  # `start_index_map` maps indices in `start_indices` to indices in `operand`.
  # For simplicity, we currently only consider the case where this mapping is
  # the identity function, i.e., [2, 3] in `start_indices` maps to
  # `operand[2, 3]`.
  if args.dnums.start_index_map != tuple(range(args.start_indices_shape[-1])):
    raise ValueError("unsupported start_index_map")

  # The batch dims in `start_indices` and `operand` should agree.
  if not core.symbolic_equal_dim(args.op_shape[0], args.start_indices_shape[0]):
    raise ValueError("Batch dimensions in operand and start_indices don't "
                     "agree")


@gather_precondition(_pre_gather_with_batch_dims)
def _gather_with_batch_dims(args: GatherArgs):
  """Implements call to gather with non-empty batch dimensions.

  E.g., when doing `jax.vmap(lax.dynamic_slice).
  """
  op_shape = jax2tf._eval_shape(args.op_shape)
  start_indices = _clip(op_shape, args.start_indices, args.slice_sizes)
  result = tf.map_fn(
    lambda idxs: tf.slice(args.operand, begin=idxs, size=args.slice_sizes),
    start_indices,
    fn_output_signature=jax2tf._to_tf_dtype(args.operand.dtype)
  )
  result = tf.squeeze(result, axis=1)
  return result


def _gather(operand, start_indices, *, dimension_numbers,
            slice_sizes: core.Shape, indices_are_sorted, unique_indices, mode,
            fill_value, _in_avals: Sequence[core.ShapedArray],
            _out_aval: core.ShapedArray):
  """Tensorflow implementation of gather."""
  if mode == lax.GatherScatterMode.FILL_OR_DROP:
    gather_fill_fn = jax2tf._convert_jax_impl(lax_slicing._gather_fill,
                                              multiple_results=False)
    return gather_fill_fn(
        operand, start_indices, dimension_numbers=dimension_numbers,
        slice_sizes=slice_sizes, unique_indices=unique_indices,
        indices_are_sorted=indices_are_sorted, fill_value=fill_value,
        output_shape=_out_aval.shape, _in_avals=_in_avals, _out_aval=_out_aval)

  # TODO(marcvanzee): Check if we need more tests in shape_poly for gather with
  # enable_xla=False.
  gather_args = GatherArgs(
      operand=operand,
      start_indices=start_indices,
      dnums=dimension_numbers,
      slice_sizes=slice_sizes,
      op_shape=_in_avals[0].shape,
      start_indices_shape=_in_avals[1].shape,
      out_aval=_out_aval)

  errors = []

  for gather_fn in [
      _gather_for_scalar_indexing, _gather_for_multidim_indexing,
      _gather_with_batch_dims
  ]:
    try:
      return gather_fn(gather_args)
    except ValueError as e:
      errors.append(f"{gather_fn}: {repr(e)}")

  error_msg = (f"Unsupported arguments for gather: {gather_args}, errors:\n" +
               "\n".join(errors))

  raise _xla_disabled_error("gather", error_msg)


tf_impl_no_xla[lax.gather_p] = _gather


def _dynamic_slice(operand, *start_indices, slice_sizes: core.Shape,
                   _in_avals: Sequence[core.ShapedArray],
                   _out_aval: core.ShapedArray):
  start_indices = tf.stack(start_indices)
  slice_sizes_tf = jax2tf._eval_shape(slice_sizes)

  operand_shape = jax2tf._eval_shape(_in_avals[0].shape)
  start_indices = _clip(operand_shape, start_indices, slice_sizes_tf)
  return tf.slice(operand, start_indices, size=slice_sizes_tf)


tf_impl_no_xla[lax.dynamic_slice_p] = _dynamic_slice


def _dynamic_update_slice(operand, update, *start_indices,
                          _in_avals: Sequence[core.ShapedArray],
                          _out_aval: core.ShapedArray):
  start_indices = tf.stack(start_indices)

  op_shape = jax2tf._eval_shape(_in_avals[0].shape)
  op_size = tf.size(operand)
  update_shape_tf = jax2tf._eval_shape(_in_avals[1].shape)

  start_indices = _clip(op_shape, start_indices, update_shape_tf)
  end_indices = tf.add(start_indices, update_shape_tf)
  flatten = tf.keras.backend.flatten

  # Get the cells to update in `operand` as an array of ids.
  id_tensor = tf.reshape(tf.range(op_size), op_shape)
  scattered_indices = tf.strided_slice(id_tensor, start_indices, end_indices)

  # Create an array containing updates at scattered_indices and zeros otherwise.
  flat_indices = tf.expand_dims(flatten(scattered_indices), -1)
  flat_update = flatten(update)
  update = tf.scatter_nd(flat_indices, flat_update, (op_size,))
  update = tf.reshape(update, op_shape)

  # Create a bool mask that is True only where `operand` should be updated.
  update_mask = tf.ones_like(flat_update, dtype=tf.bool)
  update_mask = tf.scatter_nd(flat_indices, update_mask, (op_size,))
  update_mask = tf.reshape(update_mask, op_shape)

  # Use the mask to only update `operand` with `update`.
  return tf.where(update_mask, update, operand)


tf_impl_no_xla[lax.dynamic_update_slice_p] = _dynamic_update_slice


def shift_axes_forward(operand, axes: tuple, inverse: bool=False,
                       forward: bool=True):
  """Shifts the tuple of axes to the front of an array"""
  other_axes = tuple([i for i in range(len(operand.shape)) if i not in axes])
  fwd_order = axes + other_axes if forward else other_axes + axes
  order = fwd_order if not inverse else tf.math.invert_permutation(fwd_order)
  return tf.transpose(operand, order)

def convert_scatter_jax_to_tf(update_op, unsorted_segment_op=None):
  def error(msg):
    suffix = ("See source code for the precise conditions under which "
              "scatter_(update/add/multiply/min/max) ops can be converted without XLA.")
    return _xla_disabled_error("scatter_(update/add/multiply/min/max)", f"{msg} - {suffix}")

  def _sparse_scatter(
    operand,
    scatter_indices,
    updates,
    update_jaxpr,
    update_consts,
    dimension_numbers,
    indices_are_sorted: bool,
    unique_indices: bool,
    mode,
    _in_avals: Sequence[core.ShapedArray],
    _out_aval: core.ShapedArray):
    """
    Implementation of scatter specialised to indexing from the
    front axes. This covers unique indices and non-unique indices
    of single depth.

    Note on unique indices: `tf.tensor_scatter_nd_update` interprets
    indices thusly: every axis except the final one encodes a batch
    dimension, the final axis encoding the actual indices to scatter in to.
    It enforces, at least one, batch dimension so we add an empty
    dimension to indices and updates if lacking.

    Note on non-unique indices: There is no tf op for non single depth
    indexing. But if indexing is single depth, this can be viewed as a
    segment op.
    """
    # Infer unique indices from lack of batch dimension
    unique_indices = unique_indices or (len(scatter_indices.shape) == 1)
    if unique_indices:
      suboperand = tf.gather_nd(operand, scatter_indices)
      updated_suboperand = update_op(suboperand, updates)
      # add a batch dim if none exist
      if len(scatter_indices.shape) == 1:
        scatter_indices = scatter_indices[None]
        updated_suboperand = updated_suboperand[None]
      y = tf.tensor_scatter_nd_update(operand, scatter_indices, updated_suboperand)
    else:
      if (scatter_indices.shape[-1] == 1) and (unsorted_segment_op != None):
        # If only indexing into the first dimension, it's a segment op
        operand_update = unsorted_segment_op(updates, tf.squeeze(scatter_indices, -1), operand.shape[0])
        y = update_op(operand, operand_update)
      else:
        raise error("Scatter supports unique indices. Scatter also supports non-unique indices with indexing into only one dimension for (add, mul, min, max)")
    return y

  def sparse_scatter(
    operand,
    scatter_indices,
    updates,
    update_jaxpr,
    update_consts,
    dimension_numbers,
    indices_are_sorted: bool,
    unique_indices: bool,
    mode,
    _in_avals: Sequence[core.ShapedArray],
    _out_aval: core.ShapedArray):
    """
    Wrapper around the scatter function.
    The underlying tf ops `tf.tensor_scatter_nd_update` and
    `tf.math.unsorted_segment_*` index from the front dimensions.
    `tf.math.unsorted_segment_*` indexs to a depth 1 from the front.
    `tf.tensor_scatter_nd_update` indexs from the front dimensions onwards
    , with no ability to skip a dimension. This function
    shifts the axes to be indexed to the front then calls a front-specific
    implementation, then inverse-shifts the output.

    scatter_dims_to_operand_dims: dimensions which the scatter indexes in to.
      We shift these to the front to match tf syntax. All other dims are batch
    update_window_dims: dimensions which are not batch dimensions. We shift
      these to the back as the remaining dimensions are batch dimensions.
    """
    ud = dimension_numbers.update_window_dims
    wd = dimension_numbers.inserted_window_dims
    sd = dimension_numbers.scatter_dims_to_operand_dims
    dtype = operand.dtype # assume updates has same dtype as operand
    if dtype in [tf.bool, tf.complex64]:
      raise error(f"Scatter does not support operands of type {dtype}")
    if not (wd == sd):
      raise error("Complex scatters are not supported")
    if not (mode == lax.GatherScatterMode.PROMISE_IN_BOUNDS):
      raise error("Only scatter mode `PROMISE_IN_BOUNDS` is supported")
    # Shift axes to the front to match tf syntax, inverse afterwards
    fwd = partial(shift_axes_forward, axes=sd)
    inv = partial(fwd, inverse=True)
    # shift update value axes to the back, so batch are at the front
    updates_shifted = shift_axes_forward(updates, axes=ud, forward=False)
    return inv(_sparse_scatter(
      fwd(operand),
      scatter_indices,
      updates_shifted,
      update_jaxpr,
      update_consts,
      dimension_numbers,
      indices_are_sorted,
      unique_indices,
      mode,
      _in_avals,
      _out_aval,
    ))
  return sparse_scatter

tf_impl_no_xla[lax.scatter_p] = convert_scatter_jax_to_tf(lambda x,y: y) # just replace with the update
tf_impl_no_xla[lax.scatter_add_p] = convert_scatter_jax_to_tf(tf.add,      tf.math.unsorted_segment_sum)
tf_impl_no_xla[lax.scatter_mul_p] = convert_scatter_jax_to_tf(tf.multiply, tf.math.unsorted_segment_prod)
tf_impl_no_xla[lax.scatter_min_p] = convert_scatter_jax_to_tf(tf.minimum,  tf.math.unsorted_segment_min)
tf_impl_no_xla[lax.scatter_max_p] = convert_scatter_jax_to_tf(tf.maximum,  tf.math.unsorted_segment_max)

tf_impl_no_xla[lax.sort_p] = _unimplemented("sort")
