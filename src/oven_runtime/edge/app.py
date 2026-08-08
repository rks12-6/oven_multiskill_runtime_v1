from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit

from oven_runtime.edge.approval import ConsoleApprovalGate
from oven_runtime.edge.audit import EdgeAuditStore
from oven_runtime.edge.profile import load_agilex_profile


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oven-edge")
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--runtime-root", type=Path, default=None)
    parser.add_argument("--asset-root", type=Path, default=None)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--observe-once", action="store_true")
    mode.add_argument("--execute", action="store_true")
    return parser


def _root(
    value: Path | None,
    *,
    option_name: str,
    environment_name: str,
    parser: argparse.ArgumentParser,
) -> Path:
    if value is not None:
        return value.expanduser().resolve()
    configured = os.environ.get(environment_name, "").strip()
    if not configured:
        parser.error(f"{option_name} or {environment_name} is required")
    return Path(configured).expanduser().resolve()


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    runtime_root = _root(
        args.runtime_root,
        option_name="--runtime-root",
        environment_name="OVEN_RUNTIME_ROOT",
        parser=parser,
    )
    profile_path = args.profile.expanduser().resolve()
    run_id = args.run_id or f"{datetime.now():%Y%m%d_%H%M%S}_{profile_path.stem}"
    profile = load_agilex_profile(profile_path, run_id=run_id, runtime_root=runtime_root)
    if args.plan:
        print(
            json.dumps(
                {
                    "run_id": profile.plan.run_id,
                    "skills": [stage.skill for stage in profile.plan.stages],
                    "reset_before": {stage.skill: stage.reset_before for stage in profile.plan.stages},
                    "server_host_alias": profile.server_host_alias,
                    "inference_uri": profile.inference_uri,
                    "physical_actions_enabled": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    from oven_runtime.edge.joint_gate import OnlineJointGate
    from oven_runtime.edge.ros2_observation import RosObservationSource
    from oven_runtime.edge.validation import ObservationValidator

    online_gate = OnlineJointGate(profile.gates)
    if args.observe_once:
        observation = RosObservationSource(profile.ros, online_gate)
        try:
            observation.preflight()
            validator = ObservationValidator(profile.observation_max_age_ms)
            samples: dict[str, object] = {}
            for stage in profile.plan.stages:
                captured = observation.sample(stage.skill)
                validator.validate(captured)
                state = captured.payload["state"]
                images = captured.payload["images"]
                samples[stage.skill] = {
                    "captured_age_ms": (time.monotonic_ns() - captured.captured_at_monotonic_ns) / 1_000_000,
                    "state_shape": list(state.shape),
                    "state_dtype": str(state.dtype),
                    "image_shapes": {name: list(value.shape) for name, value in images.items()},
                    "image_dtypes": {name: str(value.dtype) for name, value in images.items()},
                    "prompt": captured.payload["prompt"],
                }
            print(
                json.dumps(
                    {
                        "run_id": profile.plan.run_id,
                        "mode": "observe_once",
                        "physical_actions_enabled": False,
                        "samples": samples,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        finally:
            observation.close()

    uri = urlsplit(profile.inference_uri)
    if uri.scheme != "ws" or uri.hostname not in {"127.0.0.1", "::1"} or uri.port is None:
        parser.error("profile local_inference_uri must be a loopback ws:// URI with an explicit port")

    # Keep ROS 2 and Torch out of the read-only planning path.  They are
    # execution-time dependencies available on the Agilex edge host.
    from oven_runtime.edge.checker import TorchResnetChecker
    from oven_runtime.edge.control_client import SshControlClient
    from oven_runtime.edge.inference_client import InferenceClient
    from oven_runtime.edge.orchestrator import PipelineOrchestrator
    from oven_runtime.edge.ros2_action import RosActionExecutor
    from oven_runtime.edge.tunnel import SshInferenceTunnel
    from oven_runtime.edge.validation import ActionValidator

    asset_root = _root(
        args.asset_root,
        option_name="--asset-root",
        environment_name="OVEN_ASSET_ROOT",
        parser=parser,
    )
    tunnel = SshInferenceTunnel(
        host_alias=profile.server_host_alias,
        local_port=uri.port,
        remote_port=profile.remote_inference_port,
    )
    observation: RosObservationSource | None = None
    action_executor: RosActionExecutor | None = None
    checker: TorchResnetChecker | None = None
    try:
        tunnel.start()
        observation = RosObservationSource(profile.ros, online_gate)
        action_executor = RosActionExecutor(profile.ros, observation, online_gate)
        checker = TorchResnetChecker(
            asset_root=asset_root,
            specs=profile.checkers,
            capture_front_rgb=observation.front_rgb,
        )
        orchestrator = PipelineOrchestrator(
            plan=profile.plan,
            observation_source=observation,
            observation_validator=ObservationValidator(profile.observation_max_age_ms),
            reset_controller=action_executor,
            action_executor=action_executor,
            action_validator=ActionValidator(
                action_dimension=profile.action_dimension,
                chunk_rows=profile.action_chunk_rows,
                max_absolute_value=profile.action_max_absolute_value,
            ),
            joint_gate=online_gate,
            checker=checker,
            approval=ConsoleApprovalGate(execute_enabled=True),
            control=SshControlClient(
                profile.server_host_alias,
                remote_program=profile.remote_control_program,
                remote_runtime_root=str(profile.remote_runtime_root),
            ),
            inference=InferenceClient(profile.inference_uri, timeout_sec=profile.inference_timeout_sec),
            audit=EdgeAuditStore(runtime_root / "edge_audit"),
        )
        result = orchestrator.run()
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    finally:
        if checker is not None:
            checker.close()
        if action_executor is not None:
            action_executor.close()
        if observation is not None:
            observation.close()
        tunnel.close()


if __name__ == "__main__":
    sys.exit(main())
