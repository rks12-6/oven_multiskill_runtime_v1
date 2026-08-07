from __future__ import annotations

import threading
import time
from typing import Any

import numpy as np

from oven_runtime.common.hashing import stable_tree_hash
from oven_runtime.common.protocol import RequestKind
from oven_runtime.server.backend import BackendIdentity, BackendResult


class FakePolicyBackend:
    """Deterministic backend used to prove runtime safety without GPU or ROS."""

    def __init__(self, *, delay_sec: float = 0.0, action_shape: tuple[int, int] = (4, 7)) -> None:
        self.delay_sec = delay_sec
        self.action_shape = action_shape
        self.prepared_skill: str | None = None
        self.prepare_count = 0
        self.infer_count = 0
        self.concurrent_calls = 0
        self.max_concurrent_calls = 0
        self.started = threading.Event()
        self._counter_lock = threading.Lock()
        self._rng = np.random.default_rng(0)

    def prepare(self, skill: str) -> BackendIdentity:
        self.prepared_skill = skill
        self.prepare_count += 1
        self._rng = np.random.default_rng(0)
        return BackendIdentity(backend="fake", skill=skill, model_id=f"fake:{skill}:v1")

    def infer(self, observation: dict[str, Any], request_kind: RequestKind) -> BackendResult:
        if self.prepared_skill is None:
            raise RuntimeError("fake backend is not prepared")
        with self._counter_lock:
            self.concurrent_calls += 1
            self.max_concurrent_calls = max(self.max_concurrent_calls, self.concurrent_calls)
            self.infer_count += 1
        self.started.set()
        try:
            if self.delay_sec:
                time.sleep(self.delay_sec)
            noise = self._rng.standard_normal(self.action_shape, dtype=np.float32)
            skill_bias = float(sum(self.prepared_skill.encode("utf-8")) % 101) / 1000.0
            actions = noise + skill_bias
            return BackendResult(
                actions=actions,
                noise_hash=stable_tree_hash(noise),
                metadata={"request_kind": request_kind.value, "observation_keys": sorted(observation)},
            )
        finally:
            with self._counter_lock:
                self.concurrent_calls -= 1

    def reset_prng(self, seed: int) -> None:
        self._rng = np.random.default_rng(seed)

    def close(self) -> None:
        self.prepared_skill = None

