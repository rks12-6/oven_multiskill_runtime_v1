#!/usr/bin/env python3
"""Record measured OMR joint-return windows from ROS 2 without publishing commands.

This tool is intentionally independent of the runtime pipeline. It subscribes to
one JointState topic, records the first seven measured joint positions, and
writes a new JSON evidence file after the bounded observation interval ends.
It never creates a publisher, calls a policy server, or starts an Auto run.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from pathlib import Path
from typing import Any

import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import JointState


SKILL_ARMS = {
    "open_door": ("left", "/puppet/joint_left"),
    "transport_food": ("left", "/puppet/joint_left"),
    "close_door": ("left", "/puppet/joint_left"),
    "rotate_button": ("right", "/puppet/joint_right"),
}
LABELS = (
    "unlabeled",
    "positive_return",
    "negative_no_return",
    "negative_unstable",
    "negative_wrong_arm",
)


class JointWindowRecorder(Node):
    """Read-only ROS subscriber for one skill and one measured arm topic."""

    def __init__(
        self,
        *,
        skill: str,
        arm: str,
        topic: str,
        run_id: str,
        trial_id: str,
        stage: str,
        label: str,
        note: str,
    ) -> None:
        super().__init__(f"omr_joint_window_recorder_{os.getpid()}")
        self.skill = skill
        self.arm = arm
        self.topic = topic
        self.run_id = run_id
        self.trial_id = trial_id
        self.stage = stage
        self.label = label
        self.note = note
        self.started_monotonic_ns = time.monotonic_ns()
        self.records: list[dict[str, Any]] = []
        self._subscription = self.create_subscription(
            JointState,
            topic,
            self._joint_callback,
            qos_profile_sensor_data,
        )

    def _joint_callback(self, message: JointState) -> None:
        position = tuple(float(value) for value in message.position[:7])
        if len(position) != 7 or not all(math.isfinite(value) for value in position):
            return

        received_monotonic_ns = time.monotonic_ns()
        stamp = message.header.stamp
        ros_header_stamp_ns = int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)
        self.records.append(
            {
                "sample_index": len(self.records),
                "received_monotonic_ns": received_monotonic_ns,
                "ros_header_stamp_ns": ros_header_stamp_ns,
                "position": list(position),
            }
        )

    def evidence(self, stopped_monotonic_ns: int) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "recorder": "record_omr_joint_return_window",
            "read_only": True,
            "skill": self.skill,
            "arm": self.arm,
            "topic": self.topic,
            "run_id": self.run_id,
            "trial_id": self.trial_id,
            "stage": self.stage,
            "label": self.label,
            "note": self.note,
            "started_monotonic_ns": self.started_monotonic_ns,
            "stopped_monotonic_ns": stopped_monotonic_ns,
            "sample_count": len(self.records),
            "records": self.records,
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only OMR JointState window recorder; never publishes robot commands."
    )
    parser.add_argument("--skill", choices=tuple(SKILL_ARMS), required=True)
    parser.add_argument("--duration-sec", type=float, default=15.0)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--trial-id", default="")
    parser.add_argument("--stage", default="")
    parser.add_argument("--label", choices=LABELS, default="unlabeled")
    parser.add_argument("--note", default="")
    return parser


def _write_new_json(path: Path, value: dict[str, Any]) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write("\n")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not math.isfinite(args.duration_sec) or args.duration_sec <= 0:
        raise SystemExit("--duration-sec must be a positive finite number")

    arm, topic = SKILL_ARMS[args.skill]
    rclpy.init()
    node = JointWindowRecorder(
        skill=args.skill,
        arm=arm,
        topic=topic,
        run_id=args.run_id,
        trial_id=args.trial_id,
        stage=args.stage,
        label=args.label,
        note=args.note,
    )
    deadline = time.monotonic() + args.duration_sec
    try:
        while rclpy.ok() and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=min(0.1, max(0.0, deadline - time.monotonic())))
    except KeyboardInterrupt:
        pass
    finally:
        stopped_monotonic_ns = time.monotonic_ns()
        evidence = node.evidence(stopped_monotonic_ns)
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()

    _write_new_json(args.output, evidence)
    print(
        json.dumps(
            {
                "output": str(args.output.expanduser()),
                "skill": args.skill,
                "arm": arm,
                "topic": topic,
                "sample_count": evidence["sample_count"],
                "label": args.label,
                "read_only": True,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
