from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from oven_runtime.common.errors import ErrorCode, RuntimeFault, fault
from oven_runtime.server.audit import TrialAuditStore
from oven_runtime.server.fake_backend import FakePolicyBackend
from oven_runtime.server.runtime import PolicyRuntime
from tests.helpers import request_payload


class RuntimeHarness:
    def __init__(self, directory: Path, *, backend: FakePolicyBackend | None = None) -> None:
        self.backend = backend or FakePolicyBackend()
        self.audit = TrialAuditStore(directory / "audit")
        self.runtime = PolicyRuntime(backend=self.backend, audit=self.audit)
        self.runtime.prepare_skill("open_door")
        self.runtime.begin_trial(trial_id="run_001_open_door_attempt_1", root_seed=0)
        self.session = self.runtime.open_session()

    def warmup_and_ready(self) -> None:
        response = self.runtime.infer(request_payload(self.session, kind="warmup", sequence=0))
        if response["publishable"]:
            raise AssertionError("warm-up response became publishable")
        self.runtime.reset_prng(0)


class PolicyRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_legacy_request_never_calls_policy(self) -> None:
        harness = RuntimeHarness(self.root)
        before = harness.backend.infer_count
        with self.assertRaises(RuntimeFault) as raised:
            harness.runtime.infer({"state": [0.0] * 7})
        self.assertEqual(raised.exception.code, ErrorCode.MISSING_REQUIRED_FIELD)
        self.assertEqual(harness.backend.infer_count, before)

    def test_wrong_session_token_never_calls_policy(self) -> None:
        harness = RuntimeHarness(self.root)
        payload = request_payload(harness.session, kind="warmup", sequence=0)
        payload["session_token"] = "y" * 43
        with self.assertRaises(RuntimeFault) as raised:
            harness.runtime.infer(payload)
        self.assertEqual(raised.exception.code, ErrorCode.SESSION_INVALID)
        self.assertEqual(harness.backend.infer_count, 0)

    def test_second_session_is_rejected(self) -> None:
        harness = RuntimeHarness(self.root)
        with self.assertRaises(RuntimeFault) as raised:
            harness.runtime.open_session()
        self.assertEqual(raised.exception.code, ErrorCode.SESSION_ALREADY_ACTIVE)

    def test_warmup_is_not_publishable_and_normal_inference_is(self) -> None:
        harness = RuntimeHarness(self.root)
        warmup = harness.runtime.infer(request_payload(harness.session, kind="warmup", sequence=0))
        self.assertFalse(warmup["publishable"])
        self.assertEqual(harness.runtime.status()["state"], "PRNG_RESET_REQUIRED")
        harness.runtime.reset_prng(0)
        rollout = harness.runtime.infer(request_payload(harness.session, kind="infer", sequence=1))
        self.assertTrue(rollout["publishable"])
        self.assertEqual(rollout["sequence"], 1)

    def test_stale_generation_is_rejected_before_policy(self) -> None:
        harness = RuntimeHarness(self.root)
        payload = request_payload(
            harness.session,
            kind="warmup",
            sequence=0,
            generation=harness.session["generation"] + 1,
        )
        with self.assertRaises(RuntimeFault) as raised:
            harness.runtime.infer(payload)
        self.assertEqual(raised.exception.code, ErrorCode.GENERATION_MISMATCH)
        self.assertEqual(harness.backend.infer_count, 0)

    def test_duplicate_sequence_is_rejected_without_second_policy_call(self) -> None:
        harness = RuntimeHarness(self.root)
        harness.warmup_and_ready()
        payload = request_payload(harness.session, kind="infer", sequence=1)
        harness.runtime.infer(payload)
        before = harness.backend.infer_count
        with self.assertRaises(RuntimeFault) as raised:
            harness.runtime.infer(payload)
        self.assertEqual(raised.exception.code, ErrorCode.SEQUENCE_MISMATCH)
        self.assertEqual(harness.backend.infer_count, before)

    def test_two_legitimate_requests_execute_serially(self) -> None:
        backend = FakePolicyBackend(delay_sec=0.08)
        harness = RuntimeHarness(self.root, backend=backend)
        harness.warmup_and_ready()
        backend.started.clear()
        results: dict[int, dict] = {}
        errors: list[BaseException] = []

        def call(sequence: int) -> None:
            try:
                results[sequence] = harness.runtime.infer(
                    request_payload(harness.session, kind="infer", sequence=sequence)
                )
            except BaseException as exc:  # captured and asserted in the parent test thread
                errors.append(exc)

        first = threading.Thread(target=call, args=(1,))
        second = threading.Thread(target=call, args=(2,))
        first.start()
        self.assertTrue(backend.started.wait(timeout=1.0))
        second.start()
        first.join(timeout=2.0)
        second.join(timeout=2.0)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [1, 2])
        self.assertEqual(backend.max_concurrent_calls, 1)

    def test_audit_never_contains_session_token(self) -> None:
        harness = RuntimeHarness(self.root)
        warmup = harness.runtime.infer(request_payload(harness.session, kind="warmup", sequence=0))
        action_path = self.root / "audit" / harness.session["trial_id"] / "first_action_chunk.npy"
        self.assertFalse(action_path.exists())
        self.assertFalse(warmup["publishable"])
        harness.runtime.reset_prng(0)
        harness.runtime.infer(request_payload(harness.session, kind="infer", sequence=1))
        self.assertTrue(action_path.is_file())
        request_file = self.root / "audit" / harness.session["trial_id"] / "requests.jsonl"
        text = request_file.read_text(encoding="utf-8")
        self.assertNotIn(harness.session["session_token"], text)
        rows = [json.loads(line) for line in text.splitlines()]
        self.assertEqual([row["sequence"] for row in rows], [0, 1])

    def test_trial_id_cannot_escape_audit_root(self) -> None:
        backend = FakePolicyBackend()
        runtime = PolicyRuntime(backend=backend, audit=TrialAuditStore(self.root / "audit"))
        runtime.prepare_skill("open_door")
        with self.assertRaises(RuntimeFault) as raised:
            runtime.begin_trial(trial_id="../escape", root_seed=0)
        self.assertEqual(raised.exception.code, ErrorCode.INVALID_FIELD_VALUE)

    def test_transport_failure_after_inference_marks_trial_ambiguous(self) -> None:
        harness = RuntimeHarness(self.root)
        harness.warmup_and_ready()
        harness.runtime.infer(request_payload(harness.session, kind="infer", sequence=1))
        result = harness.runtime.mark_transport_ambiguous(
            session_id=harness.session["session_id"],
            sequence=1,
        )
        self.assertEqual(result["state"], "FAILED")
        self.assertEqual(result["trial"]["status"], "AMBIGUOUS_INFERENCE")
        manifest_path = self.root / "audit" / harness.session["trial_id"] / "server_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "AMBIGUOUS_INFERENCE")
        self.assertEqual(manifest["ambiguous_sequence"], 1)
        with self.assertRaises(RuntimeFault) as raised:
            harness.runtime.infer(request_payload(harness.session, kind="infer", sequence=2))
        self.assertEqual(raised.exception.code, ErrorCode.SESSION_REQUIRED)

    def test_audit_commit_failure_forces_failed_state(self) -> None:
        harness = RuntimeHarness(self.root)
        harness.warmup_and_ready()
        harness.runtime.close_session(harness.session["session_id"])
        with (
            patch.object(
                harness.audit,
                "end",
                side_effect=fault(ErrorCode.AUDIT_COMMIT_FAILED, "injected audit failure"),
            ),
            self.assertRaises(RuntimeFault) as raised,
        ):
            harness.runtime.end_trial()
        self.assertEqual(raised.exception.code, ErrorCode.AUDIT_COMMIT_FAILED)
        status = harness.runtime.status()
        self.assertEqual(status["state"], "FAILED")
        self.assertEqual(status["active_trial_id"], harness.session["trial_id"])


if __name__ == "__main__":
    unittest.main()
