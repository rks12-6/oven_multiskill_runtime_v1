from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from oven_runtime.common.errors import ErrorCode, RuntimeFault, fault
from oven_runtime.common.hashing import stable_tree_hash
from oven_runtime.common.protocol import TRIAL_ID_PATTERN, InferenceRequest, InferenceResponse, RequestKind
from oven_runtime.common.state import RuntimeState, SessionState, ensure_runtime_transition
from oven_runtime.server.audit import TrialAuditStore
from oven_runtime.server.backend import BackendIdentity, PolicyBackend


@dataclass
class SessionLease:
    session_id: str
    token: str
    skill: str
    generation: int
    trial_id: str
    expires_at_monotonic: float
    next_sequence: int = 0
    state: SessionState = SessionState.ACTIVE


class PolicyRuntime:
    """Fail-closed owner of one active policy, trial, session, and inference lock."""

    def __init__(
        self,
        *,
        backend: PolicyBackend,
        audit: TrialAuditStore,
        session_ttl_sec: float = 600.0,
        inference_lock_timeout_sec: float = 5.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if session_ttl_sec <= 0 or inference_lock_timeout_sec <= 0:
            raise ValueError("runtime timeouts must be positive")
        self._backend = backend
        self._audit = audit
        self._session_ttl_sec = session_ttl_sec
        self._inference_lock_timeout_sec = inference_lock_timeout_sec
        self._monotonic = monotonic
        self._condition = threading.Condition(threading.RLock())
        self._execution_lock = threading.Lock()
        self._state = RuntimeState.STARTING
        self._active_skill: str | None = None
        self._generation = 0
        self._identity: BackendIdentity | None = None
        self._session: SessionLease | None = None
        self._trial_id: str | None = None
        self._trial_root_seed: int | None = None
        self._in_flight = 0
        self._last_error: ErrorCode | None = None

    def status(self) -> dict[str, Any]:
        with self._condition:
            session = self._session
            return {
                "state": self._state.value,
                "active_skill": self._active_skill,
                "generation": self._generation,
                "backend": self._identity.backend if self._identity else None,
                "model_id": self._identity.model_id if self._identity else None,
                "active_session_id": session.session_id if session and session.state is SessionState.ACTIVE else None,
                "active_trial_id": self._trial_id,
                "in_flight": self._in_flight,
                "last_error": self._last_error.value if self._last_error else None,
            }

    def prepare_skill(self, skill: str, *, drain_timeout_sec: float = 30.0) -> dict[str, Any]:
        if not skill:
            raise fault(ErrorCode.INVALID_FIELD_VALUE, "skill must not be empty")
        if drain_timeout_sec <= 0:
            raise ValueError("drain_timeout_sec must be positive")
        with self._condition:
            if self._trial_id is not None:
                raise fault(ErrorCode.STATE_REJECTED, "end or abort the active trial before preparing a skill")
            if self._state in {RuntimeState.FAILED, RuntimeState.STOPPING, RuntimeState.LOADING, RuntimeState.SWITCHING}:
                raise fault(ErrorCode.STATE_REJECTED, "runtime cannot prepare a skill in its current state", state=self._state.value)
            self._invalidate_session_locked(SessionState.ABORTED)
            if self._state is RuntimeState.STARTING:
                self._transition_locked(RuntimeState.LOADING)
            else:
                self._transition_locked(RuntimeState.DRAINING)
                deadline = self._monotonic() + drain_timeout_sec
                while self._in_flight:
                    remaining = deadline - self._monotonic()
                    if remaining <= 0:
                        self._fail_locked(ErrorCode.INFERENCE_TIMEOUT)
                        raise fault(ErrorCode.INFERENCE_TIMEOUT, "timed out draining active inference")
                    self._condition.wait(timeout=remaining)
                self._transition_locked(RuntimeState.SWITCHING)
                self._transition_locked(RuntimeState.LOADING)

        acquired = self._execution_lock.acquire(timeout=drain_timeout_sec)
        if not acquired:
            with self._condition:
                self._fail_locked(ErrorCode.INFERENCE_BUSY)
            raise fault(ErrorCode.INFERENCE_BUSY, "cannot acquire policy execution lock for model preparation")
        try:
            identity = self._backend.prepare(skill)
            if identity.skill != skill:
                raise RuntimeError("backend returned a different skill identity")
        except Exception as exc:
            with self._condition:
                self._fail_locked(ErrorCode.POLICY_FAILED)
            if isinstance(exc, RuntimeFault):
                raise
            raise fault(ErrorCode.POLICY_FAILED, "policy backend failed to prepare the requested skill") from exc
        finally:
            self._execution_lock.release()

        with self._condition:
            self._active_skill = skill
            self._identity = identity
            self._generation += 1
            self._transition_locked(RuntimeState.READY)
            return self.status()

    def begin_trial(self, *, trial_id: str, root_seed: int) -> dict[str, Any]:
        if not isinstance(trial_id, str) or TRIAL_ID_PATTERN.fullmatch(trial_id) is None:
            raise fault(ErrorCode.INVALID_FIELD_VALUE, "trial_id format is invalid")
        if not isinstance(root_seed, int) or isinstance(root_seed, bool):
            raise fault(ErrorCode.INVALID_FIELD_TYPE, "root_seed must be an integer")
        if not self._execution_lock.acquire(timeout=self._inference_lock_timeout_sec):
            raise fault(ErrorCode.INFERENCE_BUSY, "cannot acquire policy execution lock for trial initialization")
        try:
            with self._condition:
                if self._state is not RuntimeState.READY:
                    raise fault(ErrorCode.STATE_REJECTED, "trial can begin only when runtime is ready", state=self._state.value)
                if self._trial_id is not None:
                    raise fault(ErrorCode.STATE_REJECTED, "another trial is already active")
                if self._active_skill is None:
                    raise fault(ErrorCode.SERVER_FAILED, "runtime has no active skill")
                self._backend.reset_prng(root_seed)
                active = self._audit.begin(
                    trial_id=trial_id,
                    skill=self._active_skill,
                    generation=self._generation,
                    root_seed=root_seed,
                )
                self._trial_id = active.trial_id
                self._trial_root_seed = root_seed
                return self.status()
        except RuntimeFault:
            raise
        except Exception as exc:
            with self._condition:
                self._fail_locked(ErrorCode.POLICY_FAILED)
            raise fault(ErrorCode.POLICY_FAILED, "policy backend failed to initialize trial PRNG") from exc
        finally:
            self._execution_lock.release()

    def open_session(self) -> dict[str, Any]:
        with self._condition:
            if self._state is not RuntimeState.READY:
                raise fault(ErrorCode.STATE_REJECTED, "session cannot open in the current state", state=self._state.value)
            if self._trial_id is None or self._active_skill is None:
                raise fault(ErrorCode.TRIAL_REQUIRED, "begin a trial before opening a session")
            if self._session is not None and self._session.state is SessionState.ACTIVE:
                raise fault(ErrorCode.SESSION_ALREADY_ACTIVE, "an action session is already active")
            lease = SessionLease(
                session_id=str(uuid4()),
                token=secrets.token_urlsafe(32),
                skill=self._active_skill,
                generation=self._generation,
                trial_id=self._trial_id,
                expires_at_monotonic=self._monotonic() + self._session_ttl_sec,
            )
            self._session = lease
            return {
                "session_id": lease.session_id,
                "session_token": lease.token,
                "skill": lease.skill,
                "generation": lease.generation,
                "trial_id": lease.trial_id,
                "expires_in_sec": self._session_ttl_sec,
            }

    def infer(self, payload: Any) -> dict[str, Any]:
        request = InferenceRequest.parse(payload)
        if not self._execution_lock.acquire(timeout=self._inference_lock_timeout_sec):
            raise fault(ErrorCode.INFERENCE_BUSY, "policy execution lock is busy")

        entered = False
        try:
            with self._condition:
                lease = self._validate_request_locked(request)
                self._in_flight += 1
                entered = True

            try:
                backend_result = self._backend.infer(request.observation, request.request_kind)
                observation_hash = stable_tree_hash(request.observation)
                request_hash = stable_tree_hash(
                    {
                        "protocol_version": request.protocol_version,
                        "session_id": request.session_id,
                        "trial_id": request.trial_id,
                        "request_kind": request.request_kind.value,
                        "expected_skill": request.expected_skill,
                        "expected_generation": request.expected_generation,
                        "sequence": request.sequence,
                        "observation_hash": observation_hash,
                    }
                )
                action_hash = stable_tree_hash(backend_result.actions)
                row = {
                    "schema_version": 1,
                    "session_id": request.session_id,
                    "trial_id": request.trial_id,
                    "skill": request.expected_skill,
                    "generation": request.expected_generation,
                    "sequence": request.sequence,
                    "request_kind": request.request_kind.value,
                    "request_hash": request_hash,
                    "observation_hash": observation_hash,
                    "noise_hash": backend_result.noise_hash,
                    "action_hash": action_hash,
                }
                self._audit.record_request(row, backend_result.actions, request.observation)
            except RuntimeFault as exc:
                with self._condition:
                    self._fail_locked(exc.code)
                raise
            except Exception as exc:
                with self._condition:
                    self._fail_locked(ErrorCode.POLICY_FAILED)
                raise fault(ErrorCode.POLICY_FAILED, "policy inference failed") from exc

            with self._condition:
                if self._session is not lease or lease.state is not SessionState.ACTIVE:
                    self._fail_locked(ErrorCode.SESSION_INVALID)
                    raise fault(ErrorCode.SESSION_INVALID, "session changed during inference")
                lease.next_sequence += 1
                response = InferenceResponse(
                    session_id=request.session_id,
                    trial_id=request.trial_id,
                    skill=request.expected_skill,
                    generation=request.expected_generation,
                    sequence=request.sequence,
                    publishable=True,
                    request_hash=request_hash,
                    observation_hash=observation_hash,
                    noise_hash=backend_result.noise_hash,
                    action_hash=action_hash,
                    actions=backend_result.actions,
                )
                return response.to_dict()
        finally:
            if entered:
                with self._condition:
                    self._in_flight -= 1
                    self._condition.notify_all()
            self._execution_lock.release()

    def close_session(self, session_id: str) -> dict[str, Any]:
        with self._condition:
            lease = self._session
            if lease is None or lease.state is not SessionState.ACTIVE:
                raise fault(ErrorCode.SESSION_REQUIRED, "no active session")
            if lease.session_id != session_id:
                raise fault(ErrorCode.SESSION_INVALID, "session ID does not match the active lease")
            if self._in_flight:
                raise fault(ErrorCode.INFERENCE_BUSY, "cannot close a session with inference in flight")
            self._invalidate_session_locked(SessionState.CLOSED)
            return self.status()

    def end_trial(self) -> dict[str, Any]:
        with self._condition:
            if self._session is not None and self._session.state is SessionState.ACTIVE:
                raise fault(ErrorCode.SESSION_ALREADY_ACTIVE, "close the active session before ending the trial")
            if self._trial_id is None:
                raise fault(ErrorCode.TRIAL_REQUIRED, "no active trial")
            try:
                summary = self._audit.end()
            except RuntimeFault:
                self._fail_locked(ErrorCode.AUDIT_COMMIT_FAILED)
                raise
            self._trial_id = None
            self._trial_root_seed = None
            return summary

    def abort(self, reason: str) -> dict[str, Any]:
        if not self._execution_lock.acquire(timeout=30.0):
            with self._condition:
                self._fail_locked(ErrorCode.INFERENCE_TIMEOUT)
            raise fault(ErrorCode.INFERENCE_TIMEOUT, "timed out waiting for active inference before abort")
        try:
            with self._condition:
                self._invalidate_session_locked(SessionState.ABORTED)
                try:
                    summary = self._audit.abort(reason)
                except RuntimeFault:
                    self._fail_locked(ErrorCode.AUDIT_COMMIT_FAILED)
                    raise
                self._trial_id = None
                self._trial_root_seed = None
                return {"state": self._state.value, "trial": summary}
        finally:
            self._execution_lock.release()

    def mark_transport_ambiguous(self, *, session_id: str, sequence: int) -> dict[str, Any]:
        """Fail closed after inference ran but its response couldn't be confirmed sent."""

        with self._condition:
            lease = self._session
            if lease is None or lease.session_id != session_id:
                return {"state": self._state.value, "trial": None}
            self._invalidate_session_locked(SessionState.ABORTED)
            try:
                summary = self._audit.mark_ambiguous(
                    "inference response transport failed",
                    sequence=sequence,
                )
            except RuntimeFault:
                self._fail_locked(ErrorCode.AUDIT_COMMIT_FAILED)
                raise
            self._trial_id = None
            self._trial_root_seed = None
            self._fail_locked(ErrorCode.SERVER_FAILED)
            return {"state": self._state.value, "trial": summary}

    def close(self) -> None:
        audit_error: RuntimeFault | None = None
        with self._condition:
            if self._state is not RuntimeState.STOPPING:
                self._transition_locked(RuntimeState.STOPPING)
            self._invalidate_session_locked(SessionState.ABORTED)
            if self._trial_id is not None:
                try:
                    self._audit.abort("server shutdown")
                except RuntimeFault as exc:
                    audit_error = exc
                else:
                    self._trial_id = None
                    self._trial_root_seed = None
        try:
            self._backend.close()
        finally:
            if audit_error is not None:
                raise audit_error

    def _validate_request_locked(self, request: InferenceRequest) -> SessionLease:
        lease = self._session
        if lease is None or lease.state is not SessionState.ACTIVE:
            raise fault(ErrorCode.SESSION_REQUIRED, "an active session is required")
        if self._monotonic() >= lease.expires_at_monotonic:
            self._invalidate_session_locked(SessionState.EXPIRED)
            raise fault(ErrorCode.SESSION_EXPIRED, "the active session has expired")
        if request.session_id != lease.session_id or not secrets.compare_digest(request.session_token, lease.token):
            raise fault(ErrorCode.SESSION_INVALID, "session credentials are invalid")
        if request.trial_id != lease.trial_id or request.trial_id != self._trial_id:
            raise fault(ErrorCode.TRIAL_MISMATCH, "request trial does not match the active trial")
        if request.expected_skill != lease.skill or request.expected_skill != self._active_skill:
            raise fault(ErrorCode.SKILL_MISMATCH, "request skill does not match the active policy")
        if request.expected_generation != lease.generation or request.expected_generation != self._generation:
            raise fault(ErrorCode.GENERATION_MISMATCH, "request generation does not match the active policy")
        if request.sequence != lease.next_sequence:
            raise fault(
                ErrorCode.SEQUENCE_MISMATCH,
                "request sequence is not the next expected value",
                expected=lease.next_sequence,
                received=request.sequence,
            )
        if request.request_kind is not RequestKind.INFER:
            raise fault(ErrorCode.INVALID_FIELD_VALUE, "warm-up requests are not supported")
        if self._state is not RuntimeState.READY:
            raise fault(
                ErrorCode.STATE_REJECTED,
                "request kind is not allowed in the current state",
                state=self._state.value,
                request_kind=request.request_kind.value,
            )
        return lease

    def _transition_locked(self, target: RuntimeState) -> None:
        ensure_runtime_transition(self._state, target)
        self._state = target

    def _invalidate_session_locked(self, state: SessionState) -> None:
        if self._session is not None:
            self._session.state = state
            self._session.token = ""
            self._session = None

    def _fail_locked(self, code: ErrorCode) -> None:
        self._last_error = code
        self._invalidate_session_locked(SessionState.ABORTED)
        if self._state not in {RuntimeState.FAILED, RuntimeState.STOPPING}:
            self._transition_locked(RuntimeState.FAILED)
