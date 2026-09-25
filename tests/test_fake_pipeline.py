from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np

from oven_runtime.common.errors import ErrorCode, RuntimeFault
from oven_runtime.common.protocol import InferenceRequest
from oven_runtime.edge.audit import EdgeAuditStore
from oven_runtime.edge.contracts import CheckerResult, GateResult, Observation, PublishResult
from oven_runtime.edge.inference_client import validate_inference_response
from oven_runtime.edge.joint_gate import WindowedJointGate
from oven_runtime.edge.orchestrator import PipelineOrchestrator, PipelinePlan, StagePlan
from oven_runtime.edge.validation import ActionValidator, ObservationValidator
from oven_runtime.server.audit import TrialAuditStore
from oven_runtime.server.control_protocol import ControlDispatcher
from oven_runtime.server.fake_backend import FakePolicyBackend
from oven_runtime.server.runtime import PolicyRuntime


def _arguments(command: str, values: tuple[str | int, ...]) -> dict[str, Any]:
    names = {
        "status": (),
        "prepare-skill": ("skill",),
        "begin-trial": ("trial_id", "root_seed"),
        "open-session": (),
        "close-session": ("session_id",),
        "end-trial": (),
        "abort": ("reason",),
    }
    return dict(zip(names[command], values, strict=True))


class DirectControlClient:
    def __init__(self, runtime: PolicyRuntime) -> None:
        self.dispatcher = ControlDispatcher(runtime, str(uuid4()))

    def call(self, command: str, *arguments: str | int) -> dict[str, Any]:
        response = self.dispatcher.execute(
            {
                "protocol_version": 1,
                "command": command,
                "arguments": _arguments(command, arguments),
            }
        )
        if not response["ok"]:
            raise RuntimeFault(ErrorCode(response["error_code"]), response["message"])
        return response


