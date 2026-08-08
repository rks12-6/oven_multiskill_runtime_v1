from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from oven_runtime.common.errors import ErrorCode, RuntimeFault, fault
from oven_runtime.server.control_client import control_exchange, control_request
from oven_runtime.server.control_server import MAX_CONTROL_FRAME_BYTES


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oven-serverctl")
    parser.add_argument(
        "--socket",
        type=Path,
        default=None,
        help="Unix socket path; defaults to $OVEN_RUNTIME_ROOT/control/oven-server.sock",
    )
    parser.add_argument("--stdin-json", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--timeout-sec", type=float, default=180.0)
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("status")
    prepare = subparsers.add_parser("prepare-skill")
    prepare.add_argument("skill")
    begin = subparsers.add_parser("begin-trial")
    begin.add_argument("trial_id")
    begin.add_argument("root_seed", type=int)
    subparsers.add_parser("open-session")
    close = subparsers.add_parser("close-session")
    close.add_argument("session_id")
    subparsers.add_parser("end-trial")
    abort = subparsers.add_parser("abort")
    abort.add_argument("reason")
    return parser


def _arguments(namespace: argparse.Namespace) -> dict[str, Any]:
    if namespace.command == "prepare-skill":
        return {"skill": namespace.skill}
    if namespace.command == "begin-trial":
        return {"trial_id": namespace.trial_id, "root_seed": namespace.root_seed}
    if namespace.command == "close-session":
        return {"session_id": namespace.session_id}
    if namespace.command == "abort":
        return {"reason": namespace.reason}
    return {}


def _socket_path(namespace: argparse.Namespace, parser: argparse.ArgumentParser) -> Path:
    if namespace.socket is not None:
        return namespace.socket.expanduser().resolve()
    runtime_root = os.environ.get("OVEN_RUNTIME_ROOT", "").strip()
    if not runtime_root:
        parser.error("--socket or OVEN_RUNTIME_ROOT is required")
    return Path(runtime_root).expanduser().resolve() / "control" / "oven-server.sock"


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    namespace = parser.parse_args(argv)
    socket_path = _socket_path(namespace, parser)
    try:
        if namespace.stdin_json:
            if namespace.command is not None:
                parser.error("--stdin-json cannot be combined with a subcommand")
            raw = sys.stdin.buffer.read(MAX_CONTROL_FRAME_BYTES + 1)
            if len(raw) > MAX_CONTROL_FRAME_BYTES:
                raise fault(ErrorCode.FRAME_TOO_LARGE, "stdin control request exceeds its framing limit")
            try:
                payload = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise fault(ErrorCode.FRAME_DECODE_FAILED, "stdin control request is not valid UTF-8 JSON") from exc
            response = control_exchange(socket_path, payload, timeout_sec=namespace.timeout_sec)
        else:
            if namespace.command is None:
                parser.error("a subcommand is required")
            response = control_request(
                socket_path,
                namespace.command,
                _arguments(namespace),
                timeout_sec=namespace.timeout_sec,
            )
    except RuntimeFault as exc:
        response = exc.to_dict()
    print(json.dumps(response, separators=(",", ":"), ensure_ascii=False))
    return 0 if response.get("ok") is True else 1


if __name__ == "__main__":
    sys.exit(main())
