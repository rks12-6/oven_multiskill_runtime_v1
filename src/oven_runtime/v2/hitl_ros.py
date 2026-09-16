"""ROS 2 transport implementation for the existing piper_hitl public contract."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from threading import Event, Lock, Thread, current_thread

import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import JointState
from std_msgs.msg import String, UInt64
from std_srvs.srv import SetBool, Trigger

from oven_runtime.v2.contracts import ArmPairProfile


RESET_OUTCOME_SCHEMA_VERSION = 1
RESET_OUTCOMES = frozenset({'IDLE', 'RESETTING', 'RESET_COMPLETE', 'RESET_INCOMPLETE', 'RESET_FAULT'})


def parse_reset_outcome(payload: str) -> tuple[int, str]:
    """Validate the machine-readable reset contract; never infer it from logs."""

    try:
        value = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError('invalid HITL reset outcome JSON') from error
    if not isinstance(value, dict) or value.get('schema_version') != RESET_OUTCOME_SCHEMA_VERSION:
        raise ValueError('unsupported HITL reset outcome schema')
    attempt_id = value.get('attempt_id')
    outcome = value.get('outcome')
    if not isinstance(outcome, str) or outcome not in RESET_OUTCOMES:
        raise ValueError('HITL reset outcome value is invalid')
    if (
        not isinstance(attempt_id, int)
        or isinstance(attempt_id, bool)
        or attempt_id < 0
        or (outcome == 'IDLE' and attempt_id != 0)
        or (outcome != 'IDLE' and attempt_id < 1)
    ):
        raise ValueError('HITL reset outcome attempt_id is invalid')
    return attempt_id, outcome


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
            or arm_pair.reset_outcome_topic is None
            or arm_pair.other_front_command_topic is None
        ):
            raise ValueError("HITL public endpoints must be configured")
        if (
            arm_pair.hitl_mode is not None
            and arm_pair.hitl_mode.value == 'full_hitl'
            and arm_pair.physical_takeover_topic is None
        ):
            raise ValueError("full-HITL requires a physical takeover topic")
        super().__init__(f"oven_right_hitl_transport_{os.getpid()}")
        self._arm_pair = arm_pair
        self._state: str | None = None
        self._reset_outcomes: dict[int, str] = {}
        self._reset_outcome_error: str | None = None
        self._reset_outcome_lock = Lock()
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
        self.create_subscription(
            String, arm_pair.reset_outcome_topic, self._reset_outcome_callback, state_qos
        )
        if arm_pair.physical_takeover_topic is not None:
            self.create_subscription(
                UInt64,
                arm_pair.physical_takeover_topic,
                self._physical_takeover_callback,
                10,
            )
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
        self._physical_takeover_handler: Callable[[int], None] | None = None
        self._physical_takeover_generation: int | None = None

    def set_physical_takeover_handler(self, handler: Callable[[int], None]) -> None:
        """Register the v2 owner cleanup for a generation-scoped Teach event."""

        with self._lease_lock:
            self._physical_takeover_handler = handler

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
            self._physical_takeover_generation = None
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
            self._physical_takeover_generation = None
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

    def start_reset(self) -> int:
        response = self._call(self._reset_client, Trigger.Request(), "start_reset")
        if not response.success:
            raise RuntimeError(f"HITL reset request rejected: {response.message}")
        try:
            attempt_id, outcome = parse_reset_outcome(response.message)
        except ValueError as error:
            raise RuntimeError(f'HITL reset response contract invalid: {error}') from error
        if outcome != 'RESETTING':
            raise RuntimeError(f'HITL reset response has unexpected outcome: {outcome}')
        return attempt_id

    def wait_for_reset_terminal(self, attempt_id: int, timeout_sec: float) -> str:
        """Wait only for this start-reset attempt, ignoring latched older outcomes."""

        self._require_open()
        if attempt_id < 1 or timeout_sec <= 0:
            raise ValueError('reset attempt identity and timeout must be positive')
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            with self._reset_outcome_lock:
                parse_error = self._reset_outcome_error
                outcome = self._reset_outcomes.get(attempt_id)
            if parse_error is not None:
                raise RuntimeError(f'HITL reset outcome contract invalid: {parse_error}')
            if outcome in {'RESET_COMPLETE', 'RESET_INCOMPLETE'}:
                return outcome
            if outcome == 'RESET_FAULT' or self._state == 'FAULT':
                raise RuntimeError(f'HITL reset attempt {attempt_id} entered FAULT')
            rclpy.spin_once(self, timeout_sec=min(0.1, max(0.0, deadline - time.monotonic())))
        raise TimeoutError(f'timed out waiting for HITL reset attempt {attempt_id} terminal outcome')

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
        next_lease_at = time.monotonic()
        while True:
            with self._lease_lock:
                interval_sec = self._lease_interval_sec
            if interval_sec is None:
                return
            # The rollout thread can be blocked in inference or in a chunk.
            # Poll subscriptions here so physical Teach still reaches the
            # cancellation owner without waiting for the next inference result.
            wait_sec = min(0.01, max(0.0, next_lease_at - time.monotonic()))
            if self._lease_stop.wait(wait_sec):
                return
            try:
                rclpy.spin_once(self, timeout_sec=0.0)
            except Exception as error:
                with self._lease_lock:
                    self._lease_failure = f"physical takeover polling failed: {type(error).__name__}"
                    self._lease_generation = None
                return
            if self._dispatch_physical_takeover():
                return
            if time.monotonic() < next_lease_at:
                continue
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
            next_lease_at = time.monotonic() + interval_sec

    def _physical_takeover_callback(self, message: UInt64) -> None:
        """Record only; cleanup runs outside the ROS subscription callback."""

        with self._lease_lock:
            self._physical_takeover_generation = int(message.data)

    def _dispatch_physical_takeover(self) -> bool:
        """Run matching physical takeover cleanup in the lease worker context."""

        with self._lease_lock:
            generation = self._physical_takeover_generation
            self._physical_takeover_generation = None
            active_generation = self._lease_generation
            handler = self._physical_takeover_handler
        if generation is None or generation != active_generation:
            return False
        if handler is None:
            with self._lease_lock:
                self._lease_failure = "physical takeover received without a v2 cancellation handler"
                self._lease_generation = None
            return True
        try:
            handler(generation)
        except Exception as error:
            with self._lease_lock:
                self._lease_failure = f"physical takeover cleanup failed: {type(error).__name__}"
                self._lease_generation = None
            return True
        return True

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

    def _reset_outcome_callback(self, message: String) -> None:
        try:
            attempt_id, outcome = parse_reset_outcome(message.data)
        except ValueError as error:
            with self._reset_outcome_lock:
                self._reset_outcome_error = str(error)
            return
        with self._reset_outcome_lock:
            self._reset_outcomes[attempt_id] = outcome

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("RosHitlTransport is closed")
