from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any
from uuid import UUID

from oven_runtime.common.errors import ErrorCode, fault

PROTOCOL_VERSION = 1
TRIAL_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


class RequestKind(str, Enum):
    INFER = "infer"


REQUIRED_REQUEST_FIELDS = frozenset(
    {
        "protocol_version",
        "message_type",
        "session_id",
        "session_token",
        "trial_id",
        "request_kind",
        "expected_skill",
        "expected_generation",
        "sequence",
        "sent_at_monotonic_ns",
        "observation",
    }
)


def _exact_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _required_string(payload: Mapping[str, Any], name: str) -> str:
    value = payload[name]
    if not isinstance(value, str):
        raise fault(ErrorCode.INVALID_FIELD_TYPE, "request field must be a string", field=name)
    if not value:
        raise fault(ErrorCode.INVALID_FIELD_VALUE, "request field must not be empty", field=name)
    return value


@dataclass(frozen=True)
class InferenceRequest:
    protocol_version: int
    session_id: str
    session_token: str
    trial_id: str
    request_kind: RequestKind
    expected_skill: str
    expected_generation: int
    sequence: int
    sent_at_monotonic_ns: int
    observation: dict[str, Any]

    @classmethod
    def parse(cls, payload: Any) -> InferenceRequest:
        if not isinstance(payload, Mapping):
            raise fault(ErrorCode.INVALID_FIELD_TYPE, "inference request must be a mapping")
        fields = set(payload)
        missing = sorted(REQUIRED_REQUEST_FIELDS - fields)
        if missing:
            raise fault(ErrorCode.MISSING_REQUIRED_FIELD, "inference request is missing required fields", fields=missing)
        unexpected = sorted(fields - REQUIRED_REQUEST_FIELDS)
        if unexpected:
            raise fault(ErrorCode.UNEXPECTED_FIELD, "inference request contains unexpected fields", fields=unexpected)

        version = payload["protocol_version"]
        if not _exact_int(version):
            raise fault(ErrorCode.INVALID_FIELD_TYPE, "protocol_version must be an integer")
        if version != PROTOCOL_VERSION:
            raise fault(
                ErrorCode.PROTOCOL_VERSION_UNSUPPORTED,
                "unsupported protocol version",
                received=version,
                supported=PROTOCOL_VERSION,
            )
        if payload["message_type"] != "inference_request":
            raise fault(ErrorCode.INVALID_MESSAGE_TYPE, "message_type must be inference_request")

        session_id = _required_string(payload, "session_id")
        try:
            UUID(session_id)
        except ValueError as exc:
            raise fault(ErrorCode.INVALID_FIELD_VALUE, "session_id must be a UUID") from exc
        session_token = _required_string(payload, "session_token")
        if not 32 <= len(session_token) <= 128:
            raise fault(ErrorCode.INVALID_FIELD_VALUE, "session_token length is invalid")
        trial_id = _required_string(payload, "trial_id")
        if TRIAL_ID_PATTERN.fullmatch(trial_id) is None:
            raise fault(ErrorCode.INVALID_FIELD_VALUE, "trial_id format is invalid")
        try:
            request_kind = RequestKind(payload["request_kind"])
        except (TypeError, ValueError) as exc:
            raise fault(ErrorCode.INVALID_FIELD_VALUE, "request_kind is invalid") from exc
        expected_skill = _required_string(payload, "expected_skill")

        generation = payload["expected_generation"]
        sequence = payload["sequence"]
        sent_at = payload["sent_at_monotonic_ns"]
        for name, value, minimum in (
            ("expected_generation", generation, 1),
            ("sequence", sequence, 0),
            ("sent_at_monotonic_ns", sent_at, 0),
        ):
            if not _exact_int(value):
                raise fault(ErrorCode.INVALID_FIELD_TYPE, "request field must be an integer", field=name)
            if value < minimum:
                raise fault(ErrorCode.INVALID_FIELD_VALUE, "request integer is below minimum", field=name, minimum=minimum)
        observation = payload["observation"]
        if not isinstance(observation, dict):
            raise fault(ErrorCode.INVALID_FIELD_TYPE, "observation must be a dictionary")

        return cls(
            protocol_version=version,
            session_id=session_id,
            session_token=session_token,
            trial_id=trial_id,
            request_kind=request_kind,
            expected_skill=expected_skill,
            expected_generation=generation,
            sequence=sequence,
            sent_at_monotonic_ns=sent_at,
            observation=observation,
        )


@dataclass(frozen=True)
class InferenceResponse:
    session_id: str
    trial_id: str
    skill: str
    generation: int
    sequence: int
    publishable: bool
    request_hash: str
    observation_hash: str
    noise_hash: str
    action_hash: str
    actions: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "message_type": "inference_response",
            "session_id": self.session_id,
            "trial_id": self.trial_id,
            "skill": self.skill,
            "generation": self.generation,
            "sequence": self.sequence,
            "publishable": self.publishable,
            "request_hash": self.request_hash,
            "observation_hash": self.observation_hash,
            "noise_hash": self.noise_hash,
            "action_hash": self.action_hash,
            "actions": self.actions,
        }
