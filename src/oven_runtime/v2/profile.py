from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from oven_runtime.v2.contracts import (
    ArmPairProfile,
    EpisodeConfig,
    HitlControlMode,
    HitlSkillBinding,
    ResetProfile,
)
from oven_runtime.v2.validation import validate_profile


@dataclass(frozen=True)
class HitlV2Profile:
    arm_pairs: tuple[ArmPairProfile, ...]
    reset_profiles: tuple[ResetProfile, ...]
    bindings: tuple[HitlSkillBinding, ...]
    episode: EpisodeConfig

    def arm_pair(self, name: str) -> ArmPairProfile:
        return _find(self.arm_pairs, name, "arm pair")

    def reset_profile(self, name: str) -> ResetProfile:
        return _find(self.reset_profiles, name, "reset profile")

    def binding(self, skill_name: str) -> HitlSkillBinding:
        return _find(self.bindings, skill_name, "skill binding", attribute="skill_name")


def load_hitl_v2_profile(path: Path) -> HitlV2Profile:
    with path.open("rb") as stream:
        raw = tomllib.load(stream)
    if raw.get("schema_version") != 1:
        raise ValueError("HITL v2 profile schema_version must be 1")
    arm_pairs = tuple(_arm_pair(value) for value in _table_list(raw, "arm_pairs"))
    reset_profiles = tuple(_reset_profile(value) for value in _table_list(raw, "reset_profiles"))
    bindings = tuple(_binding(value) for value in _table_list(raw, "skill_bindings"))
    episode_raw = _mapping(raw, "episode")
    episode = EpisodeConfig(
        reset_before=_boolean(episode_raw, "reset_before"),
        allow_takeover=_boolean(episode_raw, "allow_takeover"),
        same_episode_resume=_boolean(episode_raw, "same_episode_resume"),
        capture_correction=_boolean(episode_raw, "capture_correction"),
        reset_after_correction=_boolean(episode_raw, "reset_after_correction"),
    )
    validate_profile(
        arm_pairs=arm_pairs,
        reset_profiles=reset_profiles,
        bindings=bindings,
        episode=episode,
    )
    return HitlV2Profile(arm_pairs=arm_pairs, reset_profiles=reset_profiles, bindings=bindings, episode=episode)


def _arm_pair(value: Mapping[str, Any]) -> ArmPairProfile:
    return ArmPairProfile(
        name=_string(value, "name"),
        side=_string(value, "side"),
        execution_arm=_string(value, "execution_arm"),
        operator_arm=_optional_string(value, "operator_arm"),
        execution_feedback_topic=_string(value, "execution_feedback_topic"),
        operator_feedback_topic=_optional_string(value, "operator_feedback_topic"),
        policy_input_topic=_string(value, "policy_input_topic"),
        final_command_topic=_string(value, "final_command_topic"),
        other_front_command_topic=_optional_string(value, "other_front_command_topic"),
        execution_status_topic=_optional_string(value, "execution_status_topic"),
        operator_status_topic=_optional_string(value, "operator_status_topic"),
        execution_enable_service=_optional_string(value, "execution_enable_service"),
        operator_enable_service=_optional_string(value, "operator_enable_service"),
        hitl_state_topic=_optional_string(value, "hitl_state_topic"),
        policy_enable_service=_optional_string(value, "policy_enable_service"),
        policy_prime_ready_service=_optional_string(value, "policy_prime_ready_service"),
        reset_service=_optional_string(value, "reset_service"),
        prime_ack_timeout_sec=_optional_number(value, "prime_ack_timeout_sec"),
        policy_state_timeout_sec=_optional_number(value, "policy_state_timeout_sec"),
        reset_state_timeout_sec=_optional_number(value, "reset_state_timeout_sec"),
        hitl_enabled=_boolean(value, "hitl_enabled"),
        hitl_mode=_optional_hitl_mode(value),
    )


def _reset_profile(value: Mapping[str, Any]) -> ResetProfile:
    return ResetProfile(
        name=_string(value, "name"),
        side=_string(value, "side"),
        target_positions=_number_tuple(value, "target_positions"),
        tolerance=_number(value, "tolerance"),
        duration_sec=_number(value, "duration_sec"),
        verify_timeout_sec=_number(value, "verify_timeout_sec"),
        settle_sec=_number(value, "settle_sec"),
    )


def _binding(value: Mapping[str, Any]) -> HitlSkillBinding:
    return HitlSkillBinding(
        skill_name=_string(value, "skill_name"),
        arm_pair=_string(value, "arm_pair"),
        reset_profile=_string(value, "reset_profile"),
        allow_hitl=_boolean(value, "allow_hitl"),
    )


def _find(values: tuple[Any, ...], name: str, kind: str, attribute: str = "name") -> Any:
    for value in values:
        if getattr(value, attribute) == name:
            return value
    raise KeyError(f"unknown {kind}: {name}")


def _table_list(value: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    result = value.get(key)
    if not isinstance(result, list) or not result or not all(isinstance(item, Mapping) for item in result):
        raise ValueError(f"profile field {key} must be a non-empty table array")
    return result


def _mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    result = value.get(key)
    if not isinstance(result, Mapping):
        raise ValueError(f"profile field {key} must be a table")
    return result


def _string(value: Mapping[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ValueError(f"profile field {key} must be a non-empty string")
    return result


def _optional_string(value: Mapping[str, Any], key: str) -> str | None:
    if key not in value:
        return None
    return _string(value, key)


def _boolean(value: Mapping[str, Any], key: str) -> bool:
    result = value.get(key)
    if not isinstance(result, bool):
        raise ValueError(f"profile field {key} must be a boolean")
    return result


def _number(value: Mapping[str, Any], key: str) -> float:
    result = value.get(key)
    if not isinstance(result, (int, float)) or isinstance(result, bool) or not math.isfinite(result):
        raise ValueError(f"profile field {key} must be a finite number")
    return float(result)


def _optional_number(value: Mapping[str, Any], key: str) -> float | None:
    if key not in value:
        return None
    return _number(value, key)


def _optional_hitl_mode(value: Mapping[str, Any]) -> HitlControlMode | None:
    if "hitl_mode" not in value:
        return None
    raw = _string(value, "hitl_mode")
    try:
        return HitlControlMode(raw)
    except ValueError as error:
        raise ValueError("hitl_mode must be policy_only or full_hitl") from error


def _number_tuple(value: Mapping[str, Any], key: str) -> tuple[float, ...]:
    result = value.get(key)
    if not isinstance(result, list):
        raise ValueError(f"profile field {key} must be an array")
    return tuple(_number({key: item}, key) for item in result)
