from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from oven_runtime.common.audit_schema import EvidenceStatus, StageEvidence
from oven_runtime.common.errors import ErrorCode, RuntimeFault, fault
from oven_runtime.common.protocol import PROTOCOL_VERSION
from oven_runtime.common.protocol import TRIAL_ID_PATTERN
from oven_runtime.common.state import PipelineState, ensure_pipeline_transition
from oven_runtime.edge.audit import EdgeAuditStore
from oven_runtime.edge.contracts import (
    ApprovalGate,
    ControlClient,
    InferenceTransport,
    ObservationSource,
    PublishResult,
    ResetController,
    VisualChecker,
)
from oven_runtime.edge.validation import ActionValidator, ObservationValidator
from oven_runtime.v2.control import ControlAdapter


FIXED_SKILL_ORDER = ("open_door", "transport_food", "close_door", "rotate_button")


@dataclass(frozen=True)
class StagePlan:
    skill: str
    root_seed: int
    max_chunks: int
    action_steps: int
    settle_sec: float = 3.0
    reset_before: bool = True
    checker_required: bool = True

    def validate(self) -> None:
        if not self.skill or not isinstance(self.root_seed, int) or isinstance(self.root_seed, bool):
            raise ValueError("stage skill and root_seed are invalid")
        if (
            self.root_seed < 0
            or self.max_chunks <= 0
            or self.action_steps <= 0
            or self.settle_sec < 0
        ):
            raise ValueError("stage rollout limits are invalid")


@dataclass(frozen=True)
class PipelinePlan:
    run_id: str
    stages: tuple[StagePlan, ...]

    def validate(self) -> None:
        skills = tuple(stage.skill for stage in self.stages)
        if skills != FIXED_SKILL_ORDER and not (len(skills) == 1 and skills[0] in FIXED_SKILL_ORDER):
            raise ValueError("plan must contain the fixed four-skill pipeline or one declared skill")
        if TRIAL_ID_PATTERN.fullmatch(self.run_id) is None:
            raise ValueError("pipeline run_id format is invalid")
        for index, stage in enumerate(self.stages):
            stage.validate()
            if TRIAL_ID_PATTERN.fullmatch(f"{self.run_id}_{index}_{stage.skill}") is None:
                raise ValueError("derived trial_id format is invalid")


