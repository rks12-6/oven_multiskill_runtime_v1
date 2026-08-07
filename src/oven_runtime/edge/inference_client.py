from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from websockets.sync.client import connect

from oven_runtime.common.errors import ErrorCode, RuntimeFault, fault
from oven_runtime.common.hashing import stable_tree_hash
from oven_runtime.common.protocol import PROTOCOL_VERSION, InferenceRequest, RequestKind
from oven_runtime.common.wire import MAX_FRAME_BYTES, pack_message, unpack_message

REQUIRED_RESPONSE_FIELDS = frozenset(
    {
        "protocol_version",
        "message_type",
        "session_id",
        "trial_id",
        "skill",
        "generation",
        "sequence",
        "publishable",
        "request_hash",
        "observation_hash",
        "noise_hash",
        "action_hash",
        "actions",
    }
)


class InferenceClient:
    """One-shot client. It intentionally has no automatic retry path."""

    def __init__(self, uri: str = "ws://127.0.0.1:19110", *, timeout_sec: float = 10.0) -> None:
        if not uri.startswith("ws://127.0.0.1:") and not uri.startswith("ws://[::1]:"):
            raise ValueError("inference client URI must target a local SSH-tunnel endpoint")
        if timeout_sec <= 0:
            raise ValueError("timeout_sec must be positive")
        self.uri = uri
        self.timeout_sec = timeout_sec

    def infer(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = InferenceRequest.parse(payload)
        frame = pack_message(payload)
        try:
            with connect(
                self.uri,
                open_timeout=self.timeout_sec,
                close_timeout=self.timeout_sec,
                max_size=MAX_FRAME_BYTES,
                compression=None,
            ) as websocket:
                websocket.send(frame)
                received = websocket.recv(timeout=self.timeout_sec)
        except Exception as exc:
            raise fault(
                ErrorCode.INFERENCE_TIMEOUT,
                "inference transport failed; request outcome is ambiguous and must not be retried automatically",
            ) from exc
        response = unpack_message(received)
        return validate_inference_response(request, response)


def validate_inference_response(request: InferenceRequest, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise fault(ErrorCode.FRAME_DECODE_FAILED, "inference response must be a mapping")
    if payload.get("message_type") == "inference_error":
        code_text = payload.get("error_code")
        try:
            code = ErrorCode(code_text)
        except (TypeError, ValueError):
            code = ErrorCode.SERVER_FAILED
        message = payload.get("message")
        if not isinstance(message, str):
            message = "remote inference failed"
        raise RuntimeFault(code, message)
    missing = sorted(REQUIRED_RESPONSE_FIELDS - set(payload))
    unexpected = sorted(set(payload) - REQUIRED_RESPONSE_FIELDS)
    if missing:
        raise fault(ErrorCode.MISSING_REQUIRED_FIELD, "inference response is missing required fields", fields=missing)
    if unexpected:
        raise fault(ErrorCode.UNEXPECTED_FIELD, "inference response contains unexpected fields", fields=unexpected)
    expected_values = {
        "protocol_version": PROTOCOL_VERSION,
        "message_type": "inference_response",
        "session_id": request.session_id,
        "trial_id": request.trial_id,
        "skill": request.expected_skill,
        "generation": request.expected_generation,
        "sequence": request.sequence,
        "publishable": request.request_kind is RequestKind.INFER,
    }
    mismatched = sorted(name for name, expected in expected_values.items() if payload[name] != expected)
    if mismatched:
        raise fault(ErrorCode.RESPONSE_MISMATCH, "inference response identity does not match its request", fields=mismatched)
    observation_hash = stable_tree_hash(request.observation)
    request_hash = stable_tree_hash(
        {
            "protocol_version": request.protocol_version,
            "session_id": request.session_id,
            "trial_id": request.trial_id,
            "request_kind": request.request_kind.value,
            "expected_skill": request.expected_skill,
            "expected_generation": request.expected_generation,
            "sequence": request.sequence,
            "observation_hash": observation_hash,
        }
    )
    hashes = {
        "observation_hash": observation_hash,
        "request_hash": request_hash,
        "action_hash": stable_tree_hash(payload["actions"]),
    }
    mismatched_hashes = sorted(name for name, expected in hashes.items() if payload[name] != expected)
    if mismatched_hashes:
        raise fault(ErrorCode.RESPONSE_MISMATCH, "inference response hash verification failed", fields=mismatched_hashes)
    for name in ("noise_hash", "request_hash", "observation_hash", "action_hash"):
        if not isinstance(payload[name], str) or len(payload[name]) != 64:
            raise fault(ErrorCode.INVALID_FIELD_VALUE, "inference response hash is invalid", field=name)
    return dict(payload)
