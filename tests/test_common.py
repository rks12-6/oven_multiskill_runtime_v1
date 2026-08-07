from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from oven_runtime.common.audit_schema import EvidenceStatus, StageEvidence
from oven_runtime.common.config import RuntimeRoots, resolve_under_root
from oven_runtime.common.errors import ErrorCode, RuntimeFault
from oven_runtime.common.hashing import stable_tree_hash
from oven_runtime.common.state import RuntimeState, ensure_runtime_transition


class ConfigTests(unittest.TestCase):
    def test_runtime_roots_require_all_environment_variables(self) -> None:
        with self.assertRaises(RuntimeFault) as raised:
            RuntimeRoots.from_environment({})
        self.assertEqual(raised.exception.code, ErrorCode.CONFIG_INVALID)

    def test_relative_resource_stays_under_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = resolve_under_root(root, "checkers/open/model.pth")
            self.assertEqual(result, root / "checkers" / "open" / "model.pth")

    def test_absolute_and_parent_escape_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for value in (str(root / "model.pth"), "../outside/model.pth"):
                with self.subTest(value=value), self.assertRaises(RuntimeFault):
                    resolve_under_root(root, value)


class HashingTests(unittest.TestCase):
    def test_array_shape_and_dtype_affect_hash(self) -> None:
        raw = np.array([1, 2, 3, 4], dtype=np.int32)
        self.assertNotEqual(stable_tree_hash(raw), stable_tree_hash(raw.reshape(2, 2)))
        self.assertNotEqual(stable_tree_hash(raw), stable_tree_hash(raw.astype(np.float32)))


class StateAndEvidenceTests(unittest.TestCase):
    def test_illegal_runtime_transition_is_rejected(self) -> None:
        with self.assertRaises(RuntimeFault) as raised:
            ensure_runtime_transition(RuntimeState.STARTING, RuntimeState.READY)
        self.assertEqual(raised.exception.code, ErrorCode.STATE_REJECTED)

    def test_stage_requires_every_structured_gate(self) -> None:
        complete = StageEvidence(
            rollout_exit_code=0,
            rollout_end_reason="joint_rest_detected",
            rollout_end_reason_approved=True,
            joint_gate=EvidenceStatus.PASSED,
            checker=EvidenceStatus.PASSED,
            server_audit=EvidenceStatus.PASSED,
            edge_audit=EvidenceStatus.PASSED,
        )
        self.assertTrue(complete.completed)
        incomplete = StageEvidence(
            rollout_exit_code=0,
            rollout_end_reason="joint_rest_detected",
            rollout_end_reason_approved=True,
            joint_gate=EvidenceStatus.PASSED,
            checker=EvidenceStatus.NOT_RUN,
            server_audit=EvidenceStatus.PASSED,
            edge_audit=EvidenceStatus.PASSED,
        )
        self.assertFalse(incomplete.completed)


if __name__ == "__main__":
    unittest.main()

