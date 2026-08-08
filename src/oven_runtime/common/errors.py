from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    PROTOCOL_VERSION_UNSUPPORTED = "PROTOCOL_VERSION_UNSUPPORTED"
    INVALID_MESSAGE_TYPE = "INVALID_MESSAGE_TYPE"
    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
    UNEXPECTED_FIELD = "UNEXPECTED_FIELD"
    INVALID_FIELD_TYPE = "INVALID_FIELD_TYPE"
    INVALID_FIELD_VALUE = "INVALID_FIELD_VALUE"
    FRAME_TOO_LARGE = "FRAME_TOO_LARGE"
    FRAME_TYPE_REJECTED = "FRAME_TYPE_REJECTED"
    FRAME_DECODE_FAILED = "FRAME_DECODE_FAILED"
    RESPONSE_MISMATCH = "RESPONSE_MISMATCH"
    CONTROL_SERVER_UNAVAILABLE = "CONTROL_SERVER_UNAVAILABLE"
    SESSION_REQUIRED = "SESSION_REQUIRED"
    SESSION_INVALID = "SESSION_INVALID"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    SESSION_ALREADY_ACTIVE = "SESSION_ALREADY_ACTIVE"
    TRIAL_ALREADY_ACTIVE = "TRIAL_ALREADY_ACTIVE"
    TRIAL_REQUIRED = "TRIAL_REQUIRED"
    TRIAL_MISMATCH = "TRIAL_MISMATCH"
    SKILL_MISMATCH = "SKILL_MISMATCH"
    GENERATION_MISMATCH = "GENERATION_MISMATCH"
    SEQUENCE_MISMATCH = "SEQUENCE_MISMATCH"
    STATE_REJECTED = "STATE_REJECTED"
    INFERENCE_BUSY = "INFERENCE_BUSY"
    INFERENCE_TIMEOUT = "INFERENCE_TIMEOUT"
    POLICY_FAILED = "POLICY_FAILED"
    ACTION_INVALID = "ACTION_INVALID"
    OBSERVATION_STALE = "OBSERVATION_STALE"
    JOINT_GATE_FAILED = "JOINT_GATE_FAILED"
    CHECKER_FAILED = "CHECKER_FAILED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    PIPELINE_FAILED = "PIPELINE_FAILED"
    AUDIT_COMMIT_FAILED = "AUDIT_COMMIT_FAILED"
    CONFIG_INVALID = "CONFIG_INVALID"
    PATH_OUTSIDE_ROOT = "PATH_OUTSIDE_ROOT"
    SERVER_FAILED = "SERVER_FAILED"


@dataclass(frozen=True)
class RuntimeFault(Exception):
    """A stable, safe-to-return error without internal traceback or secrets."""

    code: ErrorCode
    message: str
    details: dict[str, Any] | None = None

    def __str__(self) -> str:
        return f"{self.code.value}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": False,
            "error_code": self.code.value,
            "message": self.message,
        }
        if self.details:
            result["details"] = dict(self.details)
        return result


def fault(code: ErrorCode, message: str, **details: Any) -> RuntimeFault:
    return RuntimeFault(code, message, details or None)
