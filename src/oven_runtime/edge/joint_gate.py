from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import numpy as np

from oven_runtime.edge.contracts import GateResult


@dataclass(frozen=True)
class JointGateProfile:
    arm: str
    targets: tuple[tuple[float, ...], ...]
    check_indices: tuple[int, ...]
    tolerance: tuple[float, ...]
    departure_threshold: float
    min_publish_step: int
    window_samples: int
    max_spread: float
    arming_indices: tuple[int, ...] = ()
    arming_min_abs_position: tuple[float, ...] = ()

    def arrays(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        targets = np.asarray(self.targets, dtype=np.float64)
        indices = np.asarray(self.check_indices, dtype=np.int64)
        tolerance = np.asarray(self.tolerance, dtype=np.float64)
        if targets.ndim != 2 or targets.shape[1] != 7 or targets.shape[0] == 0:
            raise ValueError("joint gate targets must have shape (templates, 7)")
        if indices.ndim != 1 or tolerance.shape != indices.shape or indices.size == 0:
            raise ValueError("joint gate indices and tolerance are invalid")
        if self.arm not in {"left", "right"}:
            raise ValueError("joint gate arm must be left or right")
        arming_indices = np.asarray(self.arming_indices, dtype=np.int64)
        arming_thresholds = np.asarray(self.arming_min_abs_position, dtype=np.float64)
        if arming_indices.shape != arming_thresholds.shape:
            raise ValueError("joint gate arming indices and thresholds must align")
        if arming_indices.size and (
            np.any(arming_indices < 0)
            or np.any(arming_indices >= 7)
            or np.unique(arming_indices).size != arming_indices.size
            or not np.isfinite(arming_thresholds).all()
            or np.any(arming_thresholds <= 0)
        ):
            raise ValueError("joint gate arming limits are invalid")
        if (
            not np.isfinite(targets).all()
            or not np.isfinite(tolerance).all()
            or np.any(indices < 0)
            or np.any(indices >= 7)
            or np.unique(indices).size != indices.size
            or np.any(tolerance <= 0)
        ):
            raise ValueError("joint gate targets, indices, or tolerance are invalid")
        if (
            not np.isfinite(self.departure_threshold)
            or not np.isfinite(self.max_spread)
            or self.departure_threshold <= 0
            or self.min_publish_step < 0
            or self.window_samples <= 0
            or self.max_spread <= 0
        ):
            raise ValueError("joint gate limits are invalid")
        return targets, indices, tolerance


class OnlineJointGate:
    """Departure-then-stable-return gate used while action rows are being published."""

    def __init__(self, profiles: Mapping[str, JointGateProfile]) -> None:
        self._profiles = dict(profiles)
        if not self._profiles:
            raise ValueError("at least one joint gate profile is required")
        for profile in self._profiles.values():
            profile.arrays()
        maximum_window = max(profile.window_samples for profile in self._profiles.values())
        self._samples: deque[np.ndarray] = deque(maxlen=maximum_window)
        self._skill: str | None = None
        self._departed = False
        self._armed = False
        self._lock = threading.RLock()

    def begin(self, skill: str) -> None:
        if skill not in self._profiles:
            raise ValueError(f"no joint gate profile for skill {skill}")
        with self._lock:
            self._skill = skill
            self._departed = False
            self._armed = False
            self._samples.clear()

    def update(self, arm: str, position: np.ndarray) -> None:
        sample = np.asarray(position, dtype=np.float64)
        if sample.shape != (7,) or not np.isfinite(sample).all():
            return
        with self._lock:
            if self._skill is not None and self._profiles[self._skill].arm == arm:
                self._samples.append(sample.copy())
                profile = self._profiles[self._skill]
                if profile.arming_indices:
                    indices = np.asarray(profile.arming_indices, dtype=np.int64)
                    thresholds = np.asarray(profile.arming_min_abs_position, dtype=np.float64)
                    if np.all(np.abs(sample[indices]) >= thresholds):
                        self._armed = True
                else:
                    self._armed = True

    def reached(self, skill: str, publish_step: int) -> bool:
        profile, targets, indices, tolerance, samples = self._snapshot(skill)
        if samples.size == 0:
            return False
        with self._lock:
            armed = self._armed
        if not armed:
            return False
        latest_errors = np.abs(samples[-1, indices] - targets[:, indices])
        departure_tolerance = np.maximum(tolerance, profile.departure_threshold)
        target_is_near = np.all(latest_errors < departure_tolerance, axis=1)
        with self._lock:
            if not self._departed:
                if not np.any(target_is_near):
                    self._departed = True
                    latest = self._samples[-1]
                    self._samples.clear()
                    self._samples.append(latest)
                return False
            departed = self._departed
        if not departed or publish_step < profile.min_publish_step or samples.shape[0] < profile.window_samples:
            return False
        return self._window_result(profile, targets, indices, tolerance, samples).passed

    def evaluate(self, skill: str) -> GateResult:
        profile, targets, indices, tolerance, samples = self._snapshot(skill)
        with self._lock:
            departed = self._skill == skill and self._departed
            armed = self._skill == skill and self._armed
        if not armed:
            return GateResult(False, samples.shape[0], float("inf"), float("inf"), "arming_not_observed")
        if not departed:
            return GateResult(False, samples.shape[0], float("inf"), float("inf"), "departure_not_observed")
        if samples.shape[0] < profile.window_samples:
            return GateResult(False, samples.shape[0], float("inf"), float("inf"), "insufficient_joint_samples")
        return self._window_result(profile, targets, indices, tolerance, samples)

    def _snapshot(
        self, skill: str
    ) -> tuple[JointGateProfile, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        profile = self._profiles[skill]
        targets, indices, tolerance = profile.arrays()
        with self._lock:
            if self._skill != skill or not self._samples:
                samples = np.empty((0, 7), dtype=np.float64)
            else:
                samples = np.asarray(tuple(self._samples), dtype=np.float64)[-profile.window_samples :]
        return profile, targets, indices, tolerance, samples

    @staticmethod
    def _window_result(
        profile: JointGateProfile,
        targets: np.ndarray,
        indices: np.ndarray,
        tolerance: np.ndarray,
        samples: np.ndarray,
    ) -> GateResult:
        checked = samples[:, indices]
        median = np.median(checked, axis=0)
        spread = np.ptp(checked, axis=0)
        errors = np.abs(median - targets[:, indices])
        matches = np.where(np.all(errors <= tolerance, axis=1))[0]
        maximum_spread = float(np.max(spread))
        if matches.size and maximum_spread <= profile.max_spread:
            maximum_error = float(np.max(errors[int(matches[0])]))
            return GateResult(True, samples.shape[0], maximum_error, maximum_spread, "stable_target_window")
        nearest_error = float(np.min(np.max(errors, axis=1)))
        return GateResult(False, samples.shape[0], nearest_error, maximum_spread, "joint_window_not_stable")


@dataclass
class WindowedJointGate:
    sample_joints: Callable[[], np.ndarray]
    target: np.ndarray
    tolerance: np.ndarray
    max_spread: float
    required_samples: int
    max_samples: int

    def evaluate(self, skill: str) -> GateResult:
        del skill
        target = np.asarray(self.target, dtype=np.float64)
        tolerance = np.asarray(self.tolerance, dtype=np.float64)
        if target.ndim != 1 or tolerance.shape != target.shape:
            raise ValueError("joint gate target and tolerance must be equal one-dimensional shapes")
        if self.max_spread <= 0 or self.required_samples <= 0 or self.max_samples < self.required_samples:
            raise ValueError("joint gate limits are invalid")
        window: list[np.ndarray] = []
        observed_max_error = float("inf")
        observed_max_spread = float("inf")
        for sample_index in range(1, self.max_samples + 1):
            sample = np.asarray(self.sample_joints(), dtype=np.float64)
            if sample.shape != target.shape or not np.isfinite(sample).all():
                window.clear()
                continue
            window.append(sample)
            window = window[-self.required_samples :]
            if len(window) < self.required_samples:
                continue
            stacked = np.stack(window)
            observed_max_error = float(np.max(np.abs(stacked - target)))
            observed_max_spread = float(np.max(np.ptp(stacked, axis=0)))
            within_target = bool(np.all(np.abs(stacked - target) <= tolerance))
            if within_target and observed_max_spread <= self.max_spread:
                return GateResult(
                    passed=True,
                    samples=sample_index,
                    max_error=observed_max_error,
                    max_spread=observed_max_spread,
                    reason="stable_target_window",
                )
        return GateResult(
            passed=False,
            samples=self.max_samples,
            max_error=observed_max_error,
            max_spread=observed_max_spread,
            reason="joint_window_not_stable",
        )
