from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import numpy as np
import torch
import torch.nn as nn
from PIL import Image
from torchvision import models, transforms

from oven_runtime.common.config import resolve_under_root
from oven_runtime.common.errors import ErrorCode, fault
from oven_runtime.edge.contracts import CheckerResult
from oven_runtime.edge.profile_types import CheckerSpec


class TorchResnetChecker:
    """Loads one verified checker at a time and evaluates a fresh Agilex front image."""

    def __init__(
        self,
        *,
        asset_root: Path,
        specs: Mapping[str, CheckerSpec],
        capture_front_rgb: Callable[[], tuple[np.ndarray, int]],
        max_image_age_sec: float = 1.0,
        device: str | None = None,
    ) -> None:
        self.asset_root = asset_root.expanduser().resolve()
        self.specs = dict(specs)
        for spec in self.specs.values():
            spec.validate()
        if max_image_age_sec <= 0:
            raise ValueError("max_image_age_sec must be positive")
        self.capture_front_rgb = capture_front_rgb
        self.max_image_age_sec = max_image_age_sec
        self.device = torch.device(device or ("cuda" if torch.cuda.is_available() else "cpu"))
        self._active_skill: str | None = None
        self._active_model: nn.Module | None = None
        self._classes: tuple[str, ...] = ()
        self._transform: Any = None

    def evaluate(self, skill: str) -> CheckerResult:
        spec = self.specs.get(skill)
        if spec is None:
            raise fault(ErrorCode.CHECKER_FAILED, "checker is not configured for the requested skill")
        self._prepare(skill, spec)
        rgb, captured_at = self.capture_front_rgb()
        age_ns = time.monotonic_ns() - captured_at
        if age_ns < 0 or age_ns > int(self.max_image_age_sec * 1_000_000_000):
            raise fault(ErrorCode.OBSERVATION_STALE, "checker front image is stale")
        if rgb.ndim != 3 or rgb.shape[2] != 3 or rgb.dtype != np.uint8:
            raise fault(ErrorCode.CHECKER_FAILED, "checker input must be an RGB uint8 image")
        assert self._active_model is not None and self._transform is not None
        tensor = self._transform(Image.fromarray(rgb)).unsqueeze(0).to(self.device)
        with torch.inference_mode():
            logits = self._active_model(tensor)
            probabilities = torch.softmax(logits, dim=1)[0].detach().cpu().numpy()
        predicted_index = int(np.argmax(probabilities))
        predicted_class = self._classes[predicted_index]
        confidence = float(probabilities[predicted_index])
        passed = predicted_class == spec.success_class and confidence >= spec.threshold
        return CheckerResult(
            passed=passed,
            score=confidence,
            threshold=spec.threshold,
            asset_id=spec.model_sha256,
            reason=f"predicted:{predicted_class}",
        )

    def close(self) -> None:
        self._active_model = None
        self._active_skill = None
        self._classes = ()
        self._transform = None
        if self.device.type == "cuda":
            torch.cuda.empty_cache()

    def _prepare(self, skill: str, spec: CheckerSpec) -> None:
        if self._active_skill == skill and self._active_model is not None:
            return
        model_path = resolve_under_root(self.asset_root, spec.model_relative_path)
        if not model_path.is_file():
            raise fault(ErrorCode.CHECKER_FAILED, "checker model asset is missing")
        if _sha256_file(model_path) != spec.model_sha256:
            raise fault(ErrorCode.CHECKER_FAILED, "checker model asset hash does not match its manifest")
        try:
            checkpoint = torch.load(model_path, map_location=self.device, weights_only=True)
        except Exception as exc:
            raise fault(ErrorCode.CHECKER_FAILED, "checker checkpoint cannot be loaded safely") from exc
        if not isinstance(checkpoint, Mapping):
            raise fault(ErrorCode.CHECKER_FAILED, "checker checkpoint is not a mapping")
        classes = checkpoint.get("classes", checkpoint.get("class_names"))
        state_dict = checkpoint.get("model_state_dict")
        if not isinstance(classes, (list, tuple)) or not classes or not all(isinstance(item, str) for item in classes):
            raise fault(ErrorCode.CHECKER_FAILED, "checker checkpoint classes are invalid")
        if spec.success_class not in classes or not isinstance(state_dict, Mapping):
            raise fault(ErrorCode.CHECKER_FAILED, "checker checkpoint contract does not match its profile")
        model = models.resnet18(weights=None)
        model.fc = nn.Linear(model.fc.in_features, len(classes))
        model.load_state_dict(state_dict, strict=True)
        model.to(self.device)
        model.eval()
        input_size = checkpoint.get("input_size", [224, 224])
        mean = checkpoint.get("normalize_mean", [0.485, 0.456, 0.406])
        std = checkpoint.get("normalize_std", [0.229, 0.224, 0.225])
        self.close()
        self._active_skill = skill
        self._active_model = model
        self._classes = tuple(classes)
        preprocessing: list[Any] = []
        if spec.preprocessing == "resize_center_crop":
            preprocessing.extend([transforms.Resize((256, 256)), transforms.CenterCrop(224)])
        else:
            preprocessing.append(transforms.Resize(tuple(input_size)))
        self._transform = transforms.Compose(
            [*preprocessing, transforms.ToTensor(), transforms.Normalize(mean=mean, std=std)]
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
