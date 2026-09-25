from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 on Agilex
    import tomli as tomllib

from oven_runtime.edge.joint_gate import JointGateProfile
from oven_runtime.edge.orchestrator import FIXED_SKILL_ORDER, PipelinePlan, StagePlan
from oven_runtime.edge.profile_types import CheckerSpec, RosBridgeConfig


@dataclass(frozen=True)
class AgilexProfile:
    plan: PipelinePlan
    ros: RosBridgeConfig
    gates: dict[str, JointGateProfile]
    checkers: dict[str, CheckerSpec]
    action_dimension: int
    action_chunk_rows: int
    action_max_absolute_value: float
    observation_max_age_ms: float
    server_host_alias: str
    inference_uri: str
    remote_inference_port: int
    inference_timeout_sec: float
    remote_control_program: str
    remote_runtime_root: Path


def load_agilex_profile(
    path: Path,
    *,
    run_id: str,
    runtime_root: Path,
    selected_skill: str | None = None,
) -> AgilexProfile:
    with path.open("rb") as stream:
        raw = tomllib.load(stream)
    if raw.get("schema_version") != 1:
        raise ValueError("Agilex profile schema_version must be 1")
    server = _mapping(raw, "server")
    ros = _mapping(raw, "ros")
    action = _mapping(raw, "action")
    skills = _mapping(raw, "skills")
    prompts: dict[str, str] = {}
    reset_targets: dict[str, tuple[float, ...]] = {}
    skill_arms: dict[str, str] = {}
    gates: dict[str, JointGateProfile] = {}
    checkers: dict[str, CheckerSpec] = {}
    stages: list[StagePlan] = []
    chunk_rows = _integer(action, "chunk_rows")
    settle_sec = _number(ros, "settle_sec")
    if selected_skill is not None and selected_skill not in FIXED_SKILL_ORDER:
        raise ValueError(f"unknown selected skill: {selected_skill}")
    for skill in FIXED_SKILL_ORDER:
        value = _mapping(skills, skill)
        prompt = _string(value, "prompt")
        reset_target = _float_tuple(value, "reset_target", length=7)
        arm = _string(value, "arm")
        prompts[skill] = prompt
        reset_targets[skill] = reset_target
        skill_arms[skill] = arm
        checker_required = _boolean(value, "checker_required")
        if selected_skill is None or skill == selected_skill:
            stages.append(
                StagePlan(
                    skill=skill,
                    root_seed=_integer(value, "root_seed"),
                    max_chunks=_integer(value, "max_chunks"),
                    action_steps=_integer(value, "max_chunks") * chunk_rows,
                    settle_sec=settle_sec,
                    # A standalone skill has no preceding handoff pose, so it
                    # always starts from that skill's configured reset target.
                    reset_before=True if selected_skill is not None else _boolean(value, "reset_before"),
                    checker_required=checker_required,
                )
            )
        gate = _mapping(value, "gate")
        targets_value = gate.get("targets")
        if not isinstance(targets_value, list) or not targets_value:
            raise ValueError(f"skills.{skill}.gate.targets must be a non-empty array")
        targets = tuple(_float_tuple_value(item, length=7) for item in targets_value)
        gates[skill] = JointGateProfile(
            arm=arm,
            targets=targets,
            check_indices=tuple(_integer_list(gate, "check_indices")),
            tolerance=_float_tuple(gate, "tolerance"),
            departure_threshold=_number(gate, "departure_threshold"),
            min_publish_step=_integer(gate, "min_publish_step"),
            window_samples=_integer(gate, "window_samples"),
            max_spread=_number(gate, "max_spread"),
            arming_indices=tuple(_optional_integer_list(gate, "arming_indices")),
            arming_min_abs_position=_optional_float_tuple(gate, "arming_min_abs_position"),
        )
        if checker_required:
            checker = _mapping(value, "checker")
            checkers[skill] = CheckerSpec(
                model_relative_path=_string(checker, "model_relative_path"),
                model_sha256=_string(checker, "model_sha256"),
                success_class=_string(checker, "success_class"),
                threshold=_number(checker, "threshold"),
                preprocessing=_string(checker, "preprocessing"),
            )

    profile = AgilexProfile(
        plan=PipelinePlan(run_id=run_id, stages=tuple(stages)),
        ros=RosBridgeConfig(
            prompts=prompts,
            reset_targets=reset_targets,
            skill_arms=skill_arms,
            lock_directory=runtime_root.expanduser().resolve() / "locks",
            left_joint_topic=_string(ros, "left_joint_topic"),
            right_joint_topic=_string(ros, "right_joint_topic"),
            left_command_topic=_string(ros, "left_command_topic"),
            right_command_topic=_string(ros, "right_command_topic"),
            left_camera_topic=_string(ros, "left_camera_topic"),
            right_camera_topic=_string(ros, "right_camera_topic"),
            front_camera_topic=_string(ros, "front_camera_topic"),
            image_size=_integer(ros, "image_size"),
            max_message_age_sec=_number(ros, "max_message_age_sec"),
            preflight_timeout_sec=_number(ros, "preflight_timeout_sec"),
            publish_hz=_number(ros, "publish_hz"),
            chunk_transition_steps=_integer(action, "chunk_transition_steps"),
            chunk_transition_hz=_number(action, "chunk_transition_hz"),
            max_row_delta=_float_tuple(action, "max_row_delta", length=7),
            reset_duration_sec=_number(ros, "reset_duration_sec"),
            reset_tolerance=_number(ros, "reset_tolerance"),
            reset_verify_timeout_sec=_number(ros, "reset_verify_timeout_sec"),
            stop_hold_repetitions=_integer(ros, "stop_hold_repetitions"),
        ),
        gates=gates,
        checkers=checkers,
        action_dimension=_integer(action, "dimension"),
        action_chunk_rows=chunk_rows,
        action_max_absolute_value=_number(action, "max_absolute_value"),
        observation_max_age_ms=_number(ros, "observation_max_age_ms"),
        server_host_alias=_string(server, "ssh_host_alias"),
        inference_uri=_string(server, "local_inference_uri"),
        remote_inference_port=_integer(server, "remote_inference_port"),
        inference_timeout_sec=_number(server, "inference_timeout_sec"),
        remote_control_program=_string(server, "remote_control_program"),
        remote_runtime_root=Path(_string(server, "remote_runtime_root")).expanduser(),
    )
    profile.plan.validate()
    profile.ros.validate()
    for gate in profile.gates.values():
        gate.arrays()
    for checker in profile.checkers.values():
        checker.validate()
    _validate_profile(profile)
    return profile


