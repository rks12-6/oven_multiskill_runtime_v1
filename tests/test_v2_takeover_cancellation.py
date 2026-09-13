from __future__ import annotations

import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np

from oven_runtime.edge.action_execution import ActionExecutionCore, PolicyCancelled
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

    def joint_snapshot(self) -> tuple[np.ndarray, np.ndarray]:
        return np.zeros(7, dtype=np.float64), np.zeros(7, dtype=np.float64)


class NeverGate:
    def begin(self, skill: str) -> None:
        del skill

    def reached_result(self, skill: str, publish_step: int) -> GateResult | None:
        del skill, publish_step
        return None


class RecordingExecution:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.generation = 10

    def preflight(self) -> None:
        self.events.append("producer_preflight")

    def begin_stage(self, skill: str) -> None:
        self.events.append(f"producer_begin:{skill}")

    def publish(self, actions: object):
        self.events.append("producer_publish")
        return GateResult(True, 1, 0.0, 0.0, "recording")

    def deactivate(self) -> None:
        self.events.append("producer_deactivate")

    def cancel_active_rollout(self) -> int:
        self.events.append("producer_cancel")
        self.generation += 1
        return self.generation

    def rollout_generation(self) -> int:
        return self.generation

    def ensure_rollout_generation(self, generation: int) -> None:
        if generation != self.generation:
            raise PolicyCancelled(generation)

    def close(self) -> None:
        self.events.append("producer_close")


class RecordingTransport:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.generation = 0

    def preflight(self) -> None:
        self.events.append("transport_preflight")

    def set_policy_enabled(self, enabled: bool) -> None:
        self.events.append(f"policy_enabled:{enabled}")

    def start_reset(self) -> None:
        self.events.append("start_reset")

    def advance_policy_generation(self) -> int:
        self.generation += 1
        self.events.append(f"generation:{self.generation}")
        return self.generation

    def wait_for_policy_prime(self, timeout_sec: float) -> None:
        del timeout_sec
        self.events.append("prime_ack")

    def wait_for_state(
        self, expected: str, timeout_sec: float, *, pending_states: frozenset[str]
    ) -> None:
        del timeout_sec, pending_states
        self.events.append(f"wait:{expected}")

    def publish_policy(self, positions: np.ndarray, generation: int) -> None:
        del positions
        self.events.append(f"publish_policy:{generation}")

    def publish_other_front_hold(self, positions: np.ndarray) -> None:
        del positions
        self.events.append("publish_other_front_hold")

    def request_manual_takeover(self) -> None:
        self.events.append("manual_takeover")

    def close(self) -> None:
        self.events.append("transport_close")


class TakeoverCancellationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        profile = load_agilex_profile(
            MATURE_PROFILE,
            run_id="takeover_cancel_001",
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

    def test_current_chunk_cancellation_stops_remaining_rows_and_invalidates_generation(self) -> None:
        entered_publish = threading.Event()
        published: list[np.ndarray] = []
        holder: dict[str, object] = {}

        def publish_controlled(arm: str, command: np.ndarray, other_hold: np.ndarray) -> None:
            del arm, other_hold
            published.append(command.copy())
            entered_publish.set()
            generation = int(holder["generation"])
            while not core.cancellation_requested(generation):
                time.sleep(0.001)

        core = ActionExecutionCore(
            self.config,
            Observation(),
            NeverGate(),  # type: ignore[arg-type]
            publish_controlled=publish_controlled,
        )
        generation = core.begin_stage("rotate_button")
        holder["generation"] = generation
        result: list[BaseException] = []
        thread = threading.Thread(
            target=lambda: self._capture_publish_failure(core, np.zeros((50, 7)), result)
        )
        thread.start()
        self.assertTrue(entered_publish.wait(timeout=1.0))

        next_generation = core.cancel_active_rollout()
        thread.join(timeout=1.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(published), 1)
        self.assertEqual(next_generation, generation + 1)
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], PolicyCancelled)
        with self.assertRaises(PolicyCancelled):
            core.ensure_rollout_generation(generation)

    def test_normal_rollout_without_takeover_preserves_chunk_behavior(self) -> None:
        published: list[np.ndarray] = []
        core = ActionExecutionCore(
            self.config,
            Observation(),
            NeverGate(),  # type: ignore[arg-type]
            publish_controlled=lambda arm, command, other_hold: published.append(command.copy()),
        )
        core.begin_stage("rotate_button")

        result = core.publish(np.zeros((4, 7), dtype=np.float64))

        self.assertEqual(result.published_rows, 4)
        self.assertEqual(len(published), 4)  # One transition target plus the remaining three rows.

    def test_takeover_orders_cancel_generation_fence_disable_then_manual_service(self) -> None:
        events: list[str] = []
        execution = RecordingExecution(events)
        transport = RecordingTransport(events)
        adapter = RightHitlControlAdapter(
            self.config,
            Observation(),
            NeverGate(),  # type: ignore[arg-type]
            transport,  # type: ignore[arg-type]
            prime_ack_timeout_sec=1.0,
            policy_state_timeout_sec=1.0,
            reset_state_timeout_sec=1.0,
            manual_takeover_enabled=True,
            execution=execution,
        )
        try:
            adapter.begin_stage("rotate_button")
            adapter.publish(np.zeros((1, 7), dtype=np.float64))
            old_generation = adapter.rollout_generation()
            adapter.request_manual_takeover()

            self.assertLess(events.index("producer_cancel"), events.index("generation:2"))
            self.assertLess(events.index("generation:2"), events.index("policy_enabled:False"))
            self.assertLess(events.index("policy_enabled:False"), events.index("manual_takeover"))
            self.assertLess(events.index("manual_takeover"), events.index("wait:WAIT_TEACH"))
            with self.assertRaises(PolicyCancelled):
                adapter.ensure_rollout_generation(old_generation)
        finally:
            adapter.close()

    def test_policy_only_adapter_rejects_manual_takeover_before_transport_call(self) -> None:
        events: list[str] = []
        adapter = RightHitlControlAdapter(
            self.config,
            Observation(),
            NeverGate(),  # type: ignore[arg-type]
            RecordingTransport(events),  # type: ignore[arg-type]
            prime_ack_timeout_sec=1.0,
            policy_state_timeout_sec=1.0,
            reset_state_timeout_sec=1.0,
            manual_takeover_enabled=False,
            execution=RecordingExecution(events),
        )
        try:
            with self.assertRaisesRegex(Exception, "unavailable"):
                adapter.request_manual_takeover()
            self.assertNotIn("manual_takeover", events)
        finally:
            adapter.close()

    @staticmethod
    def _capture_publish_failure(
        core: ActionExecutionCore, actions: np.ndarray, result: list[BaseException]
    ) -> None:
        try:
            core.publish(actions)
        except BaseException as error:
            result.append(error)


if __name__ == "__main__":
    unittest.main()
