from __future__ import annotations

import re
import socket
import subprocess
import time

from oven_runtime.common.errors import ErrorCode, fault


class SshInferenceTunnel:
    def __init__(
        self,
        *,
        host_alias: str,
        local_port: int,
        remote_port: int,
        startup_timeout_sec: float = 10.0,
    ) -> None:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", host_alias) is None:
            raise ValueError("host_alias must be one safe SSH token")
        if not 1 <= local_port <= 65535 or not 1 <= remote_port <= 65535:
            raise ValueError("SSH tunnel ports are invalid")
        if startup_timeout_sec <= 0:
            raise ValueError("startup_timeout_sec must be positive")
        self.host_alias = host_alias
        self.local_port = local_port
        self.remote_port = remote_port
        self.startup_timeout_sec = startup_timeout_sec
        self._process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        if self._process is not None:
            raise RuntimeError("SSH inference tunnel is already started")
        argv = [
            "ssh",
            "-N",
            "-o",
            "BatchMode=yes",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=10",
            "-o",
            "ServerAliveCountMax=3",
            "-L",
            f"127.0.0.1:{self.local_port}:127.0.0.1:{self.remote_port}",
            self.host_alias,
        ]
        try:
            self._process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise fault(ErrorCode.CONTROL_SERVER_UNAVAILABLE, "cannot start SSH inference tunnel") from exc
        deadline = time.monotonic() + self.startup_timeout_sec
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise fault(ErrorCode.CONTROL_SERVER_UNAVAILABLE, "SSH inference tunnel exited during startup")
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.settimeout(0.2)
            try:
                if probe.connect_ex(("127.0.0.1", self.local_port)) == 0:
                    return
            finally:
                probe.close()
            time.sleep(0.05)
        self.close()
        raise fault(ErrorCode.CONTROL_SERVER_UNAVAILABLE, "SSH inference tunnel did not become ready")

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3.0)
