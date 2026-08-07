from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

from oven_runtime.common.errors import ErrorCode, fault
from oven_runtime.common.protocol import PROTOCOL_VERSION
from oven_runtime.server.control_server import MAX_CONTROL_FRAME_BYTES


def control_request(path: Path, command: str, arguments: dict[str, Any], *, timeout_sec: float = 10.0) -> dict[str, Any]:
    return control_exchange(
        path,
        {
        "protocol_version": PROTOCOL_VERSION,
        "command": command,
        "arguments": arguments,
        },
        timeout_sec=timeout_sec,
    )


def control_exchange(path: Path, payload: Any, *, timeout_sec: float = 10.0) -> dict[str, Any]:
    encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) > MAX_CONTROL_FRAME_BYTES:
        raise fault(ErrorCode.FRAME_TOO_LARGE, "control request exceeds its framing limit")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout_sec)
    try:
        client.connect(str(path))
        client.sendall(encoded)
        chunks = bytearray()
        while not chunks.endswith(b"\n"):
            block = client.recv(min(4096, MAX_CONTROL_FRAME_BYTES + 1 - len(chunks)))
            if not block:
                raise fault(ErrorCode.CONTROL_SERVER_UNAVAILABLE, "control server closed without a response")
            chunks.extend(block)
            if len(chunks) > MAX_CONTROL_FRAME_BYTES:
                raise fault(ErrorCode.FRAME_TOO_LARGE, "control response exceeds its framing limit")
    except OSError as exc:
        raise fault(ErrorCode.CONTROL_SERVER_UNAVAILABLE, "cannot communicate with local control server") from exc
    finally:
        client.close()
    try:
        response = json.loads(chunks)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise fault(ErrorCode.FRAME_DECODE_FAILED, "control response is not valid UTF-8 JSON") from exc
    if not isinstance(response, dict):
        raise fault(ErrorCode.FRAME_DECODE_FAILED, "control response must be a dictionary")
    return response
