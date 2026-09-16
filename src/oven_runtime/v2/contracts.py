from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class HitlControlMode(str, Enum):
    """Explicit capability contracts for HITL-controlled execution arms."""

    POLICY_ONLY = "policy_only"
    FULL_HITL = "full_hitl"


@dataclass(frozen=True)
class ArmPairProfile:
    """Hardware/control-topic overlay for one execution/operator arm pair."""

    name: str
    side: str
    execution_arm: str
    operator_arm: str | None
    execution_feedback_topic: str
    operator_feedback_topic: str | None
    policy_input_topic: str
    final_command_topic: str
    other_front_command_topic: str | None
    execution_status_topic: str | None
    operator_status_topic: str | None
    execution_enable_service: str | None
    operator_enable_service: str | None
    hitl_state_topic: str | None
    policy_enable_service: str | None
    policy_prime_ready_service: str | None
    policy_lease_topic: str | None
    physical_takeover_topic: str | None
    reset_service: str | None
    reset_outcome_topic: str | None
    prime_ack_timeout_sec: float | None
    policy_lease_interval_sec: float | None
    policy_progress_timeout_sec: float | None
    policy_state_timeout_sec: float | None
    reset_state_timeout_sec: float | None
    hitl_enabled: bool
    hitl_mode: HitlControlMode | None


@dataclass(frozen=True)
class ResetProfile:
    """Declarative reset target only; this contract sends no commands."""

    name: str
    side: str
    target_positions: tuple[float, ...]
    tolerance: float
    duration_sec: float
    verify_timeout_sec: float
    settle_sec: float


@dataclass(frozen=True)
class HitlSkillBinding:
    """Maps an existing mature skill name to v2 hardware/reset capabilities."""

    skill_name: str
    arm_pair: str
    reset_profile: str
    allow_hitl: bool


@dataclass(frozen=True)
class EpisodeConfig:
    """Episode-level HITL policy. same_episode_resume is intentionally unsupported in MVP."""

    reset_before: bool
    allow_takeover: bool
    same_episode_resume: bool
    capture_correction: bool
    reset_after_correction: bool
