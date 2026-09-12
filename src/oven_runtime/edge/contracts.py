from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class Observation:
    payload: dict[str, Any]
    captured_at_monotonic_ns: int


@dataclass(frozen=True)
class PublishResult:
    stop_requested: bool = False
    reason: str | None = None
    published_rows: int = 0
    gate_result: GateResult | None = None


@dataclass(frozen=True)
class GateResult:
    passed: bool
    samples: int
    max_error: float
    max_spread: float
    reason: str


@dataclass(frozen=True)
class CheckerResult:
    passed: bool
    score: float
    threshold: float
    asset_id: str
    reason: str


class ObservationSource(Protocol):
    def preflight(self) -> None: ...

    def sample(self, skill: str) -> Observation: ...


class ResetController(Protocol):
    def preflight(self) -> None: ...

    def reset(self, skill: str) -> None: ...


class ActionExecutor(Protocol):
    def preflight(self) -> None: ...

    def begin_stage(self, skill: str) -> None: ...

    def publish(self, actions: Any) -> PublishResult: ...

    def stop(self, reason: str) -> None: ...


class JointGate(Protocol):
    def evaluate(self, skill: str) -> GateResult: ...


class VisualChecker(Protocol):
    def evaluate(self, skill: str) -> CheckerResult: ...


class ApprovalGate(Protocol):
    def approve_run(self, run_id: str, skills: tuple[str, ...]) -> bool: ...


class ControlClient(Protocol):
    def call(self, command: str, *arguments: str | int) -> dict[str, Any]: ...


class InferenceTransport(Protocol):
    def infer(self, payload: dict[str, Any]) -> dict[str, Any]: ...
