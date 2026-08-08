from __future__ import annotations

import os
import threading
import time

import cv2
import numpy as np
import rclpy
from cv_bridge import CvBridge
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image, JointState

from oven_runtime.common.errors import ErrorCode, fault
from oven_runtime.edge.contracts import Observation
from oven_runtime.edge.joint_gate import OnlineJointGate
from oven_runtime.edge.profile_types import RosBridgeConfig


class RosObservationSource(Node):
    """Read-only ROS 2 node: it creates subscriptions, never publishers."""

    def __init__(self, config: RosBridgeConfig, online_gate: OnlineJointGate) -> None:
        config.validate()
        self._owns_rclpy = not rclpy.ok()
        if self._owns_rclpy:
            rclpy.init()
        super().__init__(f"oven_observation_{os.getpid()}")
        self.config = config
        self.online_gate = online_gate
        self._cv_bridge = CvBridge()
        self._data_lock = threading.RLock()
        self._left_joint: tuple[np.ndarray, int] | None = None
        self._right_joint: tuple[np.ndarray, int] | None = None
        self._left_image: tuple[Image, int] | None = None
        self._right_image: tuple[Image, int] | None = None
        self._front_image: tuple[Image, int] | None = None
        self._preflight_complete = False
        self._closed = False

        self.create_subscription(JointState, config.left_joint_topic, self._left_joint_callback, qos_profile_sensor_data)
        self.create_subscription(
            JointState, config.right_joint_topic, self._right_joint_callback, qos_profile_sensor_data
        )
        self.create_subscription(Image, config.left_camera_topic, self._left_image_callback, qos_profile_sensor_data)
        self.create_subscription(Image, config.right_camera_topic, self._right_image_callback, qos_profile_sensor_data)
        self.create_subscription(Image, config.front_camera_topic, self._front_image_callback, qos_profile_sensor_data)

        self._executor = MultiThreadedExecutor(num_threads=2)
        self._executor.add_node(self)
        self._spin_thread = threading.Thread(target=self._executor.spin, name="oven-ros2-observation", daemon=True)
        self._spin_thread.start()

    def preflight(self) -> None:
        if self._preflight_complete:
            return
        deadline = time.monotonic() + self.config.preflight_timeout_sec
        while time.monotonic() < deadline:
            if self._messages_fresh():
                self._preflight_complete = True
                return
            time.sleep(0.02)
        raise fault(ErrorCode.OBSERVATION_STALE, "required Agilex ROS observations are not fresh")

    def sample(self, skill: str) -> Observation:
        if not self._preflight_complete:
            raise fault(ErrorCode.STATE_REJECTED, "ROS observation preflight has not completed")
        if skill not in self.config.prompts:
            raise fault(ErrorCode.INVALID_FIELD_VALUE, "skill has no configured prompt")
        with self._data_lock:
            arm = self.config.skill_arms[skill]
            joint = self._left_joint if arm == "left" else self._right_joint
            wrist_image = self._left_image if arm == "left" else self._right_image
            entries = (joint, wrist_image, self._front_image)
            if any(entry is None for entry in entries):
                raise fault(ErrorCode.OBSERVATION_STALE, "required ROS observations are missing")
            front_image = self._front_image
            assert joint is not None and wrist_image is not None and front_image is not None
            state = joint[0].copy()
            captured_at = min(entry[1] for entry in entries if entry is not None)
            wrist_message = wrist_image[0]
            front_message = front_image[0]
        cam_wrist = self._convert_image(wrist_message)
        cam_high = self._convert_image(front_message)
        wrist_key = "cam_left_wrist" if arm == "left" else "cam_right_wrist"
        return Observation(
            payload={
                "state": state,
                "images": {
                    wrist_key: cam_wrist,
                    "cam_high": cam_high,
                    "cam_low": cam_high.copy(),
                },
                "prompt": self.config.prompts[skill],
            },
            captured_at_monotonic_ns=captured_at,
        )

    def front_rgb(self) -> tuple[np.ndarray, int]:
        with self._data_lock:
            if self._front_image is None:
                raise fault(ErrorCode.OBSERVATION_STALE, "front camera observation is unavailable")
            message, captured_at = self._front_image
        rgb = self._cv_bridge.imgmsg_to_cv2(message, desired_encoding="rgb8")
        return np.asarray(rgb, dtype=np.uint8), captured_at

    def joint_snapshot(self) -> tuple[np.ndarray, np.ndarray]:
        with self._data_lock:
            if self._left_joint is None or self._right_joint is None:
                raise RuntimeError("joint observations are unavailable")
            return self._left_joint[0].copy(), self._right_joint[0].copy()

    def arm_snapshot(self, arm: str) -> tuple[np.ndarray, np.ndarray]:
        left, right = self.joint_snapshot()
        if arm == "left":
            return left, right
        if arm == "right":
            return right, left
        raise ValueError("arm must be left or right")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._executor.shutdown(timeout_sec=2.0)
        self._spin_thread.join(timeout=2.0)
        self.destroy_node()
        if self._owns_rclpy and rclpy.ok():
            rclpy.shutdown()

    def _left_joint_callback(self, message: JointState) -> None:
        value = self._joint_array(message)
        if value is None:
            return
        received = time.monotonic_ns()
        with self._data_lock:
            self._left_joint = (value, received)
        self.online_gate.update("left", value)

    def _right_joint_callback(self, message: JointState) -> None:
        value = self._joint_array(message)
        if value is not None:
            with self._data_lock:
                self._right_joint = (value, time.monotonic_ns())
            self.online_gate.update("right", value)

    def _left_image_callback(self, message: Image) -> None:
        with self._data_lock:
            self._left_image = (message, time.monotonic_ns())

    def _right_image_callback(self, message: Image) -> None:
        with self._data_lock:
            self._right_image = (message, time.monotonic_ns())

    def _front_image_callback(self, message: Image) -> None:
        with self._data_lock:
            self._front_image = (message, time.monotonic_ns())

    @staticmethod
    def _joint_array(message: JointState) -> np.ndarray | None:
        value = np.asarray(message.position[:7], dtype=np.float64)
        if value.shape != (7,) or not np.isfinite(value).all():
            return None
        return value

    def _messages_fresh(self) -> bool:
        now = time.monotonic_ns()
        maximum_age_ns = int(self.config.max_message_age_sec * 1_000_000_000)
        with self._data_lock:
            entries = [self._left_joint, self._right_joint, self._front_image]
            if "left" in self.config.skill_arms.values():
                entries.append(self._left_image)
            if "right" in self.config.skill_arms.values():
                entries.append(self._right_image)
            return all(entry is not None and 0 <= now - entry[1] <= maximum_age_ns for entry in entries)

    def _convert_image(self, message: Image) -> np.ndarray:
        rgb = self._cv_bridge.imgmsg_to_cv2(message, desired_encoding="rgb8")
        height, width = rgb.shape[:2]
        scale = min(self.config.image_size / width, self.config.image_size / height)
        resized_width = max(1, int(round(width * scale)))
        resized_height = max(1, int(round(height * scale)))
        resized = cv2.resize(rgb, (resized_width, resized_height), interpolation=cv2.INTER_AREA)
        canvas = np.zeros((self.config.image_size, self.config.image_size, 3), dtype=np.uint8)
        x = (self.config.image_size - resized_width) // 2
        y = (self.config.image_size - resized_height) // 2
        canvas[y : y + resized_height, x : x + resized_width] = resized
        return np.transpose(canvas, (2, 0, 1))
