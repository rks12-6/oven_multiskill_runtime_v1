from __future__ import annotations

import unittest
from typing import Any

from oven_runtime.edge.contracts import GateResult, PublishResult
from oven_runtime.v2.control import DirectControlAdapter


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object | None]] = []
        self.result = PublishResult(
            stop_requested=True,
            reason="joint_rest_detected",
            published_rows=2,
            gate_result=GateResult(True, 3, 0.0, 0.0, "stable_target_window"),
        )
        self.failure: BaseException | None = None

    def _record(self, method: str, value: object | None = None) -> None:
        self.calls.append((method, value))
        if self.failure is not None:
            raise self.failure

    def preflight(self) -> None:
        self._record("preflight")

    def reset(self, skill: str) -> None:
        self._record("reset", skill)

    def begin_stage(self, skill: str) -> None:
        self._record("begin_stage", skill)

    def publish(self, actions: Any) -> PublishResult:
        self._record("publish", actions)
        return self.result

    def stop(self, reason: str) -> None:
        self._record("stop", reason)

    def close(self) -> None:
        self._record("close")


class DirectControlAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.executor = RecordingExecutor()
        self.adapter = DirectControlAdapter(self.executor)  # type: ignore[arg-type]

    def test_forwards_lifecycle_and_preserves_publish_and_gate_identity(self) -> None:
        actions = [[1.0] * 7, [2.0] * 7]

        self.adapter.preflight()
        self.adapter.reset("open_door")
        self.adapter.begin_stage("open_door")
        result = self.adapter.publish(actions)
        self.adapter.stop("rollout_complete")
        self.adapter.close()

        self.assertEqual(
            self.executor.calls,
            [
                ("preflight", None),
                ("reset", "open_door"),
                ("begin_stage", "open_door"),
                ("publish", actions),
                ("stop", "rollout_complete"),
                ("close", None),
            ],
        )
        self.assertIs(self.executor.calls[3][1], actions)
        self.assertIs(result, self.executor.result)
        self.assertIs(result.gate_result, self.executor.result.gate_result)

    def test_exceptions_propagate_unchanged_for_every_delegated_method(self) -> None:
        calls = (
            ("preflight", lambda: self.adapter.preflight()),
            ("reset", lambda: self.adapter.reset("close_door")),
            ("begin_stage", lambda: self.adapter.begin_stage("close_door")),
            ("publish", lambda: self.adapter.publish([[0.0] * 7])),
            ("stop", lambda: self.adapter.stop("failure")),
            ("close", lambda: self.adapter.close()),
        )
        for method, invoke in calls:
            with self.subTest(method=method):
                expected = RuntimeError(method)
                self.executor.failure = expected
                with self.assertRaises(RuntimeError) as raised:
                    invoke()
                self.assertIs(raised.exception, expected)
                self.executor.failure = None


if __name__ == "__main__":
    unittest.main()
