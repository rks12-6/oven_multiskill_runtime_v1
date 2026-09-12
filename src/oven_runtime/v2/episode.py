"""ROS-free lifecycle controller for one policy/correction episode.

The controller owns neither robot control nor data capture.  It only translates
valid lifecycle events into state changes and declarative effects for a future
adapter to perform.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from oven_runtime.v2.contracts import EpisodeConfig


class EpisodeState(str, Enum):
    PREPARING = "PREPARING"
    RESETTING = "RESETTING"
    READY = "READY"
    ROLLOUT = "ROLLOUT"
    CORRECTION = "CORRECTION"
    SAVING = "SAVING"
    DONE = "DONE"
    FAILED = "FAILED"


class EpisodeEvent(str, Enum):
    START = "START"
    RESET_SUCCEEDED = "RESET_SUCCEEDED"
    RESET_FAILED = "RESET_FAILED"
    START_ROLLOUT = "START_ROLLOUT"
    ROLLOUT_SUCCEEDED = "ROLLOUT_SUCCEEDED"
    ROLLOUT_FAILED = "ROLLOUT_FAILED"
    TAKEOVER_REQUESTED = "TAKEOVER_REQUESTED"
    HUMAN_STARTED = "HUMAN_STARTED"
    HUMAN_FINISHED = "HUMAN_FINISHED"
    SAVE_SUCCEEDED = "SAVE_SUCCEEDED"
    SAVE_FAILED = "SAVE_FAILED"


class ResetReason(str, Enum):
    PRE_ROLLOUT = "PRE_ROLLOUT"
    POST_CORRECTION = "POST_CORRECTION"


class EpisodeEffect(str, Enum):
    REQUEST_RESET = "REQUEST_RESET"
    START_POLICY = "START_POLICY"
    STOP_POLICY = "STOP_POLICY"
    START_CORRECTION_CAPTURE = "START_CORRECTION_CAPTURE"
    STOP_CORRECTION_CAPTURE = "STOP_CORRECTION_CAPTURE"
    SAVE_EPISODE = "SAVE_EPISODE"
    FINISH_EPISODE = "FINISH_EPISODE"
    ABORT_EPISODE = "ABORT_EPISODE"


class InvalidEpisodeTransition(RuntimeError):
    """Raised without changing controller state for an illegal event/state pair."""


@dataclass(frozen=True)
class EpisodeTransition:
    previous_state: EpisodeState
    state: EpisodeState
    effects: tuple[EpisodeEffect, ...]
    reset_reason: ResetReason | None
    failure_reason: str | None


class EpisodeController:
    """Fail-closed lifecycle state machine with no ROS or control-side effects."""

    def __init__(self, config: EpisodeConfig) -> None:
        if config.same_episode_resume:
            raise ValueError("same_episode_resume=true is unsupported in the HITL v2 MVP")
        self.config = config
        self.state = EpisodeState.PREPARING
        self.reset_reason: ResetReason | None = None
        self.failure_reason: str | None = None
        self._takeover_requested = False
        self._correction_capture_started = False

    def handle(self, event: EpisodeEvent, *, reason: str | None = None) -> EpisodeTransition:
        if self.state is EpisodeState.PREPARING and event is EpisodeEvent.START:
            if self.config.reset_before:
                self.reset_reason = ResetReason.PRE_ROLLOUT
                return self._set_state(EpisodeState.RESETTING, EpisodeEffect.REQUEST_RESET)
            return self._set_state(EpisodeState.READY)

        if self.state is EpisodeState.RESETTING:
            return self._handle_reset(event, reason)
        if self.state is EpisodeState.READY and event is EpisodeEvent.START_ROLLOUT:
            return self._set_state(EpisodeState.ROLLOUT, EpisodeEffect.START_POLICY)
        if self.state is EpisodeState.ROLLOUT:
            return self._handle_rollout(event, reason)
        if self.state is EpisodeState.CORRECTION:
            return self._handle_correction(event)
        if self.state is EpisodeState.SAVING:
            return self._handle_saving(event, reason)
        self._reject(event)

    def _handle_reset(self, event: EpisodeEvent, reason: str | None) -> EpisodeTransition:
        if event is EpisodeEvent.RESET_FAILED:
            return self._fail(reason or "reset_failed")
        if event is not EpisodeEvent.RESET_SUCCEEDED:
            self._reject(event)
        if self.reset_reason is ResetReason.PRE_ROLLOUT:
            return self._set_state(EpisodeState.READY)
        if self.reset_reason is ResetReason.POST_CORRECTION:
            return self._set_state(EpisodeState.DONE, EpisodeEffect.FINISH_EPISODE)
        raise RuntimeError("RESETTING state is missing a reset reason")

    def _handle_rollout(self, event: EpisodeEvent, reason: str | None) -> EpisodeTransition:
        if event is EpisodeEvent.ROLLOUT_SUCCEEDED:
            return self._set_state(EpisodeState.DONE, EpisodeEffect.FINISH_EPISODE)
        if event is EpisodeEvent.ROLLOUT_FAILED:
            return self._fail(reason or "rollout_failed")
        if event is EpisodeEvent.TAKEOVER_REQUESTED:
            if not self.config.allow_takeover:
                self._reject(event)
            self._takeover_requested = True
            return self._set_state(EpisodeState.CORRECTION, EpisodeEffect.STOP_POLICY)
        self._reject(event)

    def _handle_correction(self, event: EpisodeEvent) -> EpisodeTransition:
        if event is EpisodeEvent.HUMAN_STARTED:
            if self._correction_capture_started:
                self._reject(event)
            self._correction_capture_started = True
            effects = (EpisodeEffect.START_CORRECTION_CAPTURE,) if self.config.capture_correction else ()
            return self._set_state(EpisodeState.CORRECTION, *effects)
        if event is EpisodeEvent.HUMAN_FINISHED:
            if not self._correction_capture_started:
                self._reject(event)
            effects: tuple[EpisodeEffect, ...] = (EpisodeEffect.SAVE_EPISODE,)
            if self.config.capture_correction:
                effects = (EpisodeEffect.STOP_CORRECTION_CAPTURE, *effects)
            return self._set_state(EpisodeState.SAVING, *effects)
        self._reject(event)

    def _handle_saving(self, event: EpisodeEvent, reason: str | None) -> EpisodeTransition:
        if event is EpisodeEvent.SAVE_FAILED:
            return self._fail(reason or "save_failed")
        if event is not EpisodeEvent.SAVE_SUCCEEDED:
            self._reject(event)
        if self.config.reset_after_correction:
            self.reset_reason = ResetReason.POST_CORRECTION
            return self._set_state(EpisodeState.RESETTING, EpisodeEffect.REQUEST_RESET)
        return self._set_state(EpisodeState.DONE, EpisodeEffect.FINISH_EPISODE)

    def _fail(self, reason: str) -> EpisodeTransition:
        self.failure_reason = reason
        return self._set_state(EpisodeState.FAILED, EpisodeEffect.ABORT_EPISODE)

    def _set_state(self, state: EpisodeState, *effects: EpisodeEffect) -> EpisodeTransition:
        previous_state = self.state
        self.state = state
        return EpisodeTransition(
            previous_state=previous_state,
            state=state,
            effects=effects,
            reset_reason=self.reset_reason,
            failure_reason=self.failure_reason,
        )

    def _reject(self, event: EpisodeEvent) -> None:
        raise InvalidEpisodeTransition(f"event {event.value} is invalid while episode is {self.state.value}")
