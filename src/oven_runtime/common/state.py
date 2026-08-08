from __future__ import annotations

from enum import Enum

from oven_runtime.common.errors import ErrorCode, fault


class RuntimeState(str, Enum):
    STARTING = "STARTING"
    LOADING = "LOADING"
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
    RuntimeState.LOADING: frozenset({RuntimeState.READY, RuntimeState.FAILED, RuntimeState.STOPPING}),
    RuntimeState.READY: frozenset({RuntimeState.DRAINING, RuntimeState.FAILED, RuntimeState.STOPPING}),
    RuntimeState.DRAINING: frozenset({RuntimeState.SWITCHING, RuntimeState.FAILED, RuntimeState.STOPPING}),
    RuntimeState.SWITCHING: frozenset({RuntimeState.LOADING, RuntimeState.FAILED, RuntimeState.STOPPING}),
    RuntimeState.FAILED: frozenset({RuntimeState.STOPPING}),
    RuntimeState.STOPPING: frozenset(),
}

PIPELINE_TRANSITIONS: dict[PipelineState, frozenset[PipelineState]] = {
    PipelineState.CREATED: frozenset({PipelineState.PREFLIGHT, PipelineState.SAFE_STOP}),
    PipelineState.PREFLIGHT: frozenset({PipelineState.RESETTING, PipelineState.SAFE_STOP}),
    PipelineState.RESETTING: frozenset({PipelineState.PREPARING_POLICY, PipelineState.SAFE_STOP}),
    PipelineState.PREPARING_POLICY: frozenset({PipelineState.READY_TO_ROLLOUT, PipelineState.SAFE_STOP}),
    PipelineState.READY_TO_ROLLOUT: frozenset({PipelineState.ROLLOUT, PipelineState.SAFE_STOP}),
    PipelineState.ROLLOUT: frozenset({PipelineState.JOINT_GATE, PipelineState.SAFE_STOP}),
    PipelineState.JOINT_GATE: frozenset({PipelineState.CHECKING, PipelineState.SAFE_STOP}),
    PipelineState.CHECKING: frozenset({PipelineState.HANDOFF, PipelineState.SAFE_STOP}),
    PipelineState.HANDOFF: frozenset(
        {PipelineState.RESETTING, PipelineState.COMPLETED, PipelineState.SAFE_STOP}
    ),
    PipelineState.SAFE_STOP: frozenset({PipelineState.FAILED}),
    PipelineState.COMPLETED: frozenset(),
    PipelineState.FAILED: frozenset(),
}


def ensure_runtime_transition(current: RuntimeState, target: RuntimeState) -> None:
    if target not in RUNTIME_TRANSITIONS[current]:
        raise fault(
            ErrorCode.STATE_REJECTED,
            "illegal runtime state transition",
            current=current.value,
            target=target.value,
        )


def ensure_pipeline_transition(current: PipelineState, target: PipelineState) -> None:
    if target not in PIPELINE_TRANSITIONS[current]:
        raise fault(
            ErrorCode.STATE_REJECTED,
            "illegal pipeline state transition",
            current=current.value,
            target=target.value,
        )
