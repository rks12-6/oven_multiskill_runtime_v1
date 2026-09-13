from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np

from oven_runtime.edge.contracts import GateResult
from oven_runtime.edge.profile import load_agilex_profile
from oven_runtime.v2.hitl_control import RightHitlControlAdapter


ROOT = Path(__file__).resolve().parents[1]
MATURE_PROFILE = ROOT / "config" / "agilex_oven_v1.toml"


class Observation:
    def preflight(self) -> None:
        return

    def arm_snapshot(self, arm: str) -> tuple[np.ndarray, np.ndarray]:
        assert arm == "right"
        return np.zeros(7, dtype=np.float64), np.zeros(7, dtype=np.float64)


class NeverGate:
    def begin(self, skill: str) -> None:
        del skill

    def reached_result(self, skill: str, publish_step: int) -> GateResult | None:
        del skill, publish_step
        return None


class AsyncPrimeTransport:
    """Fake Piper boundary where topic delivery and ACK are explicitly separate."""

    def __init__(self, *, deliver_on_ack_query: int | None, full_hitl: bool = True) -> None:
        self.deliver_on_ack_query = deliver_on_ack_query
        self.full_hitl = full_hitl
        self.events: list[str] = []
        self.generation = 0
        self.policy_cached = False
        self.policy_enabled = False
        self.lease_generation: int | None = None
        self.lease_failure: str | None = None
        self.ack_queries = 0
        self.prime_messages: list[np.ndarray] = []
        self.front_commands: list[np.ndarray] = []
        self.rear_commands: list[np.ndarray] = []
        self.other_front_holds: list[np.ndarray] = []
        self.closed = False

    def preflight(self) -> None:
        self.events.append("preflight")

    def advance_policy_generation(self) -> int:
        self.generation += 1
        self.policy_cached = False
        self.events.append(f"generation:{self.generation}")
        return self.generation

    def publish_policy(self, positions: np.ndarray, generation: int) -> None:
        assert generation == self.generation
        if self.policy_enabled:
            self.events.append("execute_policy")
            self.front_commands.append(positions.copy())
            if self.full_hitl:
                self.rear_commands.append(positions.copy())
            return
        self.events.append("prime_policy")
        self.prime_messages.append(positions.copy())

    def wait_for_policy_prime(self, timeout_sec: float) -> None:
        assert timeout_sec > 0
        while True:
            self.ack_queries += 1
            if (
                self.deliver_on_ack_query is not None
                and self.ack_queries >= self.deliver_on_ack_query
            ):
                self.policy_cached = True
                self.events.append("subscriber_delivery")
            if self.policy_cached:
                self.events.append("prime_ack:true")
                return
            self.events.append("prime_ack:false")
            if self.deliver_on_ack_query is None or self.ack_queries >= 3:
                raise TimeoutError("timed out waiting for HITL policy prime: policy has not been received")

    def set_policy_enabled(self, enabled: bool) -> None:
        if enabled and not self.policy_cached:
            raise RuntimeError("POLICY rejected: policy has not been received")
        self.policy_enabled = enabled
        self.events.append(f"enable_policy:{enabled}")

    def start_policy_lease(
        self, generation: int, *, interval_sec: float, progress_timeout_sec: float
    ) -> None:
        assert self.policy_enabled
        assert generation == self.generation
        assert interval_sec > 0 and progress_timeout_sec > 0
        self.lease_generation = generation
        self.events.append(f"lease_start:{generation}")

    def stop_policy_lease(self) -> None:
        self.lease_generation = None
        self.events.append("lease_stop")

    def ensure_policy_lease_healthy(self) -> None:
        if self.lease_failure is not None:
            raise RuntimeError(self.lease_failure)

    def wait_for_state(
        self, expected: str, timeout_sec: float, *, pending_states: frozenset[str]
    ) -> None:
        del timeout_sec, pending_states
        assert expected == ("POLICY" if self.policy_enabled else "HOLD")
        self.events.append(f"state:{expected}")

    def start_reset(self) -> None:
        self.events.append("start_reset")

    def publish_other_front_hold(self, positions: np.ndarray) -> None:
        assert self.policy_enabled
        self.events.append("other_front_hold")
        self.other_front_holds.append(positions.copy())

    def request_manual_takeover(self) -> None:
        self.events.append("manual_takeover")

    def close(self) -> None:
        self.closed = True


class PolicyPrimingIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        profile = load_agilex_profile(
            MATURE_PROFILE,
            run_id="policy_priming_001",
            runtime_root=Path(self.temporary.name),
            selected_skill="rotate_button",
        )
        self.config = replace(
            profile.ros,
            chunk_transition_steps=1,
            chunk_transition_hz=1_000_000.0,
            publish_hz=1_000_000.0,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _adapter(self, transport: AsyncPrimeTransport) -> RightHitlControlAdapter:
        return RightHitlControlAdapter(
            self.config,
            Observation(),
            NeverGate(),  # type: ignore[arg-type]
            transport,  # type: ignore[arg-type]
            prime_ack_timeout_sec=1.0,
            policy_lease_interval_sec=0.1,
            policy_progress_timeout_sec=5.0,
            policy_state_timeout_sec=1.0,
            reset_state_timeout_sec=1.0,
        )

    def test_first_real_policy_is_primed_acknowledged_enabled_and_replayed_once(self) -> None:
        transport = AsyncPrimeTransport(deliver_on_ack_query=1)
        adapter = self._adapter(transport)
        row0 = np.full(7, 0.1, dtype=np.float64)
        row1 = np.full(7, 0.2, dtype=np.float64)
        try:
            adapter.begin_stage("rotate_button")
            self.assertNotIn("enable_policy:True", transport.events)

            result = adapter.publish(np.stack((row0, row1)))

            self.assertEqual(result.published_rows, 2)
            self.assertEqual(transport.events[:], [
                "preflight",
                "generation:1",
                "prime_policy",
                "subscriber_delivery",
                "prime_ack:true",
                "enable_policy:True",
                "state:POLICY",
                "lease_start:1",
                "execute_policy",
                "other_front_hold",
                "execute_policy",
                "other_front_hold",
            ])
            np.testing.assert_array_equal(transport.prime_messages[0], row0)
            self.assertEqual(len(transport.front_commands), 2)
            np.testing.assert_array_equal(transport.front_commands[0], row0)
            np.testing.assert_array_equal(transport.front_commands[1], row1)
            self.assertEqual(len(transport.rear_commands), 2)
            for front, rear in zip(transport.front_commands, transport.rear_commands):
                np.testing.assert_array_equal(front, rear)
            self.assertEqual(len(transport.other_front_holds), 2)
        finally:
            adapter.close()

    def test_ack_is_polled_until_async_subscriber_delivery_without_early_enable(self) -> None:
        transport = AsyncPrimeTransport(deliver_on_ack_query=3)
        adapter = self._adapter(transport)
        try:
            adapter.begin_stage("rotate_button")
            adapter.publish(np.full((1, 7), 0.1, dtype=np.float64))

            self.assertEqual(transport.events[2:9], [
                "prime_policy",
                "prime_ack:false",
                "prime_ack:false",
                "subscriber_delivery",
                "prime_ack:true",
                "enable_policy:True",
                "state:POLICY",
            ])
            self.assertEqual(transport.front_commands[0].tolist(), [0.1] * 7)
        finally:
            adapter.close()

    def test_missing_prime_ack_fails_closed_without_final_motion(self) -> None:
        transport = AsyncPrimeTransport(deliver_on_ack_query=None)
        adapter = self._adapter(transport)
        try:
            adapter.begin_stage("rotate_button")
            with self.assertRaisesRegex(TimeoutError, "policy prime"):
                adapter.publish(np.full((1, 7), 0.1, dtype=np.float64))

            self.assertNotIn("enable_policy:True", transport.events)
            self.assertEqual(transport.front_commands, [])
            self.assertEqual(transport.rear_commands, [])
            self.assertEqual(transport.other_front_holds, [])
        finally:
            adapter.close()

    def test_policy_only_keeps_rear_shadow_absent_after_same_handshake(self) -> None:
        transport = AsyncPrimeTransport(deliver_on_ack_query=1, full_hitl=False)
        adapter = self._adapter(transport)
        try:
            adapter.begin_stage("rotate_button")
            adapter.publish(np.full((1, 7), 0.1, dtype=np.float64))

            self.assertEqual(len(transport.front_commands), 1)
            self.assertEqual(transport.rear_commands, [])
        finally:
            adapter.close()


if __name__ == "__main__":
    unittest.main()
