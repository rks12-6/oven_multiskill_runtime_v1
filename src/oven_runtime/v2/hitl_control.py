"""Policy-only right-arm HITL control, independent of a concrete ROS graph."""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum
from threading import RLock
from typing import Any, Protocol

import numpy as np

from oven_runtime.common.errors import ErrorCode, fault
from oven_runtime.edge.action_execution import ActionExecutionCore
from oven_runtime.edge.contracts import PublishResult
from oven_runtime.edge.joint_gate import OnlineJointGate
from oven_runtime.edge.profile_types import RosBridgeConfig
from oven_runtime.v2.profile import HitlV2Profile


class HitlTransport(Protocol):
    """Small injectable boundary for the public piper_hitl ROS contract."""

    def preflight(self) -> None: ...

    def set_policy_enabled(self, enabled: bool) -> None: ...

    def start_reset(self) -> None: ...

    def wait_for_state(
        self, expected: str, timeout_sec: float, *, pending_states: frozenset[str]
    ) -> None: ...

    def advance_policy_generation(self) -> int: ...

    def publish_policy(self, positions: np.ndarray, generation: int) -> None: ...

    def publish_other_front_hold(self, positions: np.ndarray) -> None: ...

    def request_manual_takeover(self) -> None: ...

    def close(self) -> None: ...


class ActionExecution(Protocol):
    def preflight(self) -> None: ...

    def begin_stage(self, skill: str) -> None: ...

    def publish(self, actions: Any) -> PublishResult: ...

    def deactivate(self) -> None: ...

    def cancel_active_rollout(self) -> int: ...

    def rollout_generation(self) -> int: ...

    def ensure_rollout_generation(self, generation: int) -> None: ...

    def close(self) -> None: ...


class ControlMode(str, Enum):
    DIRECT = "DIRECT"
    RIGHT_HITL = "RIGHT_HITL"


def control_mode_for_skills(profile: HitlV2Profile, skills: Iterable[str]) -> ControlMode:
    """Choose from bindings, never from a mature skill-name special case."""

    modes: set[ControlMode] = set()
    for skill in skills:
        binding = profile.binding(skill)
        arm_pair = profile.arm_pair(binding.arm_pair)
        modes.add(ControlMode.RIGHT_HITL if binding.allow_hitl and arm_pair.hitl_enabled else ControlMode.DIRECT)
    if len(modes) != 1:
        raise ValueError("a mixed direct/HITL plan requires a future multi-adapter router")
    return modes.pop()


def adapter_for_skill(
    profile: HitlV2Profile, skill: str, *, direct_adapter: Any, right_hitl_adapter: Any
) -> Any:
    """Select an already-constructed adapter from the v2 binding overlay."""

    binding = profile.binding(skill)
    arm_pair = profile.arm_pair(binding.arm_pair)
    if binding.allow_hitl and arm_pair.hitl_enabled:
        return right_hitl_adapter
    return direct_adapter


