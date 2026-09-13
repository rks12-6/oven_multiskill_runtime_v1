"""ROS 2 transport implementation for the existing piper_hitl public contract."""

from __future__ import annotations

import os
import time
from threading import Event, Lock, Thread, current_thread

import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import JointState
from std_msgs.msg import String, UInt64
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
            or arm_pair.policy_prime_ready_service is None
            or arm_pair.policy_lease_topic is None
            or arm_pair.reset_service is None
            or arm_pair.other_front_command_topic is None
        ):
            raise ValueError("HITL public endpoints must be configured")
        super().__init__(f"oven_right_hitl_transport_{os.getpid()}")
        self._arm_pair = arm_pair
        self._state: str | None = None
        self._closed = False
        self._policy_publisher = self.create_publisher(JointState, arm_pair.policy_input_topic, 10)
        self._policy_lease_publisher = self.create_publisher(UInt64, arm_pair.policy_lease_topic, 10)
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
        self._policy_prime_ready_client = self.create_client(
            Trigger, arm_pair.policy_prime_ready_service
        )
        self._reset_client = self.create_client(Trigger, arm_pair.reset_service)
        self._generation_client = self.create_client(Trigger, '/hitl/advance_policy_generation')
        self._manual_takeover_client = self.create_client(SetBool, '/hitl/manual_takeover')
        self._lease_lock = Lock()
        self._lease_stop = Event()
        self._lease_thread: Thread | None = None
        self._lease_generation: int | None = None
        self._lease_interval_sec: float | None = None
        self._progress_timeout_sec: float | None = None
        self._last_policy_progress_at: float | None = None
        self._lease_failure: str | None = None

    def preflight(self) -> None:
        self._require_open()
        for client, name in (
            (self._policy_client, self._arm_pair.policy_enable_service),
            (self._policy_prime_ready_client, self._arm_pair.policy_prime_ready_service),
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

    def wait_for_policy_prime(self, timeout_sec: float) -> None:
        """Wait for Piper's read-only acknowledgement of the current policy cache."""

        self._require_open()
        if timeout_sec <= 0:
            raise ValueError("policy prime acknowledgement timeout must be positive")
        deadline = time.monotonic() + timeout_sec
        last_reason = "policy prime acknowledgement not received"
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"timed out waiting for HITL policy prime: {last_reason}")
            response = self._call(
                self._policy_prime_ready_client,
                Trigger.Request(),
                "policy_prime_ready",
                timeout_sec=remaining,
            )
            if response.success:
                return
            last_reason = response.message

    def start_policy_lease(
        self, generation: int, *, interval_sec: float, progress_timeout_sec: float
    ) -> None:
        """Start a generation-scoped controller lease after POLICY is active."""

        self._require_open()
        if generation < 0 or interval_sec <= 0 or progress_timeout_sec <= 0:
            raise ValueError("policy lease generation and timeouts must be positive")
        with self._lease_lock:
            if self._lease_thread is not None and self._lease_thread.is_alive():
                raise RuntimeError("HITL policy lease is already active")
            self._lease_stop.clear()
            self._lease_generation = generation
            self._lease_interval_sec = interval_sec
            self._progress_timeout_sec = progress_timeout_sec
            self._last_policy_progress_at = time.monotonic()
            self._lease_failure = None
        try:
            self._publish_policy_lease(generation)
        except Exception:
            self.stop_policy_lease()
            raise
        thread = Thread(target=self._policy_lease_loop, name="hitl-policy-lease", daemon=True)
        with self._lease_lock:
            self._lease_thread = thread
        thread.start()

    def stop_policy_lease(self) -> None:
        """Immediately revoke local lease publication; idempotent during cleanup."""

        with self._lease_lock:
            self._lease_generation = None
            self._lease_interval_sec = None
            self._progress_timeout_sec = None
            self._last_policy_progress_at = None
            self._lease_stop.set()
            thread = self._lease_thread
        if thread is not None and thread is not current_thread():
            thread.join(timeout=1.0)
        with self._lease_lock:
            if self._lease_thread is thread and (thread is None or not thread.is_alive()):
                self._lease_thread = None

    def ensure_policy_lease_healthy(self) -> None:
        """Raise in the rollout thread if a live controller exceeded progress deadline."""

        with self._lease_lock:
            failure = self._lease_failure
        if failure is not None:
            raise RuntimeError(f"HITL policy lease failed closed: {failure}")

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
        with self._lease_lock:
            if self._lease_generation == generation:
                self._last_policy_progress_at = time.monotonic()

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
        self.stop_policy_lease()
        self._closed = True
        self.destroy_node()

    def _policy_lease_loop(self) -> None:
        while True:
            with self._lease_lock:
                interval_sec = self._lease_interval_sec
            if interval_sec is None or self._lease_stop.wait(interval_sec):
                return
            with self._lease_lock:
                generation = self._lease_generation
                progress_timeout_sec = self._progress_timeout_sec
                last_progress_at = self._last_policy_progress_at
            if generation is None or progress_timeout_sec is None or last_progress_at is None:
                return
            age = time.monotonic() - last_progress_at
            if age > progress_timeout_sec:
                with self._lease_lock:
                    self._lease_failure = (
                        f"policy progress stale ({age:.3f}s > {progress_timeout_sec:.3f}s)"
                    )
                    self._lease_generation = None
                return
            try:
                self._publish_policy_lease(generation)
            except Exception as error:
                with self._lease_lock:
                    self._lease_failure = f"lease publisher failed: {type(error).__name__}"
                    self._lease_generation = None
                return

    def _publish_policy_lease(self, generation: int) -> None:
        message = UInt64()
        message.data = generation
        self._policy_lease_publisher.publish(message)

    def _call(
        self, client: object, request: object, name: str, *, timeout_sec: float = 1.0
    ) -> object:
        self._require_open()
        if timeout_sec <= 0:
            raise TimeoutError(f"timed out calling HITL service {name}")
        future = client.call_async(request)  # type: ignore[attr-defined]
        rclpy.spin_until_future_complete(self, future, timeout_sec=timeout_sec)
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
