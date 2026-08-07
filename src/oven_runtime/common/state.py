from __future__ import annotations

from enum import Enum

from oven_runtime.common.errors import ErrorCode, fault


class RuntimeState(str, Enum):
    STARTING = "STARTING"
    LOADING = "LOADING"
    WARMUP_REQUIRED = "WARMUP_REQUIRED"
    PRNG_RESET_REQUIRED = "PRNG_RESET_REQUIRED"
    READY = "READY"
    DRAINING = "DRAINING"
    SWITCHING = "SWITCHING"
    FAILED = "FAILED"
    STOPPING = "STOPPING"


class SessionState(str, Enum):
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"
    EXPIRED = "EXPIRED"
    ABORTED = "ABORTED"


class PipelineState(str, Enum):
    CREATED = "CREATED"
    PREFLIGHT = "PREFLIGHT"
    RESETTING = "RESETTING"
    PREPARING_POLICY = "PREPARING_POLICY"
    WARMING_UP = "WARMING_UP"
    READY_TO_ROLLOUT = "READY_TO_ROLLOUT"
    ROLLOUT = "ROLLOUT"
    JOINT_GATE = "JOINT_GATE"
    CHECKING = "CHECKING"
    HANDOFF = "HANDOFF"
    COMPLETED = "COMPLETED"
    SAFE_STOP = "SAFE_STOP"
    FAILED = "FAILED"


RUNTIME_TRANSITIONS: dict[RuntimeState, frozenset[RuntimeState]] = {
    RuntimeState.STARTING: frozenset({RuntimeState.LOADING, RuntimeState.STOPPING, RuntimeState.FAILED}),
    RuntimeState.LOADING: frozenset({RuntimeState.WARMUP_REQUIRED, RuntimeState.FAILED, RuntimeState.STOPPING}),
    RuntimeState.WARMUP_REQUIRED: frozenset(
        {RuntimeState.PRNG_RESET_REQUIRED, RuntimeState.DRAINING, RuntimeState.FAILED, RuntimeState.STOPPING}
    ),
    RuntimeState.PRNG_RESET_REQUIRED: frozenset(
        {RuntimeState.READY, RuntimeState.DRAINING, RuntimeState.FAILED, RuntimeState.STOPPING}
    ),
    RuntimeState.READY: frozenset({RuntimeState.DRAINING, RuntimeState.FAILED, RuntimeState.STOPPING}),
    RuntimeState.DRAINING: frozenset({RuntimeState.SWITCHING, RuntimeState.FAILED, RuntimeState.STOPPING}),
    RuntimeState.SWITCHING: frozenset({RuntimeState.LOADING, RuntimeState.FAILED, RuntimeState.STOPPING}),
    RuntimeState.FAILED: frozenset({RuntimeState.STOPPING}),
    RuntimeState.STOPPING: frozenset(),
}


def ensure_runtime_transition(current: RuntimeState, target: RuntimeState) -> None:
    if target not in RUNTIME_TRANSITIONS[current]:
        raise fault(
            ErrorCode.STATE_REJECTED,
            "illegal runtime state transition",
            current=current.value,
            target=target.value,
        )

