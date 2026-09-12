from __future__ import annotations

import math
from collections.abc import Iterable

from oven_runtime.v2.contracts import ArmPairProfile, EpisodeConfig, HitlSkillBinding, ResetProfile


def validate_profile(
    *,
    arm_pairs: tuple[ArmPairProfile, ...],
    reset_profiles: tuple[ResetProfile, ...],
    bindings: tuple[HitlSkillBinding, ...],
    episode: EpisodeConfig,
) -> None:
    _unique_names(arm_pairs, "arm pair")
    _unique_names(reset_profiles, "reset profile")
    _unique_names(bindings, "skill binding", attribute="skill_name")

    arm_pair_names = {profile.name for profile in arm_pairs}
    reset_profile_names = {profile.name for profile in reset_profiles}
    for profile in arm_pairs:
        _validate_arm_pair(profile)
    for profile in reset_profiles:
        _validate_reset(profile)
    for binding in bindings:
        if binding.arm_pair not in arm_pair_names:
            raise ValueError(f"binding {binding.skill_name} references unknown arm pair {binding.arm_pair}")
        if binding.reset_profile not in reset_profile_names:
            raise ValueError(f"binding {binding.skill_name} references unknown reset profile {binding.reset_profile}")
        arm_pair = next(profile for profile in arm_pairs if profile.name == binding.arm_pair)
        if binding.allow_hitl and not arm_pair.hitl_enabled:
            raise ValueError(f"binding {binding.skill_name} enables HITL on non-HITL arm pair {arm_pair.name}")
    if episode.same_episode_resume:
        raise ValueError("same_episode_resume=true is unsupported in the HITL v2 MVP")


def _unique_names(values: Iterable[object], kind: str, attribute: str = "name") -> None:
    names = [getattr(value, attribute) for value in values]
    if any(not isinstance(name, str) or not name for name in names):
        raise ValueError(f"{kind} name must be a non-empty string")
    if len(names) != len(set(names)):
        raise ValueError(f"duplicate {kind} name")


def _validate_arm_pair(profile: ArmPairProfile) -> None:
    if profile.side not in {"left", "right"}:
        raise ValueError(f"arm pair {profile.name} has invalid side")
    _required_text(profile.execution_arm, f"arm pair {profile.name}.execution_arm")
    _required_topic(profile.execution_feedback_topic, f"arm pair {profile.name}.execution_feedback_topic")
    _required_topic(profile.policy_input_topic, f"arm pair {profile.name}.policy_input_topic")
    _required_topic(profile.final_command_topic, f"arm pair {profile.name}.final_command_topic")
    _optional_topic(profile.operator_feedback_topic, f"arm pair {profile.name}.operator_feedback_topic")
    _optional_topic(profile.execution_status_topic, f"arm pair {profile.name}.execution_status_topic")
    _optional_topic(profile.operator_status_topic, f"arm pair {profile.name}.operator_status_topic")
    _optional_topic(profile.execution_enable_service, f"arm pair {profile.name}.execution_enable_service")
    _optional_topic(profile.operator_enable_service, f"arm pair {profile.name}.operator_enable_service")
    if profile.hitl_enabled and not profile.operator_arm:
        raise ValueError(f"HITL arm pair {profile.name} requires operator_arm")
    if profile.hitl_enabled and not profile.policy_input_topic:
        raise ValueError(f"HITL arm pair {profile.name} requires policy_input_topic")
    if profile.hitl_enabled:
        _required_optional_topic(
            profile.hitl_state_topic, f"HITL arm pair {profile.name}.hitl_state_topic"
        )
        _required_optional_topic(
            profile.policy_enable_service, f"HITL arm pair {profile.name}.policy_enable_service"
        )
        _required_optional_topic(profile.reset_service, f"HITL arm pair {profile.name}.reset_service")
        _positive_optional_number(
            profile.policy_state_timeout_sec,
            f"HITL arm pair {profile.name}.policy_state_timeout_sec",
        )
        _positive_optional_number(
            profile.reset_state_timeout_sec,
            f"HITL arm pair {profile.name}.reset_state_timeout_sec",
        )


def _validate_reset(profile: ResetProfile) -> None:
    if profile.side not in {"left", "right"}:
        raise ValueError(f"reset profile {profile.name} has invalid side")
    if len(profile.target_positions) != 7:
        raise ValueError(f"reset profile {profile.name}.target_positions must be exactly 7D")
    if not all(math.isfinite(value) for value in profile.target_positions):
        raise ValueError(f"reset profile {profile.name}.target_positions must be finite")
    for name, value in (
        ("tolerance", profile.tolerance),
        ("duration_sec", profile.duration_sec),
        ("verify_timeout_sec", profile.verify_timeout_sec),
        ("settle_sec", profile.settle_sec),
    ):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"reset profile {profile.name}.{name} must be positive and finite")


def _required_text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")


def _required_topic(value: str, field: str) -> None:
    _required_text(value, field)
    if not value.startswith("/"):
        raise ValueError(f"{field} must be an absolute ROS name")


def _optional_topic(value: str | None, field: str) -> None:
    if value is not None:
        _required_topic(value, field)


def _required_optional_topic(value: str | None, field: str) -> None:
    if value is None:
        raise ValueError(f"{field} must be configured")
    _required_topic(value, field)


def _positive_optional_number(value: float | None, field: str) -> None:
    if value is None or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{field} must be positive and finite")
