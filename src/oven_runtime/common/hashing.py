from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
from typing import Any

import numpy as np


def _framed(digest: "hashlib._Hash", label: str, payload: bytes) -> None:
    label_bytes = label.encode("utf-8")
    digest.update(len(label_bytes).to_bytes(4, "big"))
    digest.update(label_bytes)
    digest.update(len(payload).to_bytes(8, "big"))
    digest.update(payload)


def stable_tree_hash(value: Any) -> str:
    """Hash nested data including leaf path, kind, dtype, shape, and value."""

    digest = hashlib.sha256()

    def walk(item: Any, path: str) -> None:
        if isinstance(item, Mapping):
            _framed(digest, path, b"mapping")
            for key in sorted(item, key=str):
                walk(item[key], f"{path}/{key}")
            return
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            _framed(digest, path, f"sequence:{len(item)}".encode("ascii"))
            for index, child in enumerate(item):
                walk(child, f"{path}/{index}")
            return
        if isinstance(item, bytes):
            _framed(digest, path, b"bytes:" + item)
            return
        if isinstance(item, (str, int, float, bool)) or item is None:
            payload = json.dumps(item, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
            _framed(digest, path, b"scalar:" + type(item).__name__.encode("ascii") + b":" + payload)
            return
        array = np.ascontiguousarray(np.asarray(item))
        metadata = json.dumps(
            {"dtype": str(array.dtype), "shape": list(array.shape)},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        _framed(digest, path, b"array:" + metadata + b":" + array.tobytes())

    walk(value, "root")
    return digest.hexdigest()

