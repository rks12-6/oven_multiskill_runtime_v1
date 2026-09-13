"""ROS 2 transport implementation for the existing piper_hitl public contract."""

from __future__ import annotations

import os
import time

import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import JointState
from std_msgs.msg import String
from std_srvs.srv import SetBool, Trigger

from oven_runtime.v2.contracts import ArmPairProfile


class RosHitlTransport(Node):
    """Owns policy input, other-front hold, and HITL public service clients."""

    def __init__(self, arm_pair: ArmPairProfile) -> None:
        if not rclpy.ok():
            raise RuntimeError("create RosObservationSource before RosHitlTransport")
        if not arm_pair.hitl_enabled:
            raise ValueError("RosHitlTransport requires an HITL-enabled arm pair")
        if (
            arm_pair.hitl_state_topic is None
            or arm_pair.policy_enable_service is None
            or arm_pair.reset_service is None
            or arm_pair.other_front_command_topic is None
        ):
            raise ValueError("HITL public endpoints must be configured")
        super().__init__(f"oven_right_hitl_transport_{os.getpid()}")
        self._arm_pair = arm_pair
        self._state: str | None = None
        self._closed = False
        self._policy_publisher = self.create_publisher(JointState, arm_pair.policy_input_topic, 10)
        self._other_front_hold_publisher = self.create_publisher(
            JointState, arm_pair.other_front_command_topic, 10
        )
        state_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.create_subscription(String, arm_pair.hitl_state_topic, self._state_callback, state_qos)
        self._policy_client = self.create_client(SetBool, arm_pair.policy_enable_service)
        self._reset_client = self.create_client(Trigger, arm_pair.reset_service)
        self._generation_client = self.create_client(Trigger, '/hitl/advance_policy_generation')
        self._manual_takeover_client = self.create_client(SetBool, '/hitl/manual_takeover')

    def preflight(self) -> None:
        self._require_open()
        for client, name in (
            (self._policy_client, self._arm_pair.policy_enable_service),
            (self._reset_client, self._arm_pair.reset_service),
            (self._generation_client, '/hitl/advance_policy_generation'),
        ):
            if not client.wait_for_service(timeout_sec=1.0):
                raise RuntimeError(f"required HITL service is unavailable: {name}")

    def set_policy_enabled(self, enabled: bool) -> None:
        request = SetBool.Request()
        request.data = enabled
        response = self._call(self._policy_client, request, "enable_policy")
        if not response.success:
            raise RuntimeError(f"HITL policy request rejected: {response.message}")

    def start_reset(self) -> None:
        response = self._call(self._reset_client, Trigger.Request(), "start_reset")
        if not response.success:
            raise RuntimeError(f"HITL reset request rejected: {response.message}")

    def advance_policy_generation(self) -> int:
        response = self._call(self._generation_client, Trigger.Request(), 'advance_policy_generation')
        if not response.success:
            raise RuntimeError(f"HITL policy generation request rejected: {response.message}")
        prefix = 'policy generation advanced to '
        if not response.message.startswith(prefix):
            raise RuntimeError(f"invalid HITL policy generation response: {response.message}")
        try:
            return int(response.message[len(prefix):])
        except ValueError as error:
            raise RuntimeError(f"invalid HITL policy generation response: {response.message}") from error

    def request_manual_takeover(self) -> None:
        if self._arm_pair.hitl_mode is None or self._arm_pair.hitl_mode.value != 'full_hitl':
            raise RuntimeError('manual takeover is unavailable for policy_only HITL')
        request = SetBool.Request()
        request.data = True
        response = self._call(self._manual_takeover_client, request, 'manual_takeover')
        if not response.success:
            raise RuntimeError(f"HITL manual takeover request rejected: {response.message}")

    def wait_for_state(
        self, expected: str, timeout_sec: float, *, pending_states: frozenset[str]
    ) -> None:
        self._require_open()
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            state = self._state
            if state == expected:
                return
            if state == "FAULT":
                raise RuntimeError(f"HITL entered FAULT while waiting for {expected}")
            if state is not None and state not in pending_states:
                raise RuntimeError(f"unexpected HITL state {state} while waiting for {expected}")
            rclpy.spin_once(self, timeout_sec=min(0.1, max(0.0, deadline - time.monotonic())))
        raise TimeoutError(f"timed out waiting for HITL state {expected}")

    def publish_policy(self, positions: np.ndarray, generation: int) -> None:
        self._require_open()
        values = np.asarray(positions, dtype=np.float64)
        if values.shape != (7,) or not np.isfinite(values).all():
            raise ValueError("policy command must be seven finite values")
        message = JointState()
        message.header.stamp = self.get_clock().now().to_msg()
        message.header.frame_id = f'hitl_generation:{generation}'
        message.name = [f"joint{index}" for index in range(7)]
        message.position = values.tolist()
        self._policy_publisher.publish(message)

    def publish_other_front_hold(self, positions: np.ndarray) -> None:
        """Publish the measured hold for the non-policy front arm only."""

        self._require_open()
        values = np.asarray(positions, dtype=np.float64)
        if values.shape != (7,) or not np.isfinite(values).all():
            raise ValueError("other-front hold command must be seven finite values")
        message = JointState()
        message.header.stamp = self.get_clock().now().to_msg()
        message.name = [f"joint{index + 1}" for index in range(7)]
        message.position = values.tolist()
        self._other_front_hold_publisher.publish(message)

    def close(self) -> None:
        self._require_open()
        self._closed = True
        self.destroy_node()

    def _call(self, client: object, request: object, name: str) -> object:
        self._require_open()
        future = client.call_async(request)  # type: ignore[attr-defined]
        rclpy.spin_until_future_complete(self, future, timeout_sec=1.0)
        if not future.done():
            raise TimeoutError(f"timed out calling HITL service {name}")
        error = future.exception()
        if error is not None:
            raise error
        response = future.result()
        if response is None:
            raise RuntimeError(f"HITL service {name} returned no response")
        return response

    def _state_callback(self, message: String) -> None:
        self._state = message.data

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("RosHitlTransport is closed")
