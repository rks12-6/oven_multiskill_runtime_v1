from __future__ import annotations

import unittest
from dataclasses import replace

from oven_runtime.v2.contracts import EpisodeConfig
from oven_runtime.v2.episode import (
    EpisodeController,
    EpisodeEffect,
    EpisodeEvent,
    EpisodeState,
    InvalidEpisodeTransition,
    ResetReason,
)


DEFAULT_CONFIG = EpisodeConfig(
    reset_before=True,
    allow_takeover=True,
    same_episode_resume=False,
    capture_correction=True,
    reset_after_correction=True,
)


class EpisodeControllerTests(unittest.TestCase):
    def test_normal_policy_success(self) -> None:
        controller = EpisodeController(DEFAULT_CONFIG)
        self._assert_transition(
            controller.handle(EpisodeEvent.START), EpisodeState.RESETTING, (EpisodeEffect.REQUEST_RESET,)
        )
        self.assertEqual(controller.reset_reason, ResetReason.PRE_ROLLOUT)
        self._assert_transition(controller.handle(EpisodeEvent.RESET_SUCCEEDED), EpisodeState.READY, ())
        self._assert_transition(
            controller.handle(EpisodeEvent.START_ROLLOUT), EpisodeState.ROLLOUT, (EpisodeEffect.START_POLICY,)
        )
        self._assert_transition(
            controller.handle(EpisodeEvent.ROLLOUT_SUCCEEDED), EpisodeState.DONE, (EpisodeEffect.FINISH_EPISODE,)
        )

    def test_takeover_correction_post_reset_flow(self) -> None:
        controller = self._rollout_controller()
        self._assert_transition(
            controller.handle(EpisodeEvent.TAKEOVER_REQUESTED), EpisodeState.CORRECTION, (EpisodeEffect.STOP_POLICY,)
        )
        self._assert_transition(
            controller.handle(EpisodeEvent.HUMAN_STARTED),
            EpisodeState.CORRECTION,
            (EpisodeEffect.START_CORRECTION_CAPTURE,),
        )
        self._assert_transition(
            controller.handle(EpisodeEvent.HUMAN_FINISHED),
            EpisodeState.SAVING,
            (EpisodeEffect.STOP_CORRECTION_CAPTURE, EpisodeEffect.SAVE_EPISODE),
        )
        self._assert_transition(
            controller.handle(EpisodeEvent.SAVE_SUCCEEDED), EpisodeState.RESETTING, (EpisodeEffect.REQUEST_RESET,)
        )
        self.assertEqual(controller.reset_reason, ResetReason.POST_CORRECTION)
        self._assert_transition(
            controller.handle(EpisodeEvent.RESET_SUCCEEDED), EpisodeState.DONE, (EpisodeEffect.FINISH_EPISODE,)
        )

    def test_correction_save_without_post_reset_finishes(self) -> None:
        controller = self._rollout_controller(reset_after_correction=False)
        controller.handle(EpisodeEvent.TAKEOVER_REQUESTED)
        controller.handle(EpisodeEvent.HUMAN_STARTED)
        controller.handle(EpisodeEvent.HUMAN_FINISHED)
        self._assert_transition(
            controller.handle(EpisodeEvent.SAVE_SUCCEEDED), EpisodeState.DONE, (EpisodeEffect.FINISH_EPISODE,)
        )

    def test_failures_abort_and_retain_reason(self) -> None:
        cases = (
            (EpisodeController(DEFAULT_CONFIG), (EpisodeEvent.START, EpisodeEvent.RESET_FAILED), "pre_reset"),
            (self._rollout_controller(), (EpisodeEvent.ROLLOUT_FAILED,), "rollout"),
            (self._saving_controller(), (EpisodeEvent.SAVE_FAILED,), "save"),
        )
        for controller, events, reason in cases:
            with self.subTest(reason=reason):
                for event in events[:-1]:
                    controller.handle(event)
                transition = controller.handle(events[-1], reason=reason)
                self._assert_transition(transition, EpisodeState.FAILED, (EpisodeEffect.ABORT_EPISODE,))
                self.assertEqual(controller.failure_reason, reason)

    def test_correction_cannot_resume_rollout(self) -> None:
        controller = self._rollout_controller()
        controller.handle(EpisodeEvent.TAKEOVER_REQUESTED)
        self._assert_rejected(controller, EpisodeEvent.START_ROLLOUT, EpisodeState.CORRECTION)

    def test_unexpected_events_fail_closed(self) -> None:
        preparing = EpisodeController(DEFAULT_CONFIG)
        self._assert_rejected(preparing, EpisodeEvent.HUMAN_STARTED, EpisodeState.PREPARING)

        rollout = self._rollout_controller()
        self._assert_rejected(rollout, EpisodeEvent.SAVE_SUCCEEDED, EpisodeState.ROLLOUT)

        saving = self._saving_controller()
        self._assert_rejected(saving, EpisodeEvent.START_ROLLOUT, EpisodeState.SAVING)

    def test_done_and_failed_reject_new_execution(self) -> None:
        done = self._rollout_controller()
        done.handle(EpisodeEvent.ROLLOUT_SUCCEEDED)
        self._assert_rejected(done, EpisodeEvent.START_ROLLOUT, EpisodeState.DONE)

        failed = EpisodeController(DEFAULT_CONFIG)
        failed.handle(EpisodeEvent.START)
        failed.handle(EpisodeEvent.RESET_FAILED)
        self._assert_rejected(failed, EpisodeEvent.START_ROLLOUT, EpisodeState.FAILED)

    def test_takeover_is_rejected_when_disabled(self) -> None:
        controller = self._rollout_controller(allow_takeover=False)
        self._assert_rejected(controller, EpisodeEvent.TAKEOVER_REQUESTED, EpisodeState.ROLLOUT)

    def test_same_episode_resume_is_rejected_at_construction(self) -> None:
        with self.assertRaises(ValueError):
            EpisodeController(replace(DEFAULT_CONFIG, same_episode_resume=True))

    def _rollout_controller(self, **config_values: bool) -> EpisodeController:
        controller = EpisodeController(replace(DEFAULT_CONFIG, **config_values))
        controller.handle(EpisodeEvent.START)
        controller.handle(EpisodeEvent.RESET_SUCCEEDED)
        controller.handle(EpisodeEvent.START_ROLLOUT)
        return controller

    def _saving_controller(self) -> EpisodeController:
        controller = self._rollout_controller()
        controller.handle(EpisodeEvent.TAKEOVER_REQUESTED)
        controller.handle(EpisodeEvent.HUMAN_STARTED)
        controller.handle(EpisodeEvent.HUMAN_FINISHED)
        return controller

    def _assert_rejected(self, controller: EpisodeController, event: EpisodeEvent, state: EpisodeState) -> None:
        with self.assertRaises(InvalidEpisodeTransition):
            controller.handle(event)
        self.assertEqual(controller.state, state)

    def _assert_transition(self, transition, state: EpisodeState, effects: tuple[EpisodeEffect, ...]) -> None:
        self.assertEqual(transition.state, state)
        self.assertEqual(transition.effects, effects)


if __name__ == "__main__":
    unittest.main()
