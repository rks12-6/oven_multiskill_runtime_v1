from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import msgpack
import numpy as np

from oven_runtime.common.errors import ErrorCode, fault

NDARRAY_EXT_CODE = 42
MAX_FRAME_BYTES = 64 * 1024 * 1024
MAX_ARRAY_DIMENSIONS = 16


def _encode_extension(value: Any) -> msgpack.ExtType:
    if isinstance(value, np.generic):
        value = np.asarray(value)
    if not isinstance(value, np.ndarray):
        raise TypeError(f"unsupported wire value: {type(value).__name__}")
    if value.dtype.hasobject or value.dtype.fields is not None or value.dtype.subdtype is not None:
        raise TypeError("object, structured, and sub-array dtypes are not allowed on the wire")
    # np.ascontiguousarray promotes a zero-dimensional scalar to shape (1,).
    # Keep scalar shape intact while still copying non-contiguous arrays.
    contiguous = value if value.flags.c_contiguous else np.ascontiguousarray(value)
    body = msgpack.packb(
        {
            "dtype": contiguous.dtype.str,
            "shape": list(contiguous.shape),
            "data": contiguous.tobytes(order="C"),
        },
        use_bin_type=True,
    )
    return msgpack.ExtType(NDARRAY_EXT_CODE, body)


def _decode_extension(code: int, body: bytes) -> Any:
    if code != NDARRAY_EXT_CODE:
        raise fault(ErrorCode.FRAME_DECODE_FAILED, "binary frame contains an unsupported extension")
    try:
        value = msgpack.unpackb(body, raw=False, strict_map_key=True)
        if not isinstance(value, Mapping) or set(value) != {"dtype", "shape", "data"}:
            raise ValueError("invalid ndarray envelope")
        dtype_text = value["dtype"]
        shape = value["shape"]
        data = value["data"]
        if not isinstance(dtype_text, str) or not isinstance(shape, list) or not isinstance(data, bytes):
            raise ValueError("invalid ndarray field types")
        if len(shape) > MAX_ARRAY_DIMENSIONS:
            raise ValueError("ndarray has too many dimensions")
        if any(not isinstance(item, int) or isinstance(item, bool) or item < 0 for item in shape):
            raise ValueError("invalid ndarray shape")
        dtype = np.dtype(dtype_text)
        if (
            dtype.hasobject
            or dtype.fields is not None
            or dtype.subdtype is not None
            or dtype.kind not in "?biufcmM"
        ):
            raise ValueError("unsafe ndarray dtype")
        element_count = 1
        for item in shape:
            element_count *= item
            if element_count * dtype.itemsize > MAX_FRAME_BYTES:
                raise ValueError("ndarray exceeds the wire size limit")
        if element_count * dtype.itemsize != len(data):
            raise ValueError("ndarray byte count does not match shape and dtype")
        return np.frombuffer(data, dtype=dtype).reshape(shape).copy()
    except (TypeError, ValueError, msgpack.UnpackException) as exc:
        raise fault(ErrorCode.FRAME_DECODE_FAILED, "invalid NumPy array in binary frame") from exc


def pack_message(value: Any, *, max_bytes: int = MAX_FRAME_BYTES) -> bytes:
    if not 0 < max_bytes <= MAX_FRAME_BYTES:
        raise ValueError("max_bytes must be between 1 and 64 MiB")
    try:
        packed = msgpack.packb(value, default=_encode_extension, use_bin_type=True, strict_types=True)
    except (TypeError, ValueError, OverflowError) as exc:
        raise fault(ErrorCode.FRAME_DECODE_FAILED, "message cannot be encoded as safe MessagePack") from exc
    if len(packed) > max_bytes:
        raise fault(ErrorCode.FRAME_TOO_LARGE, "binary frame exceeds the configured size limit")
    return packed


def unpack_message(frame: Any, *, max_bytes: int = MAX_FRAME_BYTES) -> Any:
    if not 0 < max_bytes <= MAX_FRAME_BYTES:
        raise ValueError("max_bytes must be between 1 and 64 MiB")
    if not isinstance(frame, bytes):
        raise fault(ErrorCode.FRAME_TYPE_REJECTED, "inference transport accepts binary frames only")
    if len(frame) > max_bytes:
        raise fault(ErrorCode.FRAME_TOO_LARGE, "binary frame exceeds the configured size limit")
    try:
        return msgpack.unpackb(
            frame,
            raw=False,
            strict_map_key=True,
            ext_hook=_decode_extension,
        )
    except RuntimeError as exc:
        # msgpack may wrap an exception raised by ext_hook.
        if exc.__cause__ is not None:
            raise fault(ErrorCode.FRAME_DECODE_FAILED, "binary frame contains an invalid extension") from exc
        raise
    except (ValueError, TypeError, msgpack.UnpackException) as exc:
        raise fault(ErrorCode.FRAME_DECODE_FAILED, "binary frame is not valid MessagePack") from exc
