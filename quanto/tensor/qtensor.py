import torch
from torch.autograd import Function
from torch.utils import _pytree as pytree

from .core import absmax_scale, dtype_info
from .qtype import qint8, qtype


__all__ = ["qfallback", "QTensor"]


def qfallback(callable, *args, **kwargs):
    """Fallback method for QTensor inputs.

    When a torch function or an aten operation is not supported for the specified
    QTensor arguments, each QTensor arg or kwarg is dequantized to a torch.Tensor
    before calling the target function or op.
    """
    args, kwargs = pytree.tree_map_only(QTensor, lambda x: x.dequantize(), (args, kwargs or {}))
    return callable(*args, **kwargs)


class Quantizer(Function):
    """A standard symmetric quantizer.

    If the quantization scale is not specified, it uses the absmax scale for the base tensor.
    """

    @staticmethod
    def forward(ctx, base, qtype: qtype = qint8, scale=None):
        info = dtype_info(qtype.dtype)
        if scale is None:
            scale = absmax_scale(base, qtype)
        elif scale.ndim > 0:
            if torch.squeeze(scale).ndim > 1:
                raise ValueError("Quantizing along multiple axis is not supported")
            if scale.ndim != base.ndim:
                raise ValueError(
                    "When quantizing per-axis, the scale must be broadcastable to the base (Tip: try to add missing dims of length zero)."
                )
        data = base / scale
        if not qtype.is_floating_point:
            data = torch.round(data)
        data = torch.clamp(data, min=info.min, max=info.max).to(qtype.dtype)
        # The instantiation of the quantized tensor must happen within the context of the Function
        # for the autograd magic to work.
        return QTensor(qtype, data, scale)

    @staticmethod
    def backward(ctx, gO):
        # For autograd, quantization is a no-op
        return gO, None, None


class Dequantizer(Function):
    @staticmethod
    def forward(ctx, t):
        if t.qtype.is_floating_point:
            # Upcast explicitly to the scale dtype
            return t._scale * t._data.to(t._scale.dtype)
        return t._scale * t._data

    @staticmethod
    def backward(ctx, gO):
        # For autograd, dequantization is a no-op
        return gO


class QTensor(torch.Tensor):
    @staticmethod
    def __new__(cls, qtype, data, scale, requires_grad=False):
        assert data.device == scale.device
        # This constructor can ONLY create leaf Tensors wrt autograd.
        # Use QTensor.from_tensor(t) to get a non-leaf Tensor wrt autograd.
        return torch.Tensor._make_wrapper_subclass(
            cls, data.size(), strides=data.stride(), dtype=scale.dtype, device=data.device, requires_grad=requires_grad
        )

    def __init__(self, qtype, data, scale, requires_grad=False):
        self._qtype = qtype
        self._axis = None
        if scale.ndim > 0:
            if torch.squeeze(scale).ndim > 1:
                raise ValueError("QTensor cannot be quantized along multiple axis.")
            if scale.ndim != data.ndim:
                raise ValueError(
                    "The QTensor scale must be broadcastable to the base (Tip: try to add missing dims of length zero)."
                )
            dims = scale.shape
            for i in range(scale.ndim):
                if dims[i] != 1:
                    self._axis = i
            if self._axis is None:
                # All dims are 1: the scale is actually a scalar
                scale = torch.squeeze(scale)
            elif self._axis == scale.ndim - 1:
                # Align on the general convention to index the last dimension
                self._axis = -1
        self._data = data
        self._scale = scale

    def __repr__(self):  # Zero out missing values for printing
        autograd_info = (
            f", grad_fn={self.grad_fn}" if self.grad_fn else ", requires_grad=True" if self.requires_grad else ""
        )
        return f"QTensor({self._data}, scale={self._scale}, public_dtype={self.dtype}{autograd_info})"

    @classmethod
    def quantize(cls, base, qtype=qint8, scale=None):
        """Differentiable quantization function"""
        return Quantizer.apply(base, qtype, scale)

    def dequantize(self):
        """Differentiable dequantization function"""
        return Dequantizer.apply(self)

    @property
    def axis(self):
        return self._axis

    @property
    def qtype(self):
        return self._qtype

    def __tensor_flatten__(self):
        inner_tensors = ["_data", "_scale"]
        meta = {"qtype": self._qtype}
        return inner_tensors, meta

    @staticmethod
    def __tensor_unflatten__(inner_tensors, meta, outer_size, outer_stride):
        assert len(inner_tensors) == 2
        assert len(meta) == 1
        data, scale = inner_tensors["_data"], inner_tensors["_scale"]
        qtype = meta["qtype"]
        return QTensor(qtype, data, scale)

    @classmethod
    def __torch_function__(cls, func, types, args=(), kwargs=None):
        from .func import get_qtensor_func

        kwargs = kwargs or {}

        # Look for a func accepting QTensor inputs
        qfunc = get_qtensor_func(func)
        if qfunc is not None:
            return qfunc(*args, **kwargs)
        # Defer to dispatcher to look instead for QTensor operations
        with torch._C.DisableTorchFunctionSubclass():
            return func(*args, **kwargs)

    @classmethod
    def __torch_dispatch__(cls, op, types, args, kwargs=None):
        from .ops import get_qtensor_op_dispatch

        # Do not use directly op, but rather its overload
        op = op.overloadpacket
        # Look for a dispatched op accepting QTensor inputs
        qdispatch = get_qtensor_op_dispatch(op)
        if qdispatch is not None:
            return qdispatch(*args, **kwargs)
        # No dispatch available: qfallback
        return qfallback(op, *args, **kwargs)

    def numpy(self):
        return self.dequantize().cpu().numpy()