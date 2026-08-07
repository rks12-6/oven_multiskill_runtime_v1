from __future__ import annotations

import asyncio
import ipaddress
import logging
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from oven_runtime.common.errors import ErrorCode, RuntimeFault, fault
from oven_runtime.common.protocol import PROTOCOL_VERSION
from oven_runtime.common.wire import MAX_FRAME_BYTES, pack_message, unpack_message
from oven_runtime.server.runtime import PolicyRuntime

LOGGER = logging.getLogger(__name__)


class LoopbackInferenceServer:
    """Binary-only WebSocket front end; deliberately refuses non-loopback binds."""

    def __init__(
        self,
        runtime: PolicyRuntime,
        *,
        host: str = "127.0.0.1",
        port: int = 19110,
        max_frame_bytes: int = MAX_FRAME_BYTES,
    ) -> None:
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError("inference host must be a literal loopback IP address") from exc
        if not address.is_loopback:
            raise ValueError("inference server may bind only to a loopback address")
        if not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        if not 0 < max_frame_bytes <= MAX_FRAME_BYTES:
            raise ValueError("max_frame_bytes must be between 1 and 64 MiB")
        self._runtime = runtime
        self.host = host
        self.port = port
        self.max_frame_bytes = max_frame_bytes
        self._server: Server | None = None

    @property
    def bound_port(self) -> int:
        if self._server is None or not self._server.sockets:
            raise RuntimeError("inference server is not running")
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        self._server = await serve(
            self._handle_connection,
            self.host,
            self.port,
            compression=None,
            max_size=self.max_frame_bytes,
            max_queue=1,
            ping_interval=20,
            ping_timeout=20,
        )
        for listening_socket in self._server.sockets:
            bound_host = listening_socket.getsockname()[0]
            if not ipaddress.ip_address(bound_host).is_loopback:
                await self.stop()
                raise RuntimeError("inference listener self-check found a non-loopback address")

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle_connection(self, connection: ServerConnection) -> None:
        try:
            async for frame in connection:
                response = await self._process_frame(frame)
                try:
                    encoded = pack_message(response, max_bytes=self.max_frame_bytes)
                    await connection.send(encoded)
                except Exception:
                    if response.get("message_type") == "inference_response":
                        self._runtime.mark_transport_ambiguous(
                            session_id=response["session_id"],
                            sequence=response["sequence"],
                        )
                    LOGGER.exception("failed to return inference response")
                    return
        except ConnectionClosed:
            return

    async def _process_frame(self, frame: Any) -> dict[str, Any]:
        try:
            payload = unpack_message(frame, max_bytes=self.max_frame_bytes)
            return await asyncio.to_thread(self._runtime.infer, payload)
        except RuntimeFault as exc:
            return {
                "protocol_version": PROTOCOL_VERSION,
                "message_type": "inference_error",
                **exc.to_dict(),
            }
        except Exception:
            LOGGER.exception("unexpected inference server failure")
            error = fault(ErrorCode.SERVER_FAILED, "inference server failed to process request")
            return {
                "protocol_version": PROTOCOL_VERSION,
                "message_type": "inference_error",
                **error.to_dict(),
            }
