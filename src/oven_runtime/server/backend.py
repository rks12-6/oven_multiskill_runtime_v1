from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from oven_runtime.common.protocol import RequestKind


@dataclass(frozen=True)
class BackendIdentity:
    backend: str
    skill: str
    model_id: str
    asset_hashes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class BackendResult:
    actions: Any
    noise_hash: str
    metadata: dict[str, Any] = field(default_factory=dict)


class PolicyBackend(Protocol):
    def prepare(self, skill: str) -> BackendIdentity:
        """Load one skill and return the observed model identity."""

    def infer(self, observation: dict[str, Any], request_kind: RequestKind) -> BackendResult:
        """Run exactly one stateful policy inference."""

    def reset_prng(self, seed: int) -> None:
        """Initialize the policy PRNG for a formal trial."""

    def close(self) -> None:
        """Release backend resources."""
