from __future__ import annotations

import json
import re
import subprocess
import threading
from pathlib import PurePosixPath
from collections.abc import Callable, Sequence
from typing import Any
from uuid import UUID

from oven_runtime.common.errors import ErrorCode, RuntimeFault, fault
from oven_runtime.common.protocol import PROTOCOL_VERSION


class SshControlClient:
    """Run the fixed serverctl program over SSH without invoking a local shell."""

    def __init__(
        self,
        host_alias: str,
        *,
        remote_program: str = "oven-serverctl",
        remote_runtime_root: str | None = None,
        timeout_sec: float = 180.0,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", host_alias) is None:
            raise ValueError("host_alias must be one SSH host token")
        if re.fullmatch(r"/?[A-Za-z0-9_./-]+", remote_program) is None or ".." in remote_program.split("/"):
            raise ValueError("remote_program must be one executable token")
        if timeout_sec <= 0:
            raise ValueError("timeout_sec must be positive")
        self.host_alias = host_alias
        self.remote_program = remote_program
        if remote_runtime_root is not None:
            root = PurePosixPath(remote_runtime_root)
            if not root.is_absolute() or ".." in root.parts:
                raise ValueError("remote_runtime_root must be a normalized absolute POSIX path")
        self.remote_runtime_root = remote_runtime_root
        self.timeout_sec = timeout_sec
        self._runner = runner
        self._server_instance_id: str | None = None
        self._instance_lock = threading.Lock()

    def call(self, command: str, *arguments: str | int) -> dict[str, Any]:
        control_arguments = _command_arguments(command, arguments)
        request = json.dumps(
            {
                "protocol_version": PROTOCOL_VERSION,
                "command": command,
                "arguments": control_arguments,
            },
            separators=(",", ":"),
        )
        argv = [
            "ssh",
            "-o",
            "BatchMode=yes",
            self.host_alias,
            self.remote_program,
        ]
        if self.remote_runtime_root is not None:
            argv.extend(["--socket", f"{self.remote_runtime_root}/control/oven-server.sock"])
        argv.extend(["--timeout-sec", str(self.timeout_sec)])
        argv.extend([
            "--stdin-json",
        ])
        try:
            completed = self._runner(
                argv,
                shell=False,
                check=False,
                capture_output=True,
                input=request,
                text=True,
                timeout=self.timeout_sec,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise fault(ErrorCode.CONTROL_SERVER_UNAVAILABLE, "SSH control command did not complete") from exc
        if completed.returncode not in {0, 1}:
            raise fault(ErrorCode.CONTROL_SERVER_UNAVAILABLE, "SSH control process failed")
        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise fault(ErrorCode.FRAME_DECODE_FAILED, "SSH control response is not valid JSON") from exc
        validated = validate_control_response(command, response)
        instance_id = validated["server_instance_id"]
        with self._instance_lock:
            if self._server_instance_id is None:
                self._server_instance_id = instance_id
            elif self._server_instance_id != instance_id:
                raise fault(ErrorCode.RESPONSE_MISMATCH, "control server instance changed during this client run")
        return validated


def _command_arguments(command: str, values: Sequence[str | int]) -> dict[str, Any]:
    names = {
        "status": (),
        "prepare-skill": ("skill",),
        "begin-trial": ("trial_id", "root_seed"),
        "open-session": (),
        "close-session": ("session_id",),
        "end-trial": (),
        "abort": ("reason",),
    }
    expected = names.get(command)
    if expected is None or len(expected) != len(values):
        raise ValueError("control command or argument count is invalid")
    result = dict(zip(expected, values, strict=True))
    for integer_name in ("root_seed", "seed"):
        if integer_name in result:
            value = result[integer_name]
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{integer_name} must be an integer")
    return result


def validate_control_response(command: str, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise fault(ErrorCode.FRAME_DECODE_FAILED, "control response must be a dictionary")
    for field in ("protocol_version", "ok", "command", "server_instance_id"):
        if field not in payload:
            raise fault(ErrorCode.MISSING_REQUIRED_FIELD, "control response is missing required fields", field=field)
    if payload["protocol_version"] != PROTOCOL_VERSION or payload["command"] != command:
        raise fault(ErrorCode.RESPONSE_MISMATCH, "control response identity does not match its request")
    if not isinstance(payload["ok"], bool):
        raise fault(ErrorCode.INVALID_FIELD_TYPE, "control response ok field must be boolean")
    try:
        UUID(payload["server_instance_id"])
    except (TypeError, ValueError) as exc:
        raise fault(ErrorCode.INVALID_FIELD_VALUE, "control response server_instance_id must be a UUID") from exc
    if payload["ok"]:
        unexpected = sorted(set(payload) - {"protocol_version", "ok", "command", "server_instance_id", "result"})
        if unexpected:
            raise fault(ErrorCode.UNEXPECTED_FIELD, "control response contains unexpected fields", fields=unexpected)
        if "result" not in payload or not isinstance(payload["result"], dict):
            raise fault(ErrorCode.MISSING_REQUIRED_FIELD, "successful control response has no result")
        return payload
    unexpected = sorted(
        set(payload)
        - {"protocol_version", "ok", "command", "server_instance_id", "error_code", "message", "details"}
    )
    if unexpected:
        raise fault(ErrorCode.UNEXPECTED_FIELD, "control error response contains unexpected fields", fields=unexpected)
    code_text = payload.get("error_code")
    try:
        code = ErrorCode(code_text)
    except (TypeError, ValueError):
        code = ErrorCode.SERVER_FAILED
    message = payload.get("message")
    if not isinstance(message, str):
        message = "remote control command failed"
    raise RuntimeFault(code, message)
