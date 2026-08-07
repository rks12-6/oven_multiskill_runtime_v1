from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import threading
from typing import Any
from uuid import uuid4

import numpy as np

from oven_runtime.common.errors import ErrorCode, RuntimeFault, fault


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass
class ActiveTrial:
    trial_id: str
    skill: str
    generation: int
    root_seed: int
    directory: Path
    request_count: int = 0
    first_action_hash: str | None = None
    last_action_hash: str | None = None
    request_rows: list[dict[str, Any]] = field(default_factory=list)


class TrialAuditStore:
    """One active, non-overwritable server-side trial."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._active: ActiveTrial | None = None
        self._lock = threading.RLock()

    @property
    def active(self) -> ActiveTrial | None:
        with self._lock:
            return self._active

    def begin(self, *, trial_id: str, skill: str, generation: int, root_seed: int) -> ActiveTrial:
        with self._lock:
            if self._active is not None:
                raise fault(ErrorCode.TRIAL_ALREADY_ACTIVE, "a server audit trial is already active")
            directory = self.root / trial_id
            try:
                directory.mkdir(parents=True, exist_ok=False)
                manifest = {
                    "schema_version": 1,
                    "trial_id": trial_id,
                    "skill": skill,
                    "generation": generation,
                    "root_seed": root_seed,
                    "status": "ACTIVE",
                }
                _atomic_json(directory / "server_manifest.json", manifest)
            except FileExistsError as exc:
                raise fault(ErrorCode.AUDIT_COMMIT_FAILED, "trial directory already exists", trial_id=trial_id) from exc
            except OSError as exc:
                raise fault(ErrorCode.AUDIT_COMMIT_FAILED, "cannot create server audit trial", trial_id=trial_id) from exc
            self._active = ActiveTrial(trial_id, skill, generation, root_seed, directory)
            return self._active

    def record_request(self, row: dict[str, Any], actions: Any) -> None:
        with self._lock:
            active = self._active
            if active is None:
                raise fault(ErrorCode.TRIAL_REQUIRED, "no active server audit trial")
            try:
                action_hash = str(row["action_hash"])
                if row.get("request_kind") == "infer" and active.first_action_hash is None:
                    action_array = np.asarray(actions)
                    action_path = active.directory / "first_action_chunk.npy"
                    temporary_action = action_path.with_name(f".{action_path.name}.{uuid4().hex}.tmp")
                    try:
                        with temporary_action.open("xb") as stream:
                            np.save(stream, action_array, allow_pickle=False)
                            stream.flush()
                            os.fsync(stream.fileno())
                        temporary_action.replace(action_path)
                    finally:
                        temporary_action.unlink(missing_ok=True)
                    _atomic_json(
                        active.directory / "first_action_chunk.json",
                        {
                            "schema_version": 1,
                            "trial_id": active.trial_id,
                            "sequence": row["sequence"],
                            "shape": list(action_array.shape),
                            "dtype": str(action_array.dtype),
                            "action_hash": action_hash,
                        },
                    )
                request_path = active.directory / "requests.jsonl"
                with request_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError as exc:
                raise fault(ErrorCode.AUDIT_COMMIT_FAILED, "cannot append server request evidence") from exc
            active.request_count += 1
            active.request_rows.append(dict(row))
            if row.get("request_kind") == "infer" and active.first_action_hash is None:
                active.first_action_hash = action_hash
            if row.get("request_kind") == "infer":
                active.last_action_hash = action_hash

    def end(self) -> dict[str, Any]:
        with self._lock:
            active = self._active
            if active is None:
                raise fault(ErrorCode.TRIAL_REQUIRED, "no active server audit trial")
            summary = {
                "schema_version": 1,
                "trial_id": active.trial_id,
                "skill": active.skill,
                "generation": active.generation,
                "root_seed": active.root_seed,
                "status": "COMMITTED",
                "request_count": active.request_count,
                "first_action_hash": active.first_action_hash,
                "last_action_hash": active.last_action_hash,
            }
            try:
                _atomic_json(active.directory / "trial_summary.json", summary)
                _atomic_json(active.directory / "server_manifest.json", summary)
            except (OSError, RuntimeFault) as exc:
                raise fault(ErrorCode.AUDIT_COMMIT_FAILED, "cannot commit server audit trial") from exc
            self._active = None
            return summary

    def abort(self, reason: str) -> dict[str, Any] | None:
        with self._lock:
            active = self._active
            if active is None:
                return None
            summary = {
                "schema_version": 1,
                "trial_id": active.trial_id,
                "skill": active.skill,
                "generation": active.generation,
                "root_seed": active.root_seed,
                "status": "ABORTED",
                "reason": reason,
                "request_count": active.request_count,
            }
            try:
                _atomic_json(active.directory / "server_manifest.json", summary)
            except OSError as exc:
                raise fault(ErrorCode.AUDIT_COMMIT_FAILED, "cannot abort server audit trial") from exc
            self._active = None
            return summary
