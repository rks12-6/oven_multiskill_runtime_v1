from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

from oven_runtime.common.config import resolve_under_root
from oven_runtime.server.audit import TrialAuditStore
from oven_runtime.server.control_protocol import ControlDispatcher
from oven_runtime.server.control_server import UnixControlServer
from oven_runtime.server.fake_backend import FakePolicyBackend
from oven_runtime.server.inference_server import LoopbackInferenceServer
from oven_runtime.server.runtime import PolicyRuntime


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oven-server")
    parser.add_argument("--runtime-root", type=Path, default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=19110)
    parser.add_argument("--ready-file", type=Path, default=None)
    parser.add_argument("--backend", choices=("fake", "openpi-composite"), default="fake")
    parser.add_argument("--backend-profile", type=Path, default=None)
    parser.add_argument("--artifact-root", type=Path, default=None)
    parser.add_argument("--checkpoint-root", type=Path, default=None)
    return parser


def _runtime_root(value: Path | None, parser: argparse.ArgumentParser) -> Path:
    if value is not None:
        return value.expanduser().resolve()
    configured = os.environ.get("OVEN_RUNTIME_ROOT", "").strip()
    if not configured:
        parser.error("--runtime-root or OVEN_RUNTIME_ROOT is required")
    return Path(configured).expanduser().resolve()


async def run_server(
    runtime_root: Path,
    host: str,
    port: int,
    ready_file: Path | None,
    *,
    backend: object | None = None,
) -> None:
    runtime_root.mkdir(parents=True, exist_ok=True)
    os.chmod(runtime_root, 0o700)
    runtime = PolicyRuntime(
        backend=backend or FakePolicyBackend(),
        audit=TrialAuditStore(runtime_root / "audit"),
    )
    instance_id = str(uuid4())
    control = UnixControlServer(
        runtime_root / "control" / "oven-server.sock",
        ControlDispatcher(runtime, instance_id),
    )
    inference = LoopbackInferenceServer(runtime, host=host, port=port)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop_event.set)
    try:
        await control.start()
        await inference.start()
        if ready_file is not None:
            ready_file = resolve_under_root(runtime_root, str(ready_file))
            ready_file.parent.mkdir(parents=True, exist_ok=True)
            ready_file.write_text(
                json.dumps(
                    {
                        "server_instance_id": instance_id,
                        "control_socket": str(control.path),
                        "inference_host": host,
                        "inference_port": inference.bound_port,
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        await stop_event.wait()
    finally:
        await inference.stop()
        await control.stop()
        runtime.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    namespace = parser.parse_args(argv)
    root = _runtime_root(namespace.runtime_root, parser)
    backend: object = FakePolicyBackend()
    if namespace.backend == "openpi-composite":
        if namespace.backend_profile is None or namespace.artifact_root is None or namespace.checkpoint_root is None:
            parser.error("openpi-composite requires --backend-profile, --artifact-root, and --checkpoint-root")
        from oven_runtime.server.composite_backend import OpenPiCompositeBackend, load_composite_profile

        backend = OpenPiCompositeBackend(
            load_composite_profile(
                namespace.backend_profile,
                artifact_root=namespace.artifact_root,
                checkpoint_root=namespace.checkpoint_root,
            )
        )
    try:
        asyncio.run(run_server(root, namespace.host, namespace.port, namespace.ready_file, backend=backend))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
