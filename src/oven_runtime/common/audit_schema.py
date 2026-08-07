from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class EvidenceStatus(str, Enum):
    NOT_RUN = "NOT_RUN"
    PASSED = "PASSED"
    FAILED = "FAILED"
    INCOMPLETE = "INCOMPLETE"


@dataclass(frozen=True)
class StageEvidence:
    rollout_exit_code: int | None
    rollout_end_reason: str | None
    rollout_end_reason_approved: bool
    joint_gate: EvidenceStatus
    checker: EvidenceStatus
    server_audit: EvidenceStatus
    edge_audit: EvidenceStatus

    @property
    def completed(self) -> bool:
        return (
            self.rollout_exit_code == 0
            and self.rollout_end_reason is not None
            and self.rollout_end_reason_approved
            and self.joint_gate is EvidenceStatus.PASSED
            and self.checker is EvidenceStatus.PASSED
            and self.server_audit is EvidenceStatus.PASSED
            and self.edge_audit is EvidenceStatus.PASSED
        )

