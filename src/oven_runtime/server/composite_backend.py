from __future__ import annotations

import gc
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import flax
import jax
import jax.numpy as jnp
import numpy as np

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - server target is Python 3.11
    import tomli as tomllib

from openpi import transforms
from openpi.models import model as model_lib
from openpi.policies import policy as policy_lib
from openpi.training import checkpoints as checkpoints_lib
from openpi.training import config as config_lib

from oven_runtime.common.hashing import stable_tree_hash
from oven_runtime.common.protocol import RequestKind
from oven_runtime.server.backend import BackendIdentity, BackendResult


@dataclass(frozen=True)
class CompositeSkillSpec:
    name: str
    config_name: str
    checkpoint: Path
    adapter: Path
    manifest: Path


@dataclass(frozen=True)
class CompositeBackendProfile:
    shared_params: Path
    skills: dict[str, CompositeSkillSpec]


def _under(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("backend profile paths must be normalized relative paths")
    resolved_root = root.expanduser().resolve()
    resolved = (resolved_root / candidate).resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ValueError("backend profile path escapes its configured root")
    return resolved


def load_composite_profile(path: Path, *, artifact_root: Path, checkpoint_root: Path) -> CompositeBackendProfile:
    with path.expanduser().resolve().open("rb") as stream:
        raw = tomllib.load(stream)
    if raw.get("schema_version") != 1:
        raise ValueError("composite backend profile schema_version must be 1")
    skills_raw = raw.get("skills")
    if not isinstance(skills_raw, Mapping) or not skills_raw:
        raise ValueError("composite backend profile must define skills")
    skills: dict[str, CompositeSkillSpec] = {}
    for name, value in skills_raw.items():
        if not isinstance(name, str) or not isinstance(value, Mapping):
            raise ValueError("composite backend skill entries are invalid")
        fields = ("config_name", "checkpoint_relative_path", "adapter_relative_path", "manifest_relative_path")
        if any(not isinstance(value.get(field), str) or not value[field] for field in fields):
            raise ValueError(f"composite backend skill {name} has invalid fields")
        skills[name] = CompositeSkillSpec(
            name=name,
            config_name=value["config_name"],
            checkpoint=_under(checkpoint_root, value["checkpoint_relative_path"]),
            adapter=_under(artifact_root, value["adapter_relative_path"]),
            manifest=_under(artifact_root, value["manifest_relative_path"]),
        )
    shared_relative = raw.get("shared_params_relative_path")
    if not isinstance(shared_relative, str) or not shared_relative:
        raise ValueError("shared_params_relative_path is required")
    return CompositeBackendProfile(shared_params=_under(artifact_root, shared_relative), skills=skills)


def _flat(tree: Mapping[str, Any]) -> dict[str, Any]:
    return flax.traverse_util.flatten_dict(tree, sep="/")


def _unflat(tree: Mapping[str, Any]) -> dict[str, Any]:
    return flax.traverse_util.unflatten_dict(tree, sep="/")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class OpenPiCompositeBackend:
    """OpenPI backend rebuilt inside this project from a shared base and exact skill deltas."""

    def __init__(self, profile: CompositeBackendProfile) -> None:
        self.profile = profile
        if not profile.shared_params.is_dir():
            raise FileNotFoundError(f"shared parameter tree is missing: {profile.shared_params}")
        self._shared = _flat(
            model_lib.restore_params(profile.shared_params, restore_type=np.ndarray, dtype=jnp.bfloat16)
        )
        self._active_spec: CompositeSkillSpec | None = None
        self._active_config: Any = None
        self._policy: policy_lib.Policy | None = None
        self._noise_rng = jax.random.key(0)

    def prepare(self, skill: str) -> BackendIdentity:
        spec = self.profile.skills.get(skill)
        if spec is None:
            raise ValueError(f"unknown composite skill: {skill}")
        self._validate_assets(spec)
        adapter = _flat(model_lib.restore_params(spec.adapter, restore_type=np.ndarray, dtype=jnp.bfloat16))
        overlap = set(adapter) & set(self._shared)
        merged = dict(self._shared)
        merged.update(adapter)
        config = config_lib.get_config(spec.config_name)
        model = config.model.load(_unflat(merged))
        data_config = config.data.create(config.assets_dirs, config.model)
        if data_config.asset_id is None:
            raise ValueError(f"OpenPI data config has no asset_id for {skill}")
        stats = checkpoints_lib.load_norm_stats(spec.checkpoint / "assets", data_config.asset_id)
        policy = policy_lib.Policy(
            model,
            transforms=[
                transforms.InjectDefaultPrompt(None),
                *data_config.data_transforms.inputs,
                transforms.Normalize(stats, use_quantiles=data_config.use_quantile_norm),
                *data_config.model_transforms.inputs,
            ],
            output_transforms=[
                *data_config.model_transforms.outputs,
                transforms.Unnormalize(stats, use_quantiles=data_config.use_quantile_norm),
                *data_config.data_transforms.outputs,
            ],
            metadata={**(config.policy_metadata or {}), "oven_runtime_skill": skill},
        )
        self._active_spec = spec
        self._active_config = config
        self._policy = policy
        self._noise_rng = jax.random.key(0)
        del adapter, merged, model
        gc.collect()
        return BackendIdentity(
            backend="openpi-composite",
            skill=skill,
            model_id=f"{spec.config_name}:{spec.checkpoint.name}",
            asset_hashes={
                "adapter_manifest": _sha256_file(spec.manifest),
                "adapter_overwrite_leaves": str(len(overlap)),
            },
        )

    def infer(self, observation: dict[str, Any], request_kind: RequestKind) -> BackendResult:
        if self._policy is None or self._active_config is None or self._active_spec is None:
            raise RuntimeError("composite backend is not prepared")
        self._noise_rng, sample_key = jax.random.split(self._noise_rng)
        model_config = self._active_config.model
        noise = np.asarray(
            jax.random.normal(
                sample_key,
                (model_config.action_horizon, model_config.action_dim),
                dtype=jnp.float32,
            )
        )
        result = self._policy.infer(observation, noise=noise)
        if not isinstance(result, Mapping) or "actions" not in result:
            raise RuntimeError("OpenPI policy response has no actions")
        return BackendResult(
            actions=np.asarray(result["actions"]),
            noise_hash=stable_tree_hash(noise),
            metadata={"request_kind": request_kind.value, "skill": self._active_spec.name},
        )

    def reset_prng(self, seed: int) -> None:
        self._noise_rng = jax.random.key(seed)
        if self._policy is not None:
            self._policy._rng = jax.random.key(seed)  # noqa: SLF001 - OpenPI exposes no public reset API.

    def close(self) -> None:
        self._policy = None
        self._active_config = None
        self._active_spec = None
        self._shared.clear()
        gc.collect()

    @staticmethod
    def _validate_assets(spec: CompositeSkillSpec) -> None:
        if not spec.checkpoint.is_dir() or not spec.adapter.is_dir() or not spec.manifest.is_file():
            raise FileNotFoundError(f"composite assets are incomplete for {spec.name}")
        manifest = json.loads(spec.manifest.read_text(encoding="utf-8"))
        if manifest.get("artifact_type") != "exact_composite_adapter":
            raise ValueError(f"adapter manifest identity is invalid for {spec.name}")
        if manifest.get("exact_parameter_recomposition") is not True:
            raise ValueError(f"adapter manifest lacks exact recomposition evidence for {spec.name}")
        expected_checkpoint = spec.checkpoint / "params"
        if Path(str(manifest.get("full_checkpoint", ""))).resolve() != expected_checkpoint:
            raise ValueError(f"adapter manifest checkpoint does not match the backend profile for {spec.name}")
        if Path(str(manifest.get("adapter", ""))).resolve() != spec.adapter:
            raise ValueError(f"adapter manifest path does not match the backend profile for {spec.name}")


def validate_composite_assets(profile: CompositeBackendProfile) -> dict[str, Any]:
    if not profile.shared_params.is_dir():
        raise FileNotFoundError(f"shared parameter tree is missing: {profile.shared_params}")
    skills: dict[str, Any] = {}
    for name, spec in profile.skills.items():
        OpenPiCompositeBackend._validate_assets(spec)
        manifest = json.loads(spec.manifest.read_text(encoding="utf-8"))
        skills[name] = {
            "config_name": spec.config_name,
            "checkpoint": str(spec.checkpoint),
            "adapter": str(spec.adapter),
            "manifest_sha256": _sha256_file(spec.manifest),
            "adapter_leaf_count": manifest.get("adapter_leaf_count"),
            "exact_parameter_recomposition": manifest.get("exact_parameter_recomposition"),
        }
    return {"shared_params": str(profile.shared_params), "skills": skills}
