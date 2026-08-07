from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np
from websockets.sync.client import connect

from oven_runtime.common.errors import ErrorCode
from oven_runtime.common.wire import unpack_message
from oven_runtime.edge.inference_client import InferenceClient
from oven_runtime.server.control_client import control_request
from tests.helpers import request_payload


class TwoProcessTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime_root = self.root / "runtime"
        self.ready_file = self.runtime_root / "ready.json"
        environment = os.environ.copy()
        source_root = Path(__file__).resolve().parents[1] / "src"
        environment["PYTHONPATH"] = str(source_root)
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "oven_runtime.server.app",
                "--runtime-root",
                str(self.runtime_root),
                "--port",
                "0",
                "--ready-file",
                "ready.json",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not self.ready_file.exists():
            if self.process.poll() is not None:
                stdout, stderr = self.process.communicate()
                self.fail(f"fake server exited before readiness\nstdout={stdout}\nstderr={stderr}")
            time.sleep(0.02)
        if not self.ready_file.exists():
            self.process.terminate()
            stdout, stderr = self.process.communicate(timeout=5)
            self.fail(f"fake server did not become ready\nstdout={stdout}\nstderr={stderr}")
        ready = json.loads(self.ready_file.read_text(encoding="utf-8"))
        self.socket_path = Path(ready["control_socket"])
        self.uri = f"ws://127.0.0.1:{ready['inference_port']}"

    def tearDown(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.communicate(timeout=5)
        self.temporary.cleanup()

    def control(self, command: str, arguments: dict) -> dict:
        response = control_request(self.socket_path, command, arguments)
        self.assertTrue(response["ok"], response)
        return response["result"]

    def test_full_warmup_rollout_and_audit_commit(self) -> None:
        self.assertEqual(stat.S_IMODE(self.socket_path.stat().st_mode), 0o600)
        self.control("prepare-skill", {"skill": "open_door"})
        self.control("begin-trial", {"trial_id": "integration_trial_1", "root_seed": 17})
        session = self.control("open-session", {})
        client = InferenceClient(self.uri)
        observation = {
            "state": np.arange(7, dtype=np.float32),
            "image": np.zeros((4, 5, 3), dtype=np.uint8),
        }
        warmup = client.infer(
            request_payload(session, kind="warmup", sequence=0, observation=observation)
        )
        self.assertFalse(warmup["publishable"])
        self.control("reset-prng", {"seed": 17})
        rollout = client.infer(
            request_payload(session, kind="infer", sequence=1, observation=observation)
        )
        self.assertTrue(rollout["publishable"])
        self.control("close-session", {"session_id": session["session_id"]})
        summary = self.control("end-trial", {})
        self.assertEqual(summary["status"], "COMMITTED")
        self.assertEqual(summary["request_count"], 2)
        audit = self.root / "runtime" / "audit" / "integration_trial_1"
        self.assertTrue((audit / "first_action_chunk.npy").is_file())
        self.assertNotIn(session["session_token"], (audit / "requests.jsonl").read_text(encoding="utf-8"))

    def test_text_websocket_frame_is_rejected_before_policy(self) -> None:
        before = self.control("status", {})
        with connect(self.uri, compression=None) as websocket:
            websocket.send("legacy text request")
            response = unpack_message(websocket.recv(timeout=5))
        self.assertEqual(response["message_type"], "inference_error")
        self.assertEqual(response["error_code"], ErrorCode.FRAME_TYPE_REJECTED.value)
        after = self.control("status", {})
        self.assertEqual(after["in_flight"], 0)
        self.assertEqual(after["generation"], before["generation"])

    def test_second_server_cannot_steal_or_unlink_control_socket(self) -> None:
        second_ready = self.runtime_root / "second-ready.json"
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        second = subprocess.run(
            [
                sys.executable,
                "-m",
                "oven_runtime.server.app",
                "--runtime-root",
                str(self.runtime_root),
                "--port",
                "0",
                "--ready-file",
                "second-ready.json",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            env=environment,
        )
        self.assertNotEqual(second.returncode, 0)
        self.assertFalse(second_ready.exists())
        self.assertTrue(self.socket_path.exists())
        status = self.control("status", {})
        self.assertEqual(status["state"], "STARTING")


if __name__ == "__main__":
    unittest.main()
