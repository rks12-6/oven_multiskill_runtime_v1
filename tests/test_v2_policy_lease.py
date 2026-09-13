from __future__ import annotations

from threading import Event
import unittest
from pathlib import Path

import rclpy

from oven_runtime.v2.hitl_ros import RosHitlTransport
from oven_runtime.v2.profile import load_hitl_v2_profile


ROOT = Path(__file__).resolve().parents[1]
HITL_PROFILE = ROOT / "config" / "hitl_v2.toml"


class RecordingPublisher:
    def __init__(self) -> None:
        self.messages: list[object] = []

    def publish(self, message: object) -> None:
        self.messages.append(message)


class PolicyLeaseWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        rclpy.init()
        profile = load_hitl_v2_profile(HITL_PROFILE)
        self.transport = RosHitlTransport(profile.arm_pair("right_hitl"))
        self.policy_messages = RecordingPublisher()
        self.lease_messages = RecordingPublisher()
        self.transport._policy_publisher = self.policy_messages  # type: ignore[assignment]
        self.transport._policy_lease_publisher = self.lease_messages  # type: ignore[assignment]

    def tearDown(self) -> None:
        self.transport.close()
        rclpy.shutdown()

    def test_live_controller_exceeding_progress_deadline_stops_lease_without_joint_command(self) -> None:
        self.transport.start_policy_lease(7, interval_sec=0.001, progress_timeout_sec=0.01)
        Event().wait(0.05)

        with self.assertRaisesRegex(RuntimeError, "policy progress stale"):
            self.transport.ensure_policy_lease_healthy()

        self.assertGreaterEqual(len(self.lease_messages.messages), 1)
        self.assertEqual(self.policy_messages.messages, [])
        self.transport.stop_policy_lease()
        self.assertIsNone(self.transport._lease_thread)


if __name__ == "__main__":
    unittest.main()
