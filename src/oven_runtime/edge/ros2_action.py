from __future__ import annotations

import os
from typing import Any

import numpy as np
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import JointState

from oven_runtime.edge.action_execution import ActionExecutionCore
from oven_runtime.edge.contracts import PublishResult
from oven_runtime.edge.joint_gate import OnlineJointGate
from oven_runtime.edge.profile_types import RosBridgeConfig
from oven_runtime.edge.ros2_observation import RosObservationSource


class RosActionExecutor(Node):
    """The mature direct-command transport around shared action semantics."""

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
        self._closed = False
        self._left_publisher = self.create_publisher(JointState, config.left_command_topic, 10)
        self._right_publisher = self.create_publisher(JointState, config.right_command_topic, 10)
        self._execution = ActionExecutionCore(
            config,
            observation,
            online_gate,
            publish_controlled=self._publish_controlled,
            external_publishers_present=self._external_publishers_present,
        )

    def preflight(self) -> None:
        self._execution.preflight()

    def reset(self, skill: str) -> None:
        self._execution.reset(skill)

    def begin_stage(self, skill: str) -> None:
        self._execution.begin_stage(skill)

    def rollout_generation(self) -> int:
        return self._execution.rollout_generation()

    def ensure_rollout_generation(self, generation: int) -> None:
        self._execution.ensure_rollout_generation(generation)

    def publish(self, actions: Any) -> PublishResult:
        return self._execution.publish(actions)

    def stop(self, reason: str) -> None:
        self._execution.stop(reason)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.destroy_node()
        self._execution.close()

    def _external_publishers_present(self) -> bool:
        own_name = self.get_name()
        own_namespace = self.get_namespace()
        for topic in (self.config.left_command_topic, self.config.right_command_topic):
            for endpoint in self.get_publishers_info_by_topic(topic):
                if endpoint.node_name != own_name or endpoint.node_namespace != own_namespace:
                    return True
        return False

    def _publish_pair(self, left: np.ndarray, right: np.ndarray) -> None:
        stamp = self.get_clock().now().to_msg()
        names = [f"joint{index + 1}" for index in range(7)]
        for publisher, values in ((self._left_publisher, left), (self._right_publisher, right)):
            message = JointState()
            message.header.stamp = stamp
            message.name = names
            message.position = np.asarray(values, dtype=np.float64).tolist()
            publisher.publish(message)

    def _publish_controlled(self, arm: str, command: np.ndarray, other_hold: np.ndarray) -> None:
        if arm == "left":
            self._publish_pair(command, other_hold)
        elif arm == "right":
            self._publish_pair(other_hold, command)
        else:
            raise ValueError("arm must be left or right")