class PipelineOrchestrator:
    """The single Agilex component allowed to advance the four-skill pipeline."""

    def __init__(
        self,
        *,
        plan: PipelinePlan,
        observation_source: ObservationSource,
        observation_validator: ObservationValidator,
        reset_controller: ResetController,
        action_executor: ControlAdapter,
        action_validator: ActionValidator,
        checker: VisualChecker,
        approval: ApprovalGate,
        control: ControlClient,
        inference: InferenceTransport,
        audit: EdgeAuditStore,
        sleep: Any = time.sleep,
    ) -> None:
        plan.validate()
        self.plan = plan
        self.observation_source = observation_source
        self.observation_validator = observation_validator
        self.reset_controller = reset_controller
        self.action_executor = action_executor
        self.action_validator = action_validator
        self.checker = checker
        self.approval = approval
        self.control = control
        self.inference = inference
        self.audit = audit
        self.sleep = sleep
        self.state = PipelineState.CREATED
        self._active_session_id: str | None = None
        self._trial_started = False

    def run(self) -> dict[str, Any]:
        skills = tuple(stage.skill for stage in self.plan.stages)
        if not self.approval.approve_run(self.plan.run_id, skills):
            raise fault(ErrorCode.APPROVAL_REQUIRED, "run-level operator approval was not granted")
        self.audit.begin(self.plan.run_id, tuple(stage.skill for stage in self.plan.stages))
        try:
            self._transition(PipelineState.PREFLIGHT)
            self.observation_source.preflight()
            self.reset_controller.preflight()
            self.action_executor.preflight()
            for index, stage in enumerate(self.plan.stages):
                self._run_stage(index, stage)
            self._transition(PipelineState.COMPLETED)
            return self.audit.complete()
        except (KeyboardInterrupt, SystemExit):
            self._safe_stop(ErrorCode.PIPELINE_FAILED, "operator interrupted pipeline")
            raise
        except RuntimeFault as exc:
            self._safe_stop(exc.code, exc.message)
            raise
        except Exception as exc:
            wrapped = fault(ErrorCode.PIPELINE_FAILED, "pipeline component failed")
            self._safe_stop(wrapped.code, wrapped.message)
            raise wrapped from exc

    def _run_stage(self, index: int, stage: StagePlan) -> None:
        trial_id = f"{self.plan.run_id}_{index}_{stage.skill}"
        self.audit.start_stage(index, stage.skill, trial_id)
        self._transition(PipelineState.RESETTING)
        if stage.reset_before:
            self.reset_controller.reset(stage.skill)
            self.sleep(stage.settle_sec)

        self._transition(PipelineState.PREPARING_POLICY)
        self._control("prepare-skill", stage.skill)
        self._trial_started = True
        self._control("begin-trial", trial_id, stage.root_seed)
        session = self._control("open-session")
        self._active_session_id = session["session_id"]

        self._transition(PipelineState.READY_TO_ROLLOUT)
        self.action_executor.begin_stage(stage.skill)
        self._transition(PipelineState.ROLLOUT)
        rollout_result = self._rollout(stage, session)
        self.action_executor.stop("rollout_complete")
        self._control("close-session", self._active_session_id)
        self._active_session_id = None
        server_summary = self._control("end-trial")
        if server_summary.get("status") != "COMMITTED":
            raise fault(ErrorCode.AUDIT_COMMIT_FAILED, "server did not commit its trial evidence")
        self._trial_started = False

        self._transition(PipelineState.JOINT_GATE)
        gate_result = rollout_result.gate_result
        if gate_result is None or not gate_result.passed:
            raise fault(ErrorCode.JOINT_GATE_FAILED, "rollout ended without joint-rest evidence")

        self._transition(PipelineState.CHECKING)
        if stage.checker_required:
            checker_result = self.checker.evaluate(stage.skill)
            if not checker_result.passed:
                raise fault(ErrorCode.CHECKER_FAILED, "visual checker rejected the stage", reason=checker_result.reason)
            checker_evidence = EvidenceStatus.PASSED
        else:
            from oven_runtime.edge.contracts import CheckerResult

            checker_result = CheckerResult(
                passed=True,
                score=1.0,
                threshold=0.0,
                asset_id="not_required",
                reason="checker_not_required",
            )
            checker_evidence = EvidenceStatus.NOT_REQUIRED

        evidence = StageEvidence(
            rollout_exit_code=0,
            rollout_end_reason=rollout_result.reason or "joint_rest_detected",
            rollout_end_reason_approved=True,
            joint_gate=EvidenceStatus.PASSED,
            checker=checker_evidence,
            server_audit=EvidenceStatus.PASSED,
            edge_audit=EvidenceStatus.PASSED,
        )
        self.audit.complete_stage(
            evidence=evidence,
            gate=gate_result,
            checker=checker_result,
            server_summary=server_summary,
        )
        self._transition(PipelineState.HANDOFF)

    def _rollout(self, stage: StagePlan, session: dict[str, Any]) -> PublishResult:
        published_steps = 0
        for sequence in range(stage.max_chunks):
            generation = self.action_executor.rollout_generation()
            response = self.inference.infer(self._request(session, "infer", sequence))
            self.action_executor.ensure_rollout_generation(generation)
            if response.get("publishable") is not True:
                raise fault(ErrorCode.ACTION_INVALID, "formal inference response is not publishable")
            actions = self.action_validator.validate(response["actions"])
            if published_steps + len(actions) > stage.action_steps:
                raise fault(ErrorCode.ACTION_INVALID, "action chunk exceeds the stage action-step budget")
            publish_result = self.action_executor.publish(actions)
            if not 0 < publish_result.published_rows <= len(actions):
                raise fault(ErrorCode.ACTION_INVALID, "action executor reported an invalid published-row count")
            published_steps += publish_result.published_rows
            self.audit.record_rollout(
                {
                    "schema_version": 1,
                    "skill": stage.skill,
                    "session_id": session["session_id"],
                    "generation": session["generation"],
                    "sequence": sequence,
                    "published_rows": publish_result.published_rows,
                    "published_steps_total": published_steps,
                    "request_hash": response["request_hash"],
                    "action_hash": response["action_hash"],
                }
            )
            if publish_result.stop_requested:
                if publish_result.gate_result is None or not publish_result.gate_result.passed:
                    raise fault(ErrorCode.JOINT_GATE_FAILED, "executor stopped without passed joint-rest evidence")
                return publish_result
        raise fault(
            ErrorCode.JOINT_GATE_FAILED,
            "stage exhausted its action-step budget without joint-rest detection",
            published_steps=published_steps,
        )

    def _request(self, session: dict[str, Any], request_kind: str, sequence: int) -> dict[str, Any]:
        observation = self.observation_validator.validate(self.observation_source.sample(session["skill"]))
        return {
            "protocol_version": PROTOCOL_VERSION,
            "message_type": "inference_request",
            "session_id": session["session_id"],
            "session_token": session["session_token"],
            "trial_id": session["trial_id"],
            "request_kind": request_kind,
            "expected_skill": session["skill"],
            "expected_generation": session["generation"],
            "sequence": sequence,
            "sent_at_monotonic_ns": time.monotonic_ns(),
            "observation": observation,
        }

    def _control(self, command: str, *arguments: str | int) -> dict[str, Any]:
        response = self.control.call(command, *arguments)
        result = response.get("result")
        if response.get("ok") is not True or not isinstance(result, dict):
            raise fault(ErrorCode.SERVER_FAILED, "control client returned an invalid success response")
        return result

    def _safe_stop(self, code: ErrorCode, message: str) -> None:
        cleanup_failures: list[str] = []
        if self.state not in {PipelineState.SAFE_STOP, PipelineState.FAILED, PipelineState.COMPLETED}:
            ensure_pipeline_transition(self.state, PipelineState.SAFE_STOP)
            self.state = PipelineState.SAFE_STOP
        try:
            self.action_executor.stop(f"safe_stop:{code.value}")
        except Exception as exc:
            cleanup_failures.append(f"action_stop:{type(exc).__name__}")
        if self._active_session_id is not None:
            try:
                self._control("close-session", self._active_session_id)
            except Exception as exc:
                cleanup_failures.append(f"close_session:{type(exc).__name__}")
            self._active_session_id = None
        if self._trial_started:
            try:
                self._control("abort", f"edge safe stop: {code.value}")
            except Exception as exc:
                cleanup_failures.append(f"abort_trial:{type(exc).__name__}")
            self._trial_started = False
        if self.state is PipelineState.SAFE_STOP:
            ensure_pipeline_transition(self.state, PipelineState.FAILED)
            self.state = PipelineState.FAILED
        if cleanup_failures:
            message = f"{message}; cleanup_failures={','.join(cleanup_failures)}"
        try:
            self.audit.fail(state=self.state.value, error_code=code.value, message=message)
        except Exception:
            # Preserve the original pipeline failure. The caller still closes
            # local resources in app.main's finally block.
            pass

    def _transition(self, target: PipelineState) -> None:
        ensure_pipeline_transition(self.state, target)
        self.state = target
        self.audit.set_pipeline_state(target.value)