def _validate_profile(profile: AgilexProfile) -> None:
    if profile.action_dimension <= 0 or profile.action_chunk_rows <= 0:
        raise ValueError("action.dimension must be positive")
    if not math.isfinite(profile.action_max_absolute_value) or profile.action_max_absolute_value <= 0:
        raise ValueError("action.max_absolute_value must be a positive finite number")
    if not math.isfinite(profile.observation_max_age_ms) or profile.observation_max_age_ms <= 0:
        raise ValueError("ros.observation_max_age_ms must be a positive finite number")
    if not 1 <= profile.remote_inference_port <= 65535:
        raise ValueError("server.remote_inference_port is invalid")
    if profile.inference_timeout_sec <= 0:
        raise ValueError("server.inference_timeout_sec must be positive")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", profile.server_host_alias) is None:
        raise ValueError("server.ssh_host_alias must be one safe SSH token")
    if not profile.remote_control_program.startswith("/") or ".." in Path(profile.remote_control_program).parts:
        raise ValueError("server.remote_control_program must be a normalized absolute path")
    if not profile.remote_runtime_root.is_absolute() or ".." in profile.remote_runtime_root.parts:
        raise ValueError("server.remote_runtime_root must be a normalized absolute path")
    try:
        uri = urlsplit(profile.inference_uri)
        port = uri.port
    except ValueError as exc:
        raise ValueError("server.local_inference_uri has an invalid port") from exc
    if (
        uri.scheme != "ws"
        or uri.hostname not in {"127.0.0.1", "::1"}
        or port is None
        or uri.username is not None
        or uri.password is not None
        or uri.path not in {"", "/"}
        or uri.query
        or uri.fragment
    ):
        raise ValueError("server.local_inference_uri must be a plain loopback ws:// URI")


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


def _boolean(value: Mapping[str, Any], key: str) -> bool:
    result = value.get(key)
    if not isinstance(result, bool):
        raise ValueError(f"profile field {key} must be a boolean")
    return result


def _integer(value: Mapping[str, Any], key: str) -> int:
    result = value.get(key)
    if not isinstance(result, int) or isinstance(result, bool):
        raise ValueError(f"profile field {key} must be an integer")
    return result


def _number(value: Mapping[str, Any], key: str) -> float:
    result = value.get(key)
    if not isinstance(result, (int, float)) or isinstance(result, bool):
        raise ValueError(f"profile field {key} must be numeric")
    number = float(result)
    if not math.isfinite(number):
        raise ValueError(f"profile field {key} must be finite")
    return number


def _string_list(value: Mapping[str, Any], key: str) -> list[str]:
    result = value.get(key)
    if not isinstance(result, list) or not result or not all(isinstance(item, str) and item for item in result):
        raise ValueError(f"profile field {key} must be a non-empty string array")
    return result


def _integer_list(value: Mapping[str, Any], key: str) -> list[int]:
    result = value.get(key)
    if not isinstance(result, list) or not result:
        raise ValueError(f"profile field {key} must be a non-empty integer array")
    if any(not isinstance(item, int) or isinstance(item, bool) for item in result):
        raise ValueError(f"profile field {key} must contain only integers")
    return result


def _optional_integer_list(value: Mapping[str, Any], key: str) -> list[int]:
    if key not in value:
        return []
    result = value[key]
    if not isinstance(result, list) or any(not isinstance(item, int) or isinstance(item, bool) for item in result):
        raise ValueError(f"profile field {key} must contain only integers")
    return result


def _float_tuple(value: Mapping[str, Any], key: str, *, length: int | None = None) -> tuple[float, ...]:
    return _float_tuple_value(value.get(key), length=length)


def _float_tuple_value(value: Any, *, length: int | None = None) -> tuple[float, ...]:
    if not isinstance(value, list) or (length is not None and len(value) != length):
        raise ValueError("profile numeric array has an invalid length")
    if any(not isinstance(item, (int, float)) or isinstance(item, bool) for item in value):
        raise ValueError("profile numeric array contains a non-number")
    result = tuple(float(item) for item in value)
    if any(not math.isfinite(item) for item in result):
        raise ValueError("profile numeric array contains a non-finite value")
    return result


def _optional_float_tuple(value: Mapping[str, Any], key: str) -> tuple[float, ...]:
    if key not in value:
        return ()
    return _float_tuple_value(value[key])
