from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from oven_runtime.common.audit_schema import StageEvidence
from oven_runtime.common.errors import ErrorCode, fault
from oven_runtime.common.protocol import TRIAL_ID_PATTERN
from oven_runtime.edge.contracts import CheckerResult, GateResult


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
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass
class ActiveEdgeRun:
    run_id: str
    directory: Path
    planned_skills: tuple[str, ...]
    completed_stages: int = 0
    active_stage_directory: Path | None = None


class EdgeAuditStore:
    """Agilex-owned run evidence. Completion is derived only from structured gates."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._active: ActiveEdgeRun | None = None
        self._lock = threading.RLock()

    def begin(self, run_id: str, planned_skills: tuple[str, ...]) -> Path:
        if TRIAL_ID_PATTERN.fullmatch(run_id) is None:
            raise fault(ErrorCode.INVALID_FIELD_VALUE, "run_id format is invalid")
        with self._lock:
            if self._active is not None:
                raise fault(ErrorCode.STATE_REJECTED, "an edge run is already active")
            directory = self.root / run_id
            try:
                directory.mkdir(parents=True, exist_ok=False)
            except FileExistsError as exc:
                raise fault(ErrorCode.AUDIT_COMMIT_FAILED, "edge run directory already exists") from exc
            self._active = ActiveEdgeRun(run_id, directory, planned_skills)
            self._write_manifest("ACTIVE", "CREATED")
            return directory

    def set_pipeline_state(self, state: str) -> None:
        with self._lock:
            self._require_active()
            self._write_manifest("ACTIVE", state)

    def start_stage(self, index: int, skill: str, trial_id: str) -> Path:
        with self._lock:
            active = self._require_active()
            if active.active_stage_directory is not None:
                raise fault(ErrorCode.STATE_REJECTED, "an edge stage is already active")
            if index != active.completed_stages:
                raise fault(ErrorCode.STATE_REJECTED, "stage index is not the next planned stage")
            if index >= len(active.planned_skills) or active.planned_skills[index] != skill:
                raise fault(ErrorCode.STATE_REJECTED, "stage skill does not match the fixed run plan")
            directory = active.directory / "stages" / f"{index:02d}_{skill}"
            directory.mkdir(parents=True, exist_ok=False)
            active.active_stage_directory = directory
            _atomic_json(
                directory / "stage_evidence.json",
                {
                    "schema_version": 1,
                    "status": "ACTIVE",
                    "stage_index": index,
                    "skill": skill,
                    "trial_id": trial_id,
                },
            )
            return directory

    def record_rollout(self, row: dict[str, Any]) -> None:
        with self._lock:
            directory = self._require_stage()
            try:
                with (directory / "rollout.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError as exc:
                raise fault(ErrorCode.AUDIT_COMMIT_FAILED, "cannot append edge rollout evidence") from exc

    def complete_stage(
        self,
        *,
        evidence: StageEvidence,
        gate: GateResult,
        checker: CheckerResult,
        server_summary: dict[str, Any],
    ) -> dict[str, Any]:
        if not evidence.completed:
            raise fault(ErrorCode.AUDIT_COMMIT_FAILED, "cannot commit an incomplete stage as completed")
        with self._lock:
            active = self._require_active()
            directory = self._require_stage()
            value = {
                "schema_version": 1,
                "status": "COMPLETED",
                "evidence": {
                    **asdict(evidence),
                    "joint_gate": evidence.joint_gate.value,
                    "checker": evidence.checker.value,
                    "server_audit": evidence.server_audit.value,
                    "edge_audit": evidence.edge_audit.value,
                },
                "joint_gate_result": asdict(gate),
                "checker_result": asdict(checker),
                "server_summary": server_summary,
            }
            _atomic_json(directory / "stage_evidence.json", value)
            active.completed_stages += 1
            active.active_stage_directory = None
            self._write_manifest("ACTIVE", "HANDOFF")
            return value

    def fail(self, *, state: str, error_code: str, message: str) -> dict[str, Any]:
        with self._lock:
            active = self._require_active()
            if active.active_stage_directory is not None:
                _atomic_json(
                    active.active_stage_directory / "stage_failure.json",
                    {
                        "schema_version": 1,
                        "status": "FAILED",
                        "error_code": error_code,
                        "message": message,
                    },
                )
                active.active_stage_directory = None
            result = self._write_manifest("FAILED", state, error_code=error_code, message=message)
            self._active = None
            return result

    def complete(self) -> dict[str, Any]:
        with self._lock:
            active = self._require_active()
            if active.active_stage_directory is not None or active.completed_stages != len(active.planned_skills):
                raise fault(ErrorCode.AUDIT_COMMIT_FAILED, "run cannot complete before every stage is committed")
            result = self._write_manifest("COMPLETED", "COMPLETED")
            self._active = None
            return result

    def _write_manifest(self, status: str, state: str, **extra: Any) -> dict[str, Any]:
        active = self._require_active()
        manifest = {
            "schema_version": 1,
            "run_id": active.run_id,
            "status": status,
            "pipeline_state": state,
            "planned_skills": list(active.planned_skills),
            "completed_stages": active.completed_stages,
            **extra,
        }
        _atomic_json(active.directory / "run_manifest.json", manifest)
        return manifest

    def _require_active(self) -> ActiveEdgeRun:
        if self._active is None:
            raise fault(ErrorCode.STATE_REJECTED, "no active edge run")
        return self._active

    def _require_stage(self) -> Path:
        active = self._require_active()
        if active.active_stage_directory is None:
            raise fault(ErrorCode.STATE_REJECTED, "no active edge stage")
        return active.active_stage_directory
