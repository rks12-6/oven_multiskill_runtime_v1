from __future__ import annotations

import fcntl
import math
import os
import time
from typing import Any

import numpy as np
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import JointState

from oven_runtime.common.errors import ErrorCode, fault
from oven_runtime.edge.contracts import PublishResult
from oven_runtime.edge.joint_gate import OnlineJointGate
from oven_runtime.edge.profile_types import RosBridgeConfig
from oven_runtime.edge.ros2_observation import RosObservationSource


class RosActionExecutor(Node):
    """The only component allowed to create robot command publishers."""

    def __init__(
        self,
        config: RosBridgeConfig,
        observation: RosObservationSource,
        online_gate: OnlineJointGate,
    ) -> None:
        config.validate()
        if not rclpy.ok():
            raise RuntimeError("create RosObservationSource before RosActionExecutor")
        super().__init__(f"oven_action_executor_{os.getpid()}")
        self.config = config
        self.observation = observation
        self.online_gate = online_gate
        self._active_skill: str | None = None
        self._published_rows = 0
        self._last_command: np.ndarray | None = None
        self._has_published = False
        self._preflight_complete = False
        self._lock_stream: Any | None = None
        self._closed = False
        self._left_publisher = self.create_publisher(JointState, config.left_command_topic, 10)
        self._right_publisher = self.create_publisher(JointState, config.right_command_topic, 10)

    def preflight(self) -> None:
        if self._preflight_complete:
            return
        self.observation.preflight()
        self._acquire_action_lock()
        if self._external_publishers_present():
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
            self._publish_controlled(arm, start + smooth * (target - start), other_hold)
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
            self._publish_controlled(arm, command, other_hold)
            time.sleep(transition_period)
        self._published_rows += 1
        self._last_command = first.copy()
        if self.online_gate.reached(self._active_skill, self._published_rows):
            return PublishResult(True, "joint_rest_detected", published_rows=1)

        maximum_delta = np.asarray(self.config.max_row_delta, dtype=np.float64)
        period = 1.0 / self.config.publish_hz
        for row_index, row in enumerate(rows[1:], start=2):
            if np.any(np.abs(row - self._last_command) > maximum_delta):
                raise fault(ErrorCode.ACTION_INVALID, "adjacent policy action rows exceed ROS command delta limits")
            self._publish_controlled(arm, row, other_hold)
            self._last_command = row.copy()
            self._published_rows += 1
            time.sleep(period)
            if self.online_gate.reached(self._active_skill, self._published_rows):
                return PublishResult(True, "joint_rest_detected", published_rows=row_index)
        return PublishResult(published_rows=len(rows))

    def stop(self, reason: str) -> None:
        del reason
        if not self._preflight_complete or not self._has_published:
            self._active_skill = None
            self._last_command = None
            return
        try:
            left, right = self.observation.joint_snapshot()
        except RuntimeError:
            self._has_published = False
            self._active_skill = None
            self._last_command = None
            return
        period = 1.0 / self.config.publish_hz
        for _ in range(self.config.stop_hold_repetitions):
            self._publish_pair(left, right)
            time.sleep(period)
        self._has_published = False
        self._active_skill = None
        self._last_command = None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.destroy_node()
        if self._lock_stream is not None:
            fcntl.flock(self._lock_stream.fileno(), fcntl.LOCK_UN)
            self._lock_stream.close()
            self._lock_stream = None

    def _external_publishers_present(self) -> bool:
        own_name = self.get_name()
        own_namespace = self.get_namespace()
        for topic in (self.config.left_command_topic, self.config.right_command_topic):
            for endpoint in self.get_publishers_info_by_topic(topic):
                if endpoint.node_name != own_name or endpoint.node_namespace != own_namespace:
                    return True
        return False

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

    def _publish_pair(self, left: np.ndarray, right: np.ndarray) -> None:
        stamp = self.get_clock().now().to_msg()
        names = [f"joint{index + 1}" for index in range(7)]
        for publisher, values in ((self._left_publisher, left), (self._right_publisher, right)):
            message = JointState()
            message.header.stamp = stamp
            message.name = names
            message.position = np.asarray(values, dtype=np.float64).tolist()
            publisher.publish(message)
            self._has_published = True

    def _publish_controlled(self, arm: str, command: np.ndarray, other_hold: np.ndarray) -> None:
        if arm == "left":
            self._publish_pair(command, other_hold)
        elif arm == "right":
            self._publish_pair(other_hold, command)
        else:
            raise ValueError("arm must be left or right")
