from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np

from oven_runtime.edge.contracts import GateResult, PublishResult
from oven_runtime.edge.profile import load_agilex_profile
from oven_runtime.v2.hitl_control import (
    ControlMode,
    RightHitlControlAdapter,
    adapter_for_skill,
    control_mode_for_skills,
)
from oven_runtime.v2.profile import load_hitl_v2_profile


ROOT = Path(__file__).resolve().parents[1]
MATURE_PROFILE = ROOT / "config" / "agilex_oven_v1.toml"
HITL_PROFILE = ROOT / "config" / "hitl_v2.toml"


class FakeObservation:
    def __init__(self) -> None:
        self.preflight_count = 0

    def preflight(self) -> None:
        self.preflight_count += 1

    def arm_snapshot(self, arm: str) -> tuple[np.ndarray, np.ndarray]:
        if arm != "right":
            raise AssertionError(f"unexpected arm: {arm}")
        return np.zeros(7, dtype=np.float64), np.zeros(7, dtype=np.float64)


class PassingGate:
    def __init__(self) -> None:
        self.result = GateResult(True, 3, 0.0, 0.0, "stable_target_window")
        self.skills: list[str] = []

    def begin(self, skill: str) -> None:
        self.skills.append(skill)

    def reached_result(self, skill: str, publish_step: int) -> GateResult | None:
        del skill, publish_step
        return self.result


class FakeHitlTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object | None]] = []
        self.policy_messages: list[np.ndarray] = []
        self.policy_generations: list[int] = []
        self.policy_cached = False
        self.other_front_hold_messages: list[np.ndarray] = []
        self.created_publisher_topics = (
            "/hitl/policy_joint_right_cmd",
            "/joint_left_states",
        )
        self.wait_failure: BaseException | None = None
        self.closed = False
        self.generation = 0

    def preflight(self) -> None:
        self.calls.append(("preflight", None))

    def set_policy_enabled(self, enabled: bool) -> None:
        if enabled and not self.policy_cached:
            raise RuntimeError("POLICY rejected: policy has not been received")
        self.calls.append(("set_policy_enabled", enabled))

    def start_reset(self) -> None:
        self.calls.append(("start_reset", None))

    def advance_policy_generation(self) -> int:
        self.generation += 1
        self.policy_cached = False
        self.calls.append(("advance_policy_generation", self.generation))
        return self.generation

    def wait_for_policy_prime(self, timeout_sec: float) -> None:
        self.calls.append(("wait_for_policy_prime", timeout_sec))
        if not self.policy_cached:
            raise TimeoutError("policy prime acknowledgement timeout")

    def wait_for_state(
        self, expected: str, timeout_sec: float, *, pending_states: frozenset[str]
    ) -> None:
        self.calls.append(("wait_for_state", (expected, timeout_sec, pending_states)))
        if self.wait_failure is not None:
            raise self.wait_failure

    def publish_policy(self, positions: np.ndarray, generation: int) -> None:
        self.calls.append(("publish_policy", generation))
        self.policy_messages.append(positions.copy())
        self.policy_generations.append(generation)
        self.policy_cached = True

    def publish_other_front_hold(self, positions: np.ndarray) -> None:
        self.calls.append(("publish_other_front_hold", None))
        self.other_front_hold_messages.append(positions.copy())

    def request_manual_takeover(self) -> None:
        self.calls.append(("request_manual_takeover", None))

    def close(self) -> None:
        self.calls.append(("close", None))
        self.closed = True


class RightHitlControlAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.hitl_profile = load_hitl_v2_profile(HITL_PROFILE)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        profile = load_agilex_profile(
            MATURE_PROFILE,
            run_id="right_hitl_001",
            runtime_root=Path(self.temporary.name),
            selected_skill="rotate_button",
        )
        self.config = replace(profile.ros, chunk_transition_steps=1, chunk_transition_hz=1_000_000.0)
        self.observation = FakeObservation()
        self.gate = PassingGate()
        self.transport = FakeHitlTransport()
        right = self.hitl_profile.arm_pair("right_hitl")
        assert right.prime_ack_timeout_sec is not None
        assert right.policy_state_timeout_sec is not None
        assert right.reset_state_timeout_sec is not None
        self.adapter = RightHitlControlAdapter(
            self.config,
            self.observation,
            self.gate,  # type: ignore[arg-type]
            self.transport,
            prime_ack_timeout_sec=right.prime_ack_timeout_sec,
            policy_state_timeout_sec=right.policy_state_timeout_sec,
            reset_state_timeout_sec=right.reset_state_timeout_sec,
        )

    def tearDown(self) -> None:
        self.adapter.close()
        self.temporary.cleanup()

    def test_selection_is_binding_driven(self) -> None:
        self.assertEqual(control_mode_for_skills(self.hitl_profile, ("open_door",)), ControlMode.DIRECT)
        self.assertEqual(control_mode_for_skills(self.hitl_profile, ("rotate_button",)), ControlMode.RIGHT_HITL)
        direct = object()
        right_hitl = object()
        self.assertIs(
            adapter_for_skill(
                self.hitl_profile, "open_door", direct_adapter=direct, right_hitl_adapter=right_hitl
            ),
            direct,
        )
        self.assertIs(
            adapter_for_skill(
                self.hitl_profile, "rotate_button", direct_adapter=direct, right_hitl_adapter=right_hitl
            ),
            right_hitl,
        )
        with self.assertRaises(ValueError):
            control_mode_for_skills(self.hitl_profile, ("open_door", "rotate_button"))

    def test_policy_publish_preserves_other_front_hold_without_final_right_publisher(self) -> None:
        self.adapter.begin_stage("rotate_button")
        self.assertNotIn(("set_policy_enabled", True), self.transport.calls)
        result = self.adapter.publish(np.ones((1, 7), dtype=np.float64))

        self.assertIs(result.gate_result, self.gate.result)
        self.assertEqual(len(self.transport.policy_messages), 2)
        self.assertEqual(len(self.transport.other_front_hold_messages), 1)
        np.testing.assert_array_equal(self.transport.policy_messages[0], np.ones(7))
        np.testing.assert_array_equal(self.transport.policy_messages[1], np.ones(7))
        np.testing.assert_array_equal(self.transport.other_front_hold_messages[0], np.zeros(7))
        self.assertIn("/joint_left_states", self.transport.created_publisher_topics)
        self.assertNotIn("/joint_right_states", self.transport.created_publisher_topics)
        self.assertNotIn(("publish_final_command", None), self.transport.calls)
        self.assertIn(("set_policy_enabled", True), self.transport.calls)
        self.assertLess(
            self.transport.calls.index(("publish_policy", 1)),
            self.transport.calls.index(("wait_for_policy_prime", 1.0)),
        )
        self.assertLess(
            self.transport.calls.index(("wait_for_policy_prime", 1.0)),
            self.transport.calls.index(("set_policy_enabled", True)),
        )
        self.assertIn(("publish_other_front_hold", None), self.transport.calls)
        self.assertIn(("wait_for_state", ("POLICY", 5.0, frozenset({"HOLD"}))), self.transport.calls)

    def test_hold_routing_uses_execution_arm_ownership_not_skill_name(self) -> None:
        generic_transport = FakeHitlTransport()
        generic_config = replace(
            self.config,
            prompts={"future_right_skill": "generic right skill"},
            reset_targets={"future_right_skill": self.config.reset_targets["rotate_button"]},
            skill_arms={"future_right_skill": "right"},
        )
        adapter = RightHitlControlAdapter(
            generic_config,
            self.observation,
            self.gate,  # type: ignore[arg-type]
            generic_transport,
            prime_ack_timeout_sec=1.0,
            policy_state_timeout_sec=5.0,
            reset_state_timeout_sec=15.0,
        )
        try:
            adapter.begin_stage("future_right_skill")
            adapter.publish(np.ones((1, 7), dtype=np.float64))
            self.assertEqual(len(generic_transport.policy_messages), 2)
            self.assertEqual(len(generic_transport.other_front_hold_messages), 1)
            self.assertNotIn("/joint_right_states", generic_transport.created_publisher_topics)
        finally:
            adapter.close()

    def test_policy_timeout_and_unexpected_state_fail_closed(self) -> None:
        for failure in (
            TimeoutError("POLICY timeout"),
            RuntimeError("unexpected HITL state WAIT_TEACH"),
            RuntimeError("unexpected HITL state HUMAN"),
            RuntimeError("HITL entered FAULT"),
        ):
            with self.subTest(failure=failure):
                self.transport.wait_failure = failure
                self.adapter.begin_stage("rotate_button")
                with self.assertRaises(type(failure)) as raised:
                    self.adapter.publish(np.ones((1, 7), dtype=np.float64))
                self.assertIs(raised.exception, failure)

    def test_reset_waits_for_resetting_then_hold(self) -> None:
        self.adapter.reset("rotate_button")
        self.assertEqual(
            self.transport.calls[-3:],
            [
                ("start_reset", None),
                ("wait_for_state", ("RESETTING", 15.0, frozenset({"HOLD"}))),
                ("wait_for_state", ("HOLD", 15.0, frozenset({"RESETTING"}))),
            ],
        )

    def test_reset_timeout_and_fault_fail_closed(self) -> None:
        for failure in (TimeoutError("RESETTING timeout"), RuntimeError("HITL entered FAULT")):
            with self.subTest(failure=failure):
                self.transport.wait_failure = failure
                with self.assertRaises(type(failure)) as raised:
                    self.adapter.reset("rotate_button")
                self.assertIs(raised.exception, failure)

    def test_stop_disables_policy_and_blocks_future_publication(self) -> None:
        self.adapter.begin_stage("rotate_button")
        self.adapter.publish(np.ones((1, 7), dtype=np.float64))
        self.adapter.stop("rollout_complete")
        self.assertIn(("set_policy_enabled", False), self.transport.calls)
        with self.assertRaisesRegex(Exception, "armed stage"):
            self.adapter.publish(np.ones((1, 7), dtype=np.float64))

    def test_close_releases_only_adapter_owned_transport(self) -> None:
        self.adapter.begin_stage("rotate_button")
        self.adapter.publish(np.ones((1, 7), dtype=np.float64))
        self.adapter.close()
        self.assertTrue(self.transport.closed)
        self.assertIn(("set_policy_enabled", False), self.transport.calls)
        self.assertIn(("close", None), self.transport.calls)


if __name__ == "__main__":
    unittest.main()
