from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from oven_runtime.edge.joint_gate import OnlineJointGate
from oven_runtime.edge.profile import load_agilex_profile


class AgilexProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project_root = Path(__file__).resolve().parents[1]

    def test_checked_in_profile_is_complete_and_relative(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = load_agilex_profile(
                self.project_root / "config" / "agilex_oven_v1.toml",
                run_id="profile_test_001",
                runtime_root=Path(directory),
            )
        self.assertEqual(
            tuple(stage.skill for stage in profile.plan.stages),
            ("open_door", "transport_food", "close_door", "rotate_button"),
        )
        self.assertEqual(profile.action_dimension, 7)
        self.assertTrue(all(not Path(spec.model_relative_path).is_absolute() for spec in profile.checkers.values()))
        self.assertEqual(profile.ros.skill_arms["rotate_button"], "right")
        self.assertNotIn("rotate_button", profile.checkers)
        self.assertFalse(profile.plan.stages[-1].checker_required)

    def test_online_gate_requires_departure_then_stable_return(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = load_agilex_profile(
                self.project_root / "config" / "agilex_oven_v1.toml",
                run_id="profile_test_002",
                runtime_root=Path(directory),
            )
        gate = OnlineJointGate(profile.gates)
        skill = "transport_food"
        gate_profile = profile.gates[skill]
        target = np.asarray(gate_profile.targets[0], dtype=np.float64)
        gate.begin(skill)
        for _ in range(10):
            gate.update("left", target)
        self.assertFalse(gate.reached(skill, gate_profile.min_publish_step))
        self.assertFalse(gate.evaluate(skill).passed)
        departed = target.copy()
        departed[np.asarray(gate_profile.check_indices)] += 0.5
        gate.update("left", departed)
        self.assertFalse(gate.reached(skill, gate_profile.min_publish_step))
        for _ in range(gate_profile.window_samples):
            gate.update("left", target)
        self.assertTrue(gate.reached(skill, gate_profile.min_publish_step))
        self.assertTrue(gate.evaluate(skill).passed)

    def test_rotate_gate_requires_right_arm_arming_excursion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            profile = load_agilex_profile(
                self.project_root / "config" / "agilex_oven_v1.toml",
                run_id="profile_test_003",
                runtime_root=Path(directory),
            )
        skill = "rotate_button"
        gate_profile = profile.gates[skill]
        gate = OnlineJointGate(profile.gates)
        target = np.asarray(gate_profile.targets[0], dtype=np.float64)
        gate.begin(skill)
        gate.update("left", np.full(7, 2.0))
        gate.update("right", target + 0.5)
        for _ in range(gate_profile.window_samples):
            gate.update("right", target)
        self.assertFalse(gate.reached(skill, gate_profile.min_publish_step))
        armed = target.copy()
        armed[1] = 1.1
        gate.update("right", armed)
        self.assertFalse(gate.reached(skill, gate_profile.min_publish_step))
        for _ in range(gate_profile.window_samples):
            gate.update("right", target)
        self.assertTrue(gate.reached(skill, gate_profile.min_publish_step))


if __name__ == "__main__":
    unittest.main()
