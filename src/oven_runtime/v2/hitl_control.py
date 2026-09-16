"""Policy-only right-arm HITL control, independent of a concrete ROS graph."""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum
import logging
from threading import RLock
from typing import Any, Protocol

import numpy as np

from oven_runtime.common.errors import ErrorCode, fault
from oven_runtime.edge.action_execution import ActionExecutionCore
from oven_runtime.edge.contracts import PublishResult
from oven_runtime.edge.joint_gate import OnlineJointGate
from oven_runtime.edge.profile_types import RosBridgeConfig
from oven_runtime.v2.profile import HitlV2Profile


_LOGGER = logging.getLogger(__name__)


class HitlTransport(Protocol):
    """Small injectable boundary for the public piper_hitl ROS contract."""

    def preflight(self) -> None: ...

    def set_policy_enabled(self, enabled: bool) -> None: ...

    def start_reset(self) -> int: ...

    def wait_for_reset_terminal(self, attempt_id: int, timeout_sec: float) -> str: ...

    def wait_for_state(
        self, expected: str, timeout_sec: float, *, pending_states: frozenset[str]
    ) -> None: ...

    def advance_policy_generation(self) -> int: ...

    def wait_for_policy_prime(self, timeout_sec: float) -> None: ...

    def start_policy_lease(
        self, generation: int, *, interval_sec: float, progress_timeout_sec: float
    ) -> None: ...

    def stop_policy_lease(self) -> None: ...

    def ensure_policy_lease_healthy(self) -> None: ...

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
        prime_ack_timeout_sec: float,
        policy_lease_interval_sec: float,
        policy_progress_timeout_sec: float,
        policy_state_timeout_sec: float,
        reset_state_timeout_sec: float,
        manual_takeover_enabled: bool = False,
        execution: ActionExecution | None = None,
    ) -> None:
        if min(
            prime_ack_timeout_sec,
            policy_lease_interval_sec,
            policy_progress_timeout_sec,
            policy_state_timeout_sec,
            reset_state_timeout_sec,
        ) <= 0:
            raise ValueError("HITL state timeouts must be positive")
        config.validate()
        self._config = config
        self._transport = transport
        self._prime_ack_timeout_sec = prime_ack_timeout_sec
        self._policy_lease_interval_sec = policy_lease_interval_sec
        self._policy_progress_timeout_sec = policy_progress_timeout_sec
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
        self._policy_armed = False
        self._policy_generation: int | None = None
        self._takeover_lock = RLock()
        self._closed = False
        # RosHitlTransport implements this optional edge-triggered callback.
        # Keeping it capability-based preserves the pure-Python test boundary.
        handler_setter = getattr(self._transport, "set_physical_takeover_handler", None)
        if handler_setter is not None:
            handler_setter(self._on_physical_takeover)

    def preflight(self) -> None:
        self._require_open()
        self._execution.preflight()
        self._transport.preflight()

    def reset(self, skill: str) -> None:
        self._require_open()
        if self._policy_active or self._policy_armed:
            raise fault(ErrorCode.STATE_REJECTED, "cannot reset while HITL POLICY ownership is active")
        if self._config.skill_arms[skill] != "right":
            raise ValueError("RightHitlControlAdapter only accepts a right-arm binding")
        self.preflight()
        attempt_id = self._transport.start_reset()
        outcome = self._transport.wait_for_reset_terminal(
            attempt_id, self._reset_state_timeout_sec
        )
        if outcome == 'RESET_COMPLETE':
            return
        if outcome == 'RESET_INCOMPLETE':
            raise fault(
                ErrorCode.RESET_INCOMPLETE,
                'HITL reset incomplete; retry execute to continue reset from current pose',
                attempt_id=attempt_id,
            )
        raise RuntimeError(f'HITL reset attempt {attempt_id} returned unexpected terminal outcome {outcome}')

    def begin_stage(self, skill: str) -> None:
        self._require_open()
        if self._config.skill_arms[skill] != "right":
            raise ValueError("RightHitlControlAdapter only accepts a right-arm binding")
        if self._policy_active or self._policy_armed:
            raise fault(ErrorCode.STATE_REJECTED, "HITL policy stage is already active or armed")
        self.preflight()
        self._policy_generation = self._transport.advance_policy_generation()
        self._execution.begin_stage(skill)
        self._policy_armed = True

    def rollout_generation(self) -> int:
        self._require_open()
        return self._execution.rollout_generation()

    def ensure_rollout_generation(self, generation: int) -> None:
        self._require_open()
        self._execution.ensure_rollout_generation(generation)

    def publish(self, actions: Any) -> PublishResult:
        self._require_open()
        if not self._policy_active:
            self._prime_and_enable_policy(actions)
        self._transport.ensure_policy_lease_healthy()
        result = self._execution.publish(actions)
        self._transport.ensure_policy_lease_healthy()
        return result

    def stop(self, reason: str) -> None:
        self._require_open()
        del reason
        was_policy_active = self._policy_active
        was_policy_armed = self._policy_armed
        self._policy_active = False
        self._policy_armed = False
        self._transport.stop_policy_lease()
        self._execution.deactivate()
        if not was_policy_active and not was_policy_armed:
            return
        self._policy_generation = self._transport.advance_policy_generation()
        if was_policy_active:
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
            self._cancel_policy_ownership()
            self._transport.set_policy_enabled(False)
            self._transport.wait_for_state(
                "HOLD", self._policy_state_timeout_sec, pending_states=frozenset({"POLICY"})
            )
            self._transport.request_manual_takeover()
            self._transport.wait_for_state(
                "WAIT_TEACH", self._policy_state_timeout_sec, pending_states=frozenset({"HOLD"})
            )

    def _on_physical_takeover(self, generation: int) -> None:
        """Cancel the producer after Piper has locally fenced physical Teach.

        Piper publishes this only for ``POLICY -> WAIT_TEACH`` and includes the
        active Piper generation.  The matching check makes delayed events unable
        to cancel a newer rollout.  Unlike explicit takeover, Piper is already
        command-silent and Teach is already active, so this path must not call
        policy-disable or the manual-takeover service again.
        """

        if self._closed:
            return
        with self._takeover_lock:
            if (
                not self._manual_takeover_enabled
                or not self._policy_active
                or self._policy_generation != generation
            ):
                return
            self._cancel_policy_ownership()

    def _cancel_policy_ownership(self) -> None:
        """Use the one generation-fenced cancellation sequence for both triggers."""

        self._transport.stop_policy_lease()
        self._execution.cancel_active_rollout()
        self._policy_generation = self._transport.advance_policy_generation()
        self._policy_active = False
        self._policy_armed = False

    def close(self) -> None:
        if self._closed:
            return
        try:
            if self._policy_active or self._policy_armed:
                self.stop("close")
        except Exception as error:
            _LOGGER.warning("HITL close cleanup failed while stopping policy: %s", error)
        finally:
            try:
                self._transport.stop_policy_lease()
            except Exception as error:
                _LOGGER.warning("HITL close cleanup failed while stopping lease: %s", error)
            try:
                self._execution.close()
            except Exception as error:
                _LOGGER.warning("HITL close cleanup failed while closing execution: %s", error)
            try:
                self._transport.close()
            except Exception as error:
                _LOGGER.warning("HITL close cleanup failed while closing transport: %s", error)
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

    def _prime_and_enable_policy(self, actions: Any) -> None:
        """Prime Piper with a real row, receive its ACK, then grant POLICY ownership."""

        if not self._policy_armed or self._policy_generation is None:
            raise fault(ErrorCode.STATE_REJECTED, "HITL policy publication requires an armed stage")
        rows = np.asarray(actions, dtype=np.float64)
        if rows.ndim != 2 or rows.shape[0] == 0 or rows.shape[1:] != (7,) or not np.isfinite(rows).all():
            raise fault(ErrorCode.ACTION_INVALID, "HITL policy priming requires finite seven-joint actions")
        rollout_generation = self._execution.rollout_generation()
        self._execution.ensure_rollout_generation(rollout_generation)
        self._transport.publish_policy(rows[0], self._policy_generation)
        try:
            self._transport.wait_for_policy_prime(self._prime_ack_timeout_sec)
            self._execution.ensure_rollout_generation(rollout_generation)
            self._transport.set_policy_enabled(True)
            self._transport.wait_for_state(
                "POLICY", self._policy_state_timeout_sec, pending_states=frozenset({"HOLD"})
            )
            self._transport.start_policy_lease(
                self._policy_generation,
                interval_sec=self._policy_lease_interval_sec,
                progress_timeout_sec=self._policy_progress_timeout_sec,
            )
        except Exception:
            # An ACK is read-only; an error here leaves POLICY ownership unavailable.
            # A best-effort explicit disable also covers an uncertain service response.
            try:
                self._transport.stop_policy_lease()
            except Exception:
                pass
            try:
                self._transport.set_policy_enabled(False)
            except Exception:
                pass
            self._execution.deactivate()
            self._policy_generation = self._transport.advance_policy_generation()
            self._policy_armed = False
            self._policy_active = False
            raise
        self._policy_active = True
        self._policy_armed = False

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("RightHitlControlAdapter is closed")
