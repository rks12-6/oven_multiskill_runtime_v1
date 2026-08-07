from __future__ import annotations

import unittest
from uuid import uuid4

from oven_runtime.common.errors import ErrorCode, RuntimeFault
from oven_runtime.common.protocol import InferenceRequest, REQUIRED_REQUEST_FIELDS


def valid_payload() -> dict[str, object]:
    return {
        "protocol_version": 1,
        "message_type": "inference_request",
        "session_id": str(uuid4()),
        "session_token": "x" * 43,
        "trial_id": "run_001_open_door_attempt_1",
        "request_kind": "warmup",
        "expected_skill": "open_door",
        "expected_generation": 1,
        "sequence": 0,
        "sent_at_monotonic_ns": 123,
        "observation": {"state": [0.0] * 7},
    }


class InferenceRequestTests(unittest.TestCase):
    def test_valid_request_parses(self) -> None:
        request = InferenceRequest.parse(valid_payload())
        self.assertEqual(request.expected_skill, "open_door")
        self.assertEqual(request.sequence, 0)

    def test_bare_observation_is_rejected(self) -> None:
        with self.assertRaises(RuntimeFault) as raised:
            InferenceRequest.parse({"state": [0.0]})
        self.assertEqual(raised.exception.code, ErrorCode.MISSING_REQUIRED_FIELD)

    def test_every_required_field_is_actually_required(self) -> None:
        for field in REQUIRED_REQUEST_FIELDS:
            with self.subTest(field=field):
                payload = valid_payload()
                del payload[field]
                with self.assertRaises(RuntimeFault) as raised:
                    InferenceRequest.parse(payload)
                self.assertEqual(raised.exception.code, ErrorCode.MISSING_REQUIRED_FIELD)

    def test_unexpected_field_is_rejected(self) -> None:
        payload = valid_payload()
        payload["legacy_compatibility"] = True
        with self.assertRaises(RuntimeFault) as raised:
            InferenceRequest.parse(payload)
        self.assertEqual(raised.exception.code, ErrorCode.UNEXPECTED_FIELD)

    def test_bool_is_not_accepted_as_integer(self) -> None:
        for field in ("protocol_version", "expected_generation", "sequence", "sent_at_monotonic_ns"):
            with self.subTest(field=field):
                payload = valid_payload()
                payload[field] = True
                with self.assertRaises(RuntimeFault) as raised:
                    InferenceRequest.parse(payload)
                self.assertEqual(raised.exception.code, ErrorCode.INVALID_FIELD_TYPE)


if __name__ == "__main__":
    unittest.main()