class RightHitlControlAdapter:
    """Routes mature right-arm policy semantics only through the HITL arbiter."""

    def __init__(
        self,
        config: RosBridgeConfig,
        observation: Any,
        online_gate: OnlineJointGate,
        transport: HitlTransport,
        *,
        policy_state_timeout_sec: float,
        reset_state_timeout_sec: float,
        manual_takeover_enabled: bool = False,
        execution: ActionExecution | None = None,
    ) -> None:
        if policy_state_timeout_sec <= 0 or reset_state_timeout_sec <= 0:
            raise ValueError("HITL state timeouts must be positive")
        config.validate()
        self._config = config
        self._transport = transport
        self._policy_state_timeout_sec = policy_state_timeout_sec
        self._reset_state_timeout_sec = reset_state_timeout_sec
        self._manual_takeover_enabled = manual_takeover_enabled
        self._execution: ActionExecution = execution or ActionExecutionCore(
            config,
            observation,
            online_gate,
            publish_controlled=self._publish_policy_only,
        )
        self._policy_active = False
        self._policy_generation: int | None = None
        self._takeover_lock = RLock()
        self._closed = False

    def preflight(self) -> None:
        self._require_open()
        self._execution.preflight()
        self._transport.preflight()

    def reset(self, skill: str) -> None:
        self._require_open()
        if self._policy_active:
            raise fault(ErrorCode.STATE_REJECTED, "cannot reset while HITL POLICY ownership is active")
        if self._config.skill_arms[skill] != "right":
            raise ValueError("RightHitlControlAdapter only accepts a right-arm binding")
        self.preflight()
        self._transport.start_reset()
        self._transport.wait_for_state(
            "RESETTING", self._reset_state_timeout_sec, pending_states=frozenset({"HOLD"})
        )
        self._transport.wait_for_state(
            "HOLD", self._reset_state_timeout_sec, pending_states=frozenset({"RESETTING"})
        )

    def begin_stage(self, skill: str) -> None:
        self._require_open()
        if self._config.skill_arms[skill] != "right":
            raise ValueError("RightHitlControlAdapter only accepts a right-arm binding")
        self.preflight()
        self._policy_generation = self._transport.advance_policy_generation()
        self._transport.set_policy_enabled(True)
        try:
            self._transport.wait_for_state(
                "POLICY", self._policy_state_timeout_sec, pending_states=frozenset({"HOLD"})
            )
        except Exception:
            # State observation can time out after the service reached the
            # arbiter; revoke ownership before exposing that failure.
            self._transport.set_policy_enabled(False)
            raise
        self._execution.begin_stage(skill)
        self._policy_active = True

    def rollout_generation(self) -> int:
        self._require_open()
        return self._execution.rollout_generation()

    def ensure_rollout_generation(self, generation: int) -> None:
        self._require_open()
        self._execution.ensure_rollout_generation(generation)

    def publish(self, actions: Any) -> PublishResult:
        self._require_open()
        if not self._policy_active:
            raise fault(ErrorCode.STATE_REJECTED, "HITL policy publication requires a confirmed POLICY stage")
        return self._execution.publish(actions)

    def stop(self, reason: str) -> None:
        self._require_open()
        self._execution.deactivate()
        if not self._policy_active:
            return
        self._policy_active = False
        self._policy_generation = self._transport.advance_policy_generation()
        self._transport.set_policy_enabled(False)
        self._transport.wait_for_state(
            "HOLD", self._policy_state_timeout_sec, pending_states=frozenset({"POLICY"})
        )

    def request_manual_takeover(self) -> None:
        """Cancel policy production before the arbiter is allowed to enter WAIT_TEACH."""

        self._require_open()
        if not self._manual_takeover_enabled:
            raise fault(ErrorCode.STATE_REJECTED, "manual takeover is unavailable for this HITL mode")
        with self._takeover_lock:
            if not self._policy_active:
                raise fault(ErrorCode.STATE_REJECTED, "manual takeover requires active HITL POLICY")
            self._execution.cancel_active_rollout()
            self._policy_generation = self._transport.advance_policy_generation()
            self._policy_active = False
            self._transport.set_policy_enabled(False)
            self._transport.wait_for_state(
                "HOLD", self._policy_state_timeout_sec, pending_states=frozenset({"POLICY"})
            )
            self._transport.request_manual_takeover()
            self._transport.wait_for_state(
                "WAIT_TEACH", self._policy_state_timeout_sec, pending_states=frozenset({"HOLD"})
            )

    def close(self) -> None:
        if self._closed:
            return
        if self._policy_active:
            self.stop("close")
        self._execution.close()
        self._transport.close()
        self._closed = True

    def _publish_policy_only(self, arm: str, command: np.ndarray, other_hold: np.ndarray) -> None:
        if arm != "right":
            raise ValueError("RightHitlControlAdapter cannot publish a non-right command")
        if not self._policy_active:
            raise fault(ErrorCode.STATE_REJECTED, "HITL policy publication is not active")
        if self._policy_generation is None:
            raise RuntimeError("HITL policy generation has not been prepared")
        self._transport.publish_policy(command, self._policy_generation)
        self._transport.publish_other_front_hold(other_hold)

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("RightHitlControlAdapter is closed")