class DirectInferenceClient:
    def __init__(self, runtime: PolicyRuntime) -> None:
        self.runtime = runtime

    def infer(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = InferenceRequest.parse(payload)
        return validate_inference_response(request, self.runtime.infer(payload))


class FakeObservationSource:
    def __init__(self) -> None:
        self.preflight_count = 0

    def preflight(self) -> None:
        self.preflight_count += 1

    def sample(self, skill: str) -> Observation:
        return Observation(
            payload={"state": np.zeros(7, dtype=np.float32), "prompt": skill},
            captured_at_monotonic_ns=time.monotonic_ns(),
        )


class FakeResetController:
    def __init__(self) -> None:
        self.skills: list[str] = []

    def preflight(self) -> None:
        return

    def reset(self, skill: str) -> None:
        self.skills.append(skill)


class RecordingExecutor:
    def __init__(self, *, detect_joint_rest: bool = True) -> None:
        self.chunks: list[np.ndarray] = []
        self.stop_reasons: list[str] = []
        self.detect_joint_rest = detect_joint_rest

    def preflight(self) -> None:
        return

    def begin_stage(self, skill: str) -> None:
        self.active_skill = skill

    def publish(self, actions: Any) -> PublishResult:
        self.chunks.append(np.asarray(actions).copy())
        if self.detect_joint_rest:
            return PublishResult(
                stop_requested=True,
                reason="joint_rest_detected",
                published_rows=len(actions),
                gate_result=GateResult(True, 10, 0.0, 0.0, "stable_target_window"),
            )
        return PublishResult(published_rows=len(actions))

    def stop(self, reason: str) -> None:
        self.stop_reasons.append(reason)


class AlwaysApprove:
    def approve_run(self, run_id: str, skills: tuple[str, ...]) -> bool:
        return bool(run_id and skills)


class ConfigurableChecker:
    def __init__(self, failed_skill: str | None = None) -> None:
        self.failed_skill = failed_skill
        self.checked: list[str] = []

    def evaluate(self, skill: str) -> CheckerResult:
        self.checked.append(skill)
        passed = skill != self.failed_skill
        return CheckerResult(
            passed=passed,
            score=0.9 if passed else 0.1,
            threshold=0.5,
            asset_id="fake-checker-v1",
            reason="passed" if passed else "injected_failure",
        )


class PipelineHarness:
    def __init__(
        self,
        root: Path,
        *,
        failed_checker_skill: str | None = None,
        detect_joint_rest: bool = True,
    ) -> None:
        self.backend = FakePolicyBackend(action_shape=(50, 14))
        self.runtime = PolicyRuntime(
            backend=self.backend,
            audit=TrialAuditStore(root / "server_audit"),
        )
        self.observation = FakeObservationSource()
        self.reset = FakeResetController()
        self.executor = RecordingExecutor(detect_joint_rest=detect_joint_rest)
        self.checker = ConfigurableChecker(failed_checker_skill)
        self.edge_audit = EdgeAuditStore(root / "edge_audit")
        gate = WindowedJointGate(
            sample_joints=lambda: np.zeros(7, dtype=np.float64),
            target=np.zeros(7, dtype=np.float64),
            tolerance=np.full(7, 0.01, dtype=np.float64),
            max_spread=0.005,
            required_samples=3,
            max_samples=5,
        )
        plan = PipelinePlan(
            run_id="fake_run_001",
            stages=tuple(
                StagePlan(
                    skill=skill,
                    root_seed=index + 10,
                    max_chunks=3,
                    action_steps=150,
                    settle_sec=3.0,
                    checker_required=skill != "rotate_button",
                )
                for index, skill in enumerate(("open_door", "transport_food", "close_door", "rotate_button"))
            ),
        )
        self.orchestrator = PipelineOrchestrator(
            plan=plan,
            observation_source=self.observation,
            observation_validator=ObservationValidator(max_age_ms=100),
            reset_controller=self.reset,
            action_executor=self.executor,
            action_validator=ActionValidator(action_dimension=7, chunk_rows=50, max_absolute_value=10),
            checker=self.checker,
            approval=AlwaysApprove(),
            control=DirectControlClient(self.runtime),
            inference=DirectInferenceClient(self.runtime),
            audit=self.edge_audit,
            sleep=lambda _seconds: None,
        )


class FakePipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_complete_four_skill_pipeline_with_checkerless_rotate(self) -> None:
        harness = PipelineHarness(self.root)
        result = harness.orchestrator.run()
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["completed_stages"], 4)
        self.assertEqual(harness.reset.skills, ["open_door", "transport_food", "close_door", "rotate_button"])
        self.assertEqual(len(harness.executor.chunks), 4)
        self.assertTrue(all(chunk.shape == (50, 7) for chunk in harness.executor.chunks))
        self.assertEqual(harness.backend.prepare_count, 4)
        self.assertEqual(harness.checker.checked, ["open_door", "transport_food", "close_door"])
        manifest_path = self.root / "edge_audit" / "fake_run_001" / "run_manifest.json"
        self.assertEqual(json.loads(manifest_path.read_text(encoding="utf-8"))["status"], "COMPLETED")

    def test_checker_failure_blocks_next_skill_and_fails_run(self) -> None:
        harness = PipelineHarness(self.root, failed_checker_skill="transport_food")
        with self.assertRaises(RuntimeFault) as raised:
            harness.orchestrator.run()
        self.assertEqual(raised.exception.code, ErrorCode.CHECKER_FAILED)
        self.assertEqual(harness.backend.prepare_count, 2)
        self.assertNotIn("close_door", harness.reset.skills)
        self.assertTrue(harness.executor.stop_reasons[-1].startswith("safe_stop:"))
        manifest_path = self.root / "edge_audit" / "fake_run_001" / "run_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "FAILED")
        self.assertEqual(manifest["completed_stages"], 1)
        self.assertEqual(harness.runtime.status()["state"], "READY")
        harness.runtime.prepare_skill("open_door")

    def test_action_budget_exhaustion_is_failure_and_server_remains_reusable(self) -> None:
        harness = PipelineHarness(self.root, detect_joint_rest=False)
        with self.assertRaises(RuntimeFault) as raised:
            harness.orchestrator.run()
        self.assertEqual(raised.exception.code, ErrorCode.JOINT_GATE_FAILED)
        self.assertEqual(len(harness.executor.chunks), 3)
        self.assertEqual(sum(len(chunk) for chunk in harness.executor.chunks), 150)
        self.assertEqual(harness.runtime.status()["state"], "READY")


if __name__ == "__main__":
    unittest.main()
