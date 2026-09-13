from __future__ import annotations

import math
import unittest
from dataclasses import replace
from pathlib import Path

from oven_runtime.v2.contracts import HitlControlMode
from oven_runtime.v2.profile import HitlV2Profile, load_hitl_v2_profile
from oven_runtime.v2.validation import validate_profile


ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "config" / "hitl_v2.toml"


class HitlV2ProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.profile = load_hitl_v2_profile(PROFILE_PATH)

    def test_left_skills_bind_to_legacy_left_reset(self) -> None:
        for skill_name in ("open_door", "transport_food", "close_door"):
            binding = self.profile.binding(skill_name)
            self.assertEqual(binding.arm_pair, "left_legacy")
            self.assertEqual(binding.reset_profile, "left_reset")
            self.assertFalse(binding.allow_hitl)

    def test_rotate_button_binds_to_right_hitl_reset(self) -> None:
        binding = self.profile.binding("rotate_button")
        self.assertEqual(binding.arm_pair, "right_hitl")
        self.assertEqual(binding.reset_profile, "right_reset")
        self.assertTrue(binding.allow_hitl)

    def test_right_hitl_topics_match_existing_boundary(self) -> None:
        right = self.profile.arm_pair("right_hitl")
        self.assertEqual(right.policy_input_topic, "/hitl/policy_joint_right_cmd")
        self.assertEqual(right.final_command_topic, "/joint_right_states")
        self.assertEqual(right.execution_enable_service, "/hitl/right_front/enable_srv")
        self.assertEqual(right.operator_enable_service, "/hitl/right_rear/enable_srv")
        self.assertEqual(right.hitl_state_topic, "/hitl/state")
        self.assertEqual(right.policy_enable_service, "/hitl/enable_policy")
        self.assertEqual(right.reset_service, "/hitl/start_reset")
        self.assertEqual(right.hitl_mode, HitlControlMode.FULL_HITL)

    def test_policy_only_profile_has_no_operator_rear_contract(self) -> None:
        policy_only = self.profile.arm_pair("right_policy_only")
        self.assertEqual(policy_only.hitl_mode, HitlControlMode.POLICY_ONLY)
        self.assertIsNone(policy_only.operator_arm)
        self.assertIsNone(policy_only.operator_feedback_topic)
        self.assertIsNone(policy_only.operator_status_topic)

    def test_reset_targets_are_strictly_seven_dimensional(self) -> None:
        self.assertTrue(all(len(profile.target_positions) == 7 for profile in self.profile.reset_profiles))

    def test_overlay_does_not_define_mature_skill_content(self) -> None:
        text = PROFILE_PATH.read_text(encoding="utf-8")
        for forbidden in ("prompt", "gate", "checker", "max_chunks", "action_steps"):
            self.assertNotIn(forbidden, text)

    def test_invalid_profiles_fail_closed(self) -> None:
        left = self.profile.arm_pair("left_legacy")
        right = self.profile.arm_pair("right_hitl")
        left_reset = self.profile.reset_profile("left_reset")
        rotate = self.profile.binding("rotate_button")
        cases = (
            replace(self.profile, reset_profiles=(replace(left_reset, target_positions=(0.0,) * 6),)),
            replace(self.profile, arm_pairs=self.profile.arm_pairs + (left,)),
            replace(self.profile, bindings=(replace(rotate, arm_pair="missing_pair"),)),
            replace(self.profile, bindings=(replace(rotate, reset_profile="missing_reset"),)),
            replace(self.profile, bindings=(replace(rotate, arm_pair="left_legacy"),)),
            replace(self.profile, arm_pairs=(replace(right, operator_arm=None), left)),
            replace(self.profile, arm_pairs=(replace(self.profile.arm_pair("right_policy_only"), operator_arm="right_rear"), left)),
            replace(self.profile, arm_pairs=(replace(right, policy_input_topic=""), left)),
            replace(self.profile, arm_pairs=(replace(right, hitl_state_topic=None), left)),
            replace(self.profile, episode=replace(self.profile.episode, same_episode_resume=True)),
        )
        for invalid in cases:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    _validate(invalid)

    def test_nan_reset_target_fails_closed(self) -> None:
        left_reset = self.profile.reset_profile("left_reset")
        invalid = replace(self.profile, reset_profiles=(replace(left_reset, target_positions=(math.nan,) * 7),))
        with self.assertRaises(ValueError):
            _validate(invalid)


def _validate(profile: HitlV2Profile) -> None:
    validate_profile(
        arm_pairs=profile.arm_pairs,
        reset_profiles=profile.reset_profiles,
        bindings=profile.bindings,
        episode=profile.episode,
    )


if __name__ == "__main__":
    unittest.main()
