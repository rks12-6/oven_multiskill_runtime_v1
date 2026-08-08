from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class CheckerSpec:
    model_relative_path: str
    model_sha256: str
    success_class: str
    threshold: float
    preprocessing: str = "resize"

    def validate(self) -> None:
        if len(self.model_sha256) != 64 or any(character not in "0123456789abcdef" for character in self.model_sha256):
            raise ValueError("checker model_sha256 must be a lowercase SHA256 hex digest")
        if not self.success_class or not 0 < self.threshold <= 1:
            raise ValueError("checker success class and threshold are invalid")
        if self.preprocessing not in {"resize", "resize_center_crop"}:
            raise ValueError("checker preprocessing is invalid")


@dataclass(frozen=True)
class RosBridgeConfig:
    prompts: Mapping[str, str]
    reset_targets: Mapping[str, tuple[float, ...]]
    skill_arms: Mapping[str, str]
    lock_directory: Path
    left_joint_topic: str = "/puppet/joint_left"
    right_joint_topic: str = "/puppet/joint_right"
    left_command_topic: str = "/joint_left_states"
    right_command_topic: str = "/joint_right_states"
    left_camera_topic: str = "/camera_l/color/image_raw"
    right_camera_topic: str = "/camera_r/color/image_raw"
    front_camera_topic: str = "/camera_f/color/image_raw"
    image_size: int = 224
    max_message_age_sec: float = 0.5
    preflight_timeout_sec: float = 5.0
    publish_hz: float = 30.0
    chunk_transition_steps: int = 50
    chunk_transition_hz: float = 200.0
    max_row_delta: tuple[float, ...] = (0.15, 0.15, 0.15, 0.15, 0.15, 0.15, 0.08)
    reset_duration_sec: float = 4.0
    reset_tolerance: float = 0.03
    reset_verify_timeout_sec: float = 3.0
    stop_hold_repetitions: int = 3

    def validate(self) -> None:
        if set(self.prompts) != set(self.reset_targets) or set(self.prompts) != set(self.skill_arms):
            raise ValueError("ROS prompts, reset targets, and skill arms must cover the same skills")
        if any(arm not in {"left", "right"} for arm in self.skill_arms.values()):
            raise ValueError("every skill arm must be left or right")
        for target in self.reset_targets.values():
            values = np.asarray(target, dtype=np.float64)
            if values.shape != (7,) or not np.isfinite(values).all():
                raise ValueError("every reset target must contain seven finite values")
        if len(self.max_row_delta) != 7 or any(
            not math.isfinite(value) or value <= 0 for value in self.max_row_delta
        ):
            raise ValueError("joint command limits must contain seven positive finite values")
        scalar_limits = (
            self.max_message_age_sec,
            self.preflight_timeout_sec,
            self.publish_hz,
            self.chunk_transition_hz,
            self.reset_duration_sec,
            self.reset_tolerance,
            self.reset_verify_timeout_sec,
        )
        if (
            self.image_size <= 0
            or any(not math.isfinite(value) for value in scalar_limits)
            or self.max_message_age_sec <= 0
            or self.preflight_timeout_sec <= 0
            or self.publish_hz <= 0
            or self.chunk_transition_steps <= 0
            or self.chunk_transition_hz <= 0
            or self.reset_duration_sec <= 0
            or self.reset_tolerance <= 0
            or self.reset_verify_timeout_sec <= 0
            or self.stop_hold_repetitions <= 0
        ):
            raise ValueError("ROS bridge timing and safety limits must be positive")
