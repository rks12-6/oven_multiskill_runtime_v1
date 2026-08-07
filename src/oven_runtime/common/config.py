from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from oven_runtime.common.errors import ErrorCode, fault


@dataclass(frozen=True)
class RuntimeRoots:
    checkpoint: Path
    asset: Path
    runtime: Path
    openpi: Path

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> RuntimeRoots:
        values = os.environ if environment is None else environment
        names = {
            "checkpoint": "OVEN_CHECKPOINT_ROOT",
            "asset": "OVEN_ASSET_ROOT",
            "runtime": "OVEN_RUNTIME_ROOT",
            "openpi": "OVEN_OPENPI_ROOT",
        }
        missing = [variable for variable in names.values() if not values.get(variable, "").strip()]
        if missing:
            raise fault(ErrorCode.CONFIG_INVALID, "required root variables are missing", missing=sorted(missing))
        return cls(**{field: Path(values[variable]).expanduser().resolve() for field, variable in names.items()})


def resolve_under_root(root: Path, relative: str) -> Path:
    """Resolve a configured relative path without permitting absolute paths or `..` escape."""

    raw = Path(relative)
    if raw.is_absolute():
        raise fault(ErrorCode.CONFIG_INVALID, "configured resource path must be relative", value=relative)
    root_resolved = root.expanduser().resolve()
    candidate = (root_resolved / raw).resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise fault(ErrorCode.PATH_OUTSIDE_ROOT, "configured path escapes its resource root", value=relative) from exc
    return candidate

