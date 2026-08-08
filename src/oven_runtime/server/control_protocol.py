from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from oven_runtime.common.errors import ErrorCode, RuntimeFault, fault
from oven_runtime.common.protocol import PROTOCOL_VERSION
from oven_runtime.server.runtime import PolicyRuntime

CONTROL_COMMANDS = frozenset(
    {
        "status",
        "prepare-skill",
        "begin-trial",
        "open-session",
        "close-session",
        "end-trial",
        "abort",
    }
)


@dataclass(frozen=True)
class ControlRequest:
    command: str
    arguments: dict[str, Any]

    @classmethod
    def parse(cls, payload: Any) -> ControlRequest:
        if not isinstance(payload, Mapping):
            raise fault(ErrorCode.INVALID_FIELD_TYPE, "control request must be a mapping")
        expected = {"protocol_version", "command", "arguments"}
        missing = sorted(expected - set(payload))
        if missing:
            raise fault(ErrorCode.MISSING_REQUIRED_FIELD, "control request is missing required fields", fields=missing)
        unexpected = sorted(set(payload) - expected)
        if unexpected:
            raise fault(ErrorCode.UNEXPECTED_FIELD, "control request contains unexpected fields", fields=unexpected)
        version = payload["protocol_version"]
        if not isinstance(version, int) or isinstance(version, bool):
            raise fault(ErrorCode.INVALID_FIELD_TYPE, "protocol_version must be an integer")
        if version != PROTOCOL_VERSION:
            raise fault(ErrorCode.PROTOCOL_VERSION_UNSUPPORTED, "unsupported protocol version")
        command = payload["command"]
        arguments = payload["arguments"]
        if not isinstance(command, str):
            raise fault(ErrorCode.INVALID_FIELD_TYPE, "command must be a string")
        if command not in CONTROL_COMMANDS:
            raise fault(ErrorCode.INVALID_FIELD_VALUE, "control command is not supported")
        if not isinstance(arguments, dict):
            raise fault(ErrorCode.INVALID_FIELD_TYPE, "arguments must be a dictionary")
        return cls(command=command, arguments=arguments)


def _expect_arguments(arguments: dict[str, Any], required: set[str]) -> None:
    missing = sorted(required - set(arguments))
    if missing:
        raise fault(ErrorCode.MISSING_REQUIRED_FIELD, "control arguments are missing required fields", fields=missing)
    unexpected = sorted(set(arguments) - required)
    if unexpected:
        raise fault(ErrorCode.UNEXPECTED_FIELD, "control arguments contain unexpected fields", fields=unexpected)


def _string(arguments: dict[str, Any], name: str) -> str:
    value = arguments[name]
    if not isinstance(value, str):
        raise fault(ErrorCode.INVALID_FIELD_TYPE, "control argument must be a string", field=name)
    if not value:
        raise fault(ErrorCode.INVALID_FIELD_VALUE, "control argument must not be empty", field=name)
    return value


def _integer(arguments: dict[str, Any], name: str) -> int:
    value = arguments[name]
    if not isinstance(value, int) or isinstance(value, bool):
        raise fault(ErrorCode.INVALID_FIELD_TYPE, "control argument must be an integer", field=name)
    return value


class ControlDispatcher:
    def __init__(self, runtime: PolicyRuntime, server_instance_id: str) -> None:
        self._runtime = runtime
        self._server_instance_id = server_instance_id

    def execute(self, payload: Any) -> dict[str, Any]:
        command = "unknown"
        try:
            request = ControlRequest.parse(payload)
            command = request.command
            result = self._dispatch(request)
            return self._response(command=command, ok=True, result=result)
        except RuntimeFault as exc:
            return self._response(command=command, ok=False, error=exc)
        except Exception:
            return self._response(
                command=command,
                ok=False,
                error=fault(ErrorCode.SERVER_FAILED, "control command failed inside the server"),
            )

    def error_response(self, error: RuntimeFault) -> dict[str, Any]:
        return self._response(command="unknown", ok=False, error=error)

    def _dispatch(self, request: ControlRequest) -> dict[str, Any]:
        command = request.command
        arguments = request.arguments
        no_arguments: dict[str, Callable[[], dict[str, Any]]] = {
            "status": self._runtime.status,
            "open-session": self._runtime.open_session,
            "end-trial": self._runtime.end_trial,
        }
        if command in no_arguments:
            _expect_arguments(arguments, set())
            return no_arguments[command]()
        if command == "prepare-skill":
            _expect_arguments(arguments, {"skill"})
            return self._runtime.prepare_skill(_string(arguments, "skill"))
        if command == "begin-trial":
            _expect_arguments(arguments, {"trial_id", "root_seed"})
            return self._runtime.begin_trial(
                trial_id=_string(arguments, "trial_id"),
                root_seed=_integer(arguments, "root_seed"),
            )
        if command == "close-session":
            _expect_arguments(arguments, {"session_id"})
            return self._runtime.close_session(_string(arguments, "session_id"))
        if command == "abort":
            _expect_arguments(arguments, {"reason"})
            return self._runtime.abort(_string(arguments, "reason"))
        raise fault(ErrorCode.INVALID_FIELD_VALUE, "control command is not implemented")

    def _response(
        self,
        *,
        command: str,
        ok: bool,
        result: dict[str, Any] | None = None,
        error: RuntimeFault | None = None,
    ) -> dict[str, Any]:
        response: dict[str, Any] = {
            "protocol_version": PROTOCOL_VERSION,
            "ok": ok,
            "command": command,
            "server_instance_id": self._server_instance_id,
        }
        if ok:
            response["result"] = result
        else:
            if error is None:
                error = fault(ErrorCode.SERVER_FAILED, "control command failed")
            response.update(error.to_dict())
        return response
