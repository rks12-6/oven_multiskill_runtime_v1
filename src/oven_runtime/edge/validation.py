from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from oven_runtime.common.errors import ErrorCode, fault
from oven_runtime.edge.contracts import Observation


@dataclass(frozen=True)
class ObservationValidator:
    max_age_ms: float
    monotonic_ns: Callable[[], int] = time.monotonic_ns

    def validate(self, observation: Observation) -> dict[str, Any]:
        if not math.isfinite(self.max_age_ms) or self.max_age_ms <= 0:
            raise ValueError("max_age_ms must be positive")
        if not isinstance(observation.payload, dict) or not observation.payload:
            raise fault(ErrorCode.INVALID_FIELD_VALUE, "observation payload must be a non-empty dictionary")
        age_ns = self.monotonic_ns() - observation.captured_at_monotonic_ns
        if age_ns < 0 or age_ns > int(self.max_age_ms * 1_000_000):
            raise fault(
                ErrorCode.OBSERVATION_STALE,
                "observation is outside the permitted freshness window",
            )
        return observation.payload


@dataclass(frozen=True)
class ActionValidator:
    action_dimension: int
    chunk_rows: int
    max_absolute_value: float

    def validate(self, actions: Any) -> np.ndarray:
        if (
            self.action_dimension <= 0
            or self.chunk_rows <= 0
            or not math.isfinite(self.max_absolute_value)
            or self.max_absolute_value <= 0
        ):
            raise ValueError("action validation limits must be positive")
        array = np.asarray(actions)
        accepted_dimensions = (self.action_dimension, self.action_dimension * 2)
        if array.ndim != 2 or array.shape[0] != self.chunk_rows or array.shape[1] not in accepted_dimensions:
            raise fault(
                ErrorCode.ACTION_INVALID,
                "action chunk shape is invalid",
                expected_rows=self.chunk_rows,
                accepted_dimensions=list(accepted_dimensions),
                received_shape=list(array.shape),
            )
        if array.dtype.kind not in "fiu" or not np.isfinite(array).all():
            raise fault(ErrorCode.ACTION_INVALID, "action chunk must contain finite numeric values")
        # Both established single-arm clients use the first seven columns when
        # OpenPI wraps their local 7D output in a 14D ALOHA action. The ROS
        # executor selects the physical arm from the skill profile.
        projected = array[:, : self.action_dimension]
        if float(np.max(np.abs(projected))) > self.max_absolute_value:
            raise fault(ErrorCode.ACTION_INVALID, "action chunk exceeds the configured absolute limit")
        return np.asarray(projected, dtype=np.float32)
