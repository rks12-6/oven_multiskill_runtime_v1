"""Transport-independent mature action execution semantics."""

from __future__ import annotations

import fcntl
import math
import os
import time
from collections.abc import Callable
from typing import Any

import numpy as np

from oven_runtime.common.errors import ErrorCode, fault
from oven_runtime.edge.contracts import PublishResult
from oven_runtime.edge.joint_gate import OnlineJointGate
from oven_runtime.edge.profile_types import RosBridgeConfig


class ActionExecutionCore:
    """Mature non-transport lifecycle shared by direct and HITL control."""

    def __init__(
        self,
        config: RosBridgeConfig,
        observation: Any,
        online_gate: OnlineJointGate,
        *,
        publish_controlled: Callable[[str, np.ndarray, np.ndarray], None],
        external_publishers_present: Callable[[], bool] | None = None,
    ) -> None:
        self.config = config
        self.observation = observation
        self.online_gate = online_gate
        self._publish_controlled = publish_controlled
        self._external_publishers_present = external_publishers_present
        self._active_skill: str | None = None
        self._published_rows = 0
        self._last_command: np.ndarray | None = None
        self._has_published = False
        self._preflight_complete = False
        self._lock_stream: Any | None = None
        self._closed = False

    def preflight(self) -> None:
        if self._preflight_complete:
            return
        self.observation.preflight()
        self._acquire_action_lock()
        if self._external_publishers_present is not None and self._external_publishers_present():
            raise fault(ErrorCode.STATE_REJECTED, "another ROS action publisher is present")
        self._preflight_complete = True

    def reset(self, skill: str) -> None:
        self.preflight()
        target = np.asarray(self.config.reset_targets[skill], dtype=np.float64)
        arm = self.config.skill_arms[skill]
        start, other_hold = self.observation.arm_snapshot(arm)
        total_steps = max(2, int(math.ceil(self.config.reset_duration_sec * self.config.publish_hz)))
        period = 1.0 / self.config.publish_hz
        for index in range(1, total_steps + 1):
            fraction = index / total_steps
            smooth = fraction * fraction * (3.0 - 2.0 * fraction)
            self._publish(arm, start + smooth * (target - start), other_hold)
            time.sleep(period)
        deadline = time.monotonic() + self.config.reset_verify_timeout_sec
        while time.monotonic() < deadline:
            measured, _ = self.observation.arm_snapshot(arm)
            if float(np.max(np.abs(measured - target))) <= self.config.reset_tolerance:
                return
            time.sleep(0.02)
        raise fault(ErrorCode.ACTION_INVALID, f"{arm} arm did not reach its configured reset target")

    def begin_stage(self, skill: str) -> None:
        self.preflight()
        self.online_gate.begin(skill)
        arm = self.config.skill_arms[skill]
        controlled, _ = self.observation.arm_snapshot(arm)
        self._active_skill = skill
        self._published_rows = 0
        self._last_command = controlled

    def publish(self, actions: Any) -> PublishResult:
        if not self._preflight_complete or self._active_skill is None or self._last_command is None:
            raise fault(ErrorCode.STATE_REJECTED, "ROS action executor has no active stage")
        rows = np.asarray(actions, dtype=np.float64)
        arm = self.config.skill_arms[self._active_skill]
        _, other_hold = self.observation.arm_snapshot(arm)
        first = rows[0]
        transition_period = 1.0 / self.config.chunk_transition_hz
        for fraction in np.linspace(0.0, 1.0, self.config.chunk_transition_steps + 1, dtype=np.float64)[1:]:
            command = self._last_command + fraction * (first - self._last_command)
            self._publish(arm, command, other_hold)
            time.sleep(transition_period)
        self._published_rows += 1
        self._last_command = first.copy()
        gate_result = self.online_gate.reached_result(self._active_skill, self._published_rows)
        if gate_result is not None:
            return PublishResult(True, "joint_rest_detected", published_rows=1, gate_result=gate_result)
        maximum_delta = np.asarray(self.config.max_row_delta, dtype=np.float64)
        period = 1.0 / self.config.publish_hz
        for row_index, row in enumerate(rows[1:], start=2):
            if np.any(np.abs(row - self._last_command) > maximum_delta):
                raise fault(ErrorCode.ACTION_INVALID, "adjacent policy action rows exceed ROS command delta limits")
            self._publish(arm, row, other_hold)
            self._last_command = row.copy()
            self._published_rows += 1
            time.sleep(period)
            gate_result = self.online_gate.reached_result(self._active_skill, self._published_rows)
            if gate_result is not None:
                return PublishResult(True, "joint_rest_detected", published_rows=row_index, gate_result=gate_result)
        return PublishResult(published_rows=len(rows))

    def stop(self, reason: str, *, publish_hold: bool = True) -> None:
        del reason
        if self._preflight_complete and self._has_published and publish_hold:
            try:
                left, right = self.observation.joint_snapshot()
            except RuntimeError:
                pass
            else:
                arm = self.config.skill_arms[self._active_skill] if self._active_skill else "left"
                controlled, other_hold = (left, right) if arm == "left" else (right, left)
                period = 1.0 / self.config.publish_hz
                for _ in range(self.config.stop_hold_repetitions):
                    self._publish(arm, controlled, other_hold)
                    time.sleep(period)
        self.deactivate()

    def deactivate(self) -> None:
        self._has_published = False
        self._active_skill = None
        self._last_command = None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._lock_stream is not None:
            fcntl.flock(self._lock_stream.fileno(), fcntl.LOCK_UN)
            self._lock_stream.close()
            self._lock_stream = None

    def _publish(self, arm: str, command: np.ndarray, other_hold: np.ndarray) -> None:
        self._publish_controlled(arm, command, other_hold)
        self._has_published = True

    def _acquire_action_lock(self) -> None:
        if self._lock_stream is not None:
            return
        self.config.lock_directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.config.lock_directory, 0o700)
        stream = (self.config.lock_directory / "action_executor.lock").open("a+", encoding="utf-8")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            stream.close()
            raise fault(ErrorCode.STATE_REJECTED, "another oven action executor holds the process lock") from exc
        self._lock_stream = stream
