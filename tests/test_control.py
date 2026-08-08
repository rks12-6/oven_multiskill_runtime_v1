from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from oven_runtime.common.errors import ErrorCode, RuntimeFault
from oven_runtime.edge.control_client import SshControlClient, validate_control_response
from oven_runtime.server.audit import TrialAuditStore
from oven_runtime.server.control_protocol import ControlDispatcher
from oven_runtime.server.fake_backend import FakePolicyBackend
from oven_runtime.server.runtime import PolicyRuntime


class ControlProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.runtime = PolicyRuntime(
            backend=FakePolicyBackend(),
            audit=TrialAuditStore(Path(self.temporary.name) / "audit"),
        )
        self.instance_id = str(uuid4())
        self.dispatcher = ControlDispatcher(self.runtime, self.instance_id)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_unknown_and_extra_arguments_are_rejected(self) -> None:
        unknown = self.dispatcher.execute(
            {"protocol_version": 1, "command": "shell", "arguments": {}}
        )
        self.assertFalse(unknown["ok"])
        self.assertEqual(unknown["error_code"], ErrorCode.INVALID_FIELD_VALUE.value)
        extra = self.dispatcher.execute(
            {"protocol_version": 1, "command": "status", "arguments": {"shell": "id"}}
        )
        self.assertFalse(extra["ok"])
        self.assertEqual(extra["error_code"], ErrorCode.UNEXPECTED_FIELD.value)

    def test_session_token_appears_only_in_open_session_result(self) -> None:
        self.dispatcher.execute(
            {"protocol_version": 1, "command": "prepare-skill", "arguments": {"skill": "open_door"}}
        )
        self.dispatcher.execute(
            {
                "protocol_version": 1,
                "command": "begin-trial",
                "arguments": {"trial_id": "control_trial_1", "root_seed": 4},
            }
        )
        opened = self.dispatcher.execute(
            {"protocol_version": 1, "command": "open-session", "arguments": {}}
        )
        self.assertIn("session_token", opened["result"])
        status = self.dispatcher.execute(
            {"protocol_version": 1, "command": "status", "arguments": {}}
        )
        self.assertNotIn("session_token", repr(status))

    def test_ssh_client_uses_argv_without_a_shell(self) -> None:
        calls: list[tuple[list[str], dict]] = []

        def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=(
                    '{"protocol_version":1,"ok":true,"command":"status",'
                    f'"server_instance_id":"{self.instance_id}","result":{{}}}}'
                ),
                stderr="",
            )

        response = SshControlClient("robot-surf", runner=runner).call("status")
        self.assertTrue(response["ok"])
        argv, kwargs = calls[0]
        self.assertEqual(argv[-1], "--stdin-json")
        self.assertIn("oven-serverctl", argv)
        self.assertIn("--timeout-sec", argv)
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(
            kwargs["input"],
            '{"protocol_version":1,"command":"status","arguments":{}}',
        )

    def test_ssh_argument_is_json_stdin_not_remote_shell_text(self) -> None:
        calls: list[tuple[list[str], dict]] = []

        def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=(
                    '{"protocol_version":1,"ok":true,"command":"abort",'
                    f'"server_instance_id":"{self.instance_id}","result":{{}}}}'
                ),
                stderr="",
            )

        dangerous = "reason; touch /tmp/must-not-run"
        SshControlClient("robot-surf", runner=runner).call("abort", dangerous)
        argv, kwargs = calls[0]
        self.assertNotIn(dangerous, argv)
        self.assertEqual(json.loads(kwargs["input"])["arguments"]["reason"], dangerous)

    def test_failed_control_response_becomes_stable_fault(self) -> None:
        payload = {
            "protocol_version": 1,
            "ok": False,
            "command": "status",
            "server_instance_id": self.instance_id,
            "error_code": ErrorCode.STATE_REJECTED.value,
            "message": "not ready",
        }
        with self.assertRaises(RuntimeFault) as raised:
            validate_control_response("status", payload)
        self.assertEqual(raised.exception.code, ErrorCode.STATE_REJECTED)

    def test_unexpected_control_response_field_is_rejected(self) -> None:
        payload = {
            "protocol_version": 1,
            "ok": True,
            "command": "status",
            "server_instance_id": self.instance_id,
            "result": {},
            "session_token": "must-not-be-accepted-here",
        }
        with self.assertRaises(RuntimeFault) as raised:
            validate_control_response("status", payload)
        self.assertEqual(raised.exception.code, ErrorCode.UNEXPECTED_FIELD)


if __name__ == "__main__":
    unittest.main()
