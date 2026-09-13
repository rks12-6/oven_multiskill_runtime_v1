"""Control-transport boundary for HITL v2.

This module is deliberately ROS-free.  The default adapter keeps the mature
``RosActionExecutor`` as the implementation of direct robot control while
giving future control modes one explicit, narrow replacement boundary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from oven_runtime.edge.contracts import PublishResult

if TYPE_CHECKING:
    from oven_runtime.edge.ros2_action import RosActionExecutor


class ControlAdapter(Protocol):
    """The execution lifecycle exposed to the pipeline."""

    def preflight(self) -> None: ...

    def reset(self, skill: str) -> None: ...

    def begin_stage(self, skill: str) -> None: ...

    def rollout_generation(self) -> int: ...

    def ensure_rollout_generation(self, generation: int) -> None: ...

    def publish(self, actions: Any) -> PublishResult: ...

    def stop(self, reason: str) -> None: ...

    def close(self) -> None: ...


class DirectControlAdapter:
    """Transparent adapter for the mature direct ROS action executor."""

    def __init__(self, executor: RosActionExecutor) -> None:
        self._executor = executor

    def preflight(self) -> None:
        self._executor.preflight()

    def reset(self, skill: str) -> None:
        self._executor.reset(skill)

    def begin_stage(self, skill: str) -> None:
        self._executor.begin_stage(skill)

    def rollout_generation(self) -> int:
        return self._executor.rollout_generation()

    def ensure_rollout_generation(self, generation: int) -> None:
        self._executor.ensure_rollout_generation(generation)

    def publish(self, actions: Any) -> PublishResult:
        return self._executor.publish(actions)

    def stop(self, reason: str) -> None:
        self._executor.stop(reason)

    def close(self) -> None:
        self._executor.close()
