from __future__ import annotations

import unittest

import msgpack
import numpy as np

from oven_runtime.common.errors import ErrorCode, RuntimeFault
from oven_runtime.common.wire import NDARRAY_EXT_CODE, pack_message, unpack_message


class WireCodecTests(unittest.TestCase):
    def test_numpy_round_trip_preserves_dtype_shape_and_values(self) -> None:
        original = {
            "image": np.arange(24, dtype=np.uint8).reshape(2, 3, 4),
            "state": np.asarray([1.5, -2.0], dtype=np.float32),
            "scalar": np.float64(3.25),
        }
        restored = unpack_message(pack_message(original))
        for name in original:
            expected = np.asarray(original[name])
            actual = restored[name]
            self.assertIsInstance(actual, np.ndarray)
            self.assertEqual(actual.dtype, expected.dtype)
            self.assertEqual(actual.shape, expected.shape)
            np.testing.assert_array_equal(actual, expected)

    def test_text_frame_is_rejected(self) -> None:
        with self.assertRaises(RuntimeFault) as raised:
            unpack_message("not binary")
        self.assertEqual(raised.exception.code, ErrorCode.FRAME_TYPE_REJECTED)

    def test_frame_limit_is_enforced_in_both_directions(self) -> None:
        with self.assertRaises(RuntimeFault) as encoded:
            pack_message({"payload": b"x" * 100}, max_bytes=32)
        self.assertEqual(encoded.exception.code, ErrorCode.FRAME_TOO_LARGE)
        with self.assertRaises(RuntimeFault) as decoded:
            unpack_message(b"x" * 33, max_bytes=32)
        self.assertEqual(decoded.exception.code, ErrorCode.FRAME_TOO_LARGE)

    def test_malformed_numpy_extension_is_rejected(self) -> None:
        body = msgpack.packb(
            {"dtype": "<f4", "shape": [4], "data": b"too short"},
            use_bin_type=True,
        )
        frame = msgpack.packb(msgpack.ExtType(NDARRAY_EXT_CODE, body), use_bin_type=True)
        with self.assertRaises(RuntimeFault) as raised:
            unpack_message(frame)
        self.assertEqual(raised.exception.code, ErrorCode.FRAME_DECODE_FAILED)

    def test_object_array_cannot_be_encoded(self) -> None:
        with self.assertRaises(RuntimeFault) as raised:
            pack_message({"unsafe": np.asarray([object()], dtype=object)})
        self.assertEqual(raised.exception.code, ErrorCode.FRAME_DECODE_FAILED)

    def test_unknown_extension_is_rejected(self) -> None:
        frame = msgpack.packb(msgpack.ExtType(7, b"untrusted"), use_bin_type=True)
        with self.assertRaises(RuntimeFault) as raised:
            unpack_message(frame)
        self.assertEqual(raised.exception.code, ErrorCode.FRAME_DECODE_FAILED)


if __name__ == "__main__":
    unittest.main()
