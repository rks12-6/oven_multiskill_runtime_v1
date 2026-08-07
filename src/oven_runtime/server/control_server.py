from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import stat
from pathlib import Path
from typing import Any

from oven_runtime.common.errors import ErrorCode, RuntimeFault, fault
from oven_runtime.server.control_protocol import ControlDispatcher

MAX_CONTROL_FRAME_BYTES = 64 * 1024
CONTROL_TIMEOUT_SEC = 10.0
LOGGER = logging.getLogger(__name__)


class UnixControlServer:
    """A one-request-per-connection, owner-only Unix control socket."""

    def __init__(self, path: Path, dispatcher: ControlDispatcher) -> None:
        if not path.is_absolute():
            raise ValueError("control socket path must be absolute")
        self.path = path
        self._dispatcher = dispatcher
        self._server: asyncio.AbstractServer | None = None
        self._lock_fd: int | None = None

    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        self._acquire_owner_lock()
        self._remove_stale_socket()
        try:
            self._server = await asyncio.start_unix_server(
                self._handle_connection,
                path=self.path,
                limit=MAX_CONTROL_FRAME_BYTES + 1,
            )
            os.chmod(self.path, 0o600)
            actual_mode = stat.S_IMODE(self.path.stat().st_mode)
            if actual_mode != 0o600:
                raise PermissionError(f"control socket mode is {oct(actual_mode)}, expected 0o600")
        except Exception:
            await self.stop()
            raise

    async def stop(self) -> None:
        owns_path = self._lock_fd is not None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if owns_path and self.path.exists() and stat.S_ISSOCK(self.path.lstat().st_mode):
            self.path.unlink()
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None

    def _acquire_owner_lock(self) -> None:
        lock_path = self.path.parent / "oven-server.lock"
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(lock_fd)
            raise RuntimeError(f"another control server owns {lock_path}") from exc
        self._lock_fd = lock_fd

    def _remove_stale_socket(self) -> None:
        if not self.path.exists():
            return
        if not stat.S_ISSOCK(self.path.lstat().st_mode):
            raise FileExistsError(f"refusing to replace non-socket control path: {self.path}")
        self.path.unlink()

    async def _read_frame(self, reader: asyncio.StreamReader) -> bytes:
        try:
            return await asyncio.wait_for(reader.readuntil(b"\n"), timeout=CONTROL_TIMEOUT_SEC)
        except asyncio.LimitOverrunError as exc:
            raise fault(ErrorCode.FRAME_TOO_LARGE, "control request exceeds its framing limit") from exc
        except asyncio.IncompleteReadError as exc:
            if len(exc.partial) > MAX_CONTROL_FRAME_BYTES:
                raise fault(ErrorCode.FRAME_TOO_LARGE, "control request exceeds its framing limit") from exc
            raise fault(ErrorCode.FRAME_DECODE_FAILED, "control request ended before its newline delimiter") from exc

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                frame = await self._read_frame(reader)
                if not frame:
                    raise fault(ErrorCode.FRAME_DECODE_FAILED, "empty control request")
                if len(frame) > MAX_CONTROL_FRAME_BYTES or not frame.endswith(b"\n"):
                    raise fault(ErrorCode.FRAME_TOO_LARGE, "control request exceeds its framing limit")
                try:
                    payload: Any = json.loads(frame)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise fault(ErrorCode.FRAME_DECODE_FAILED, "control request is not valid UTF-8 JSON") from exc
                response = self._dispatcher.execute(payload)
            except RuntimeFault as exc:
                response = self._dispatcher.error_response(exc)
            except Exception:
                LOGGER.exception("unexpected control server failure")
                response = self._dispatcher.error_response(
                    fault(ErrorCode.SERVER_FAILED, "control server failed to process request")
                )
            encoded = (json.dumps(response, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
            writer.write(encoded)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
