from __future__ import annotations

import time
from typing import Any

from oven_runtime.common.protocol import PROTOCOL_VERSION


def request_payload(
    session: dict[str, Any],
    *,
    kind: str,
    sequence: int,
    skill: str | None = None,
    generation: int | None = None,
    observation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "message_type": "inference_request",
        "session_id": session["session_id"],
        "session_token": session["session_token"],
        "trial_id": session["trial_id"],
        "request_kind": kind,
        "expected_skill": session["skill"] if skill is None else skill,
        "expected_generation": session["generation"] if generation is None else generation,
        "sequence": sequence,
        "sent_at_monotonic_ns": time.monotonic_ns(),
        "observation": {"state": [0.0] * 7} if observation is None else observation,
    }

