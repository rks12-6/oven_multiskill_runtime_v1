from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from oven_runtime.edge import app
from oven_runtime.edge.approval import ConsoleApprovalGate


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "config" / "agilex_oven_v1.toml"


class RunIdTests(unittest.TestCase):
    def test_generated_run_ids_are_unique_skill_scoped_and_filesystem_safe(self) -> None:
        moment = datetime(2026, 9, 13, 18, 27, 31)
        first = app._resolve_run_id(None, skill="rotate_button", now=moment, token="a3f91c")
        second = app._resolve_run_id(None, skill="rotate_button", now=moment, token="bc41de")

        self.assertEqual(first, "rotate_button_20260913_182731_a3f91c")
        self.assertNotEqual(first, second)
        self.assertRegex(first, r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

    def test_explicit_run_id_is_preserved(self) -> None:
        self.assertEqual(
            app._resolve_run_id("fixed_test", skill="rotate_button"), "fixed_test"
        )

    def test_plan_construction_generates_a_new_run_id_when_unspecified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            values = []
            for token in ("a3f91c", "bc41de"):
                output = io.StringIO()
                with patch.object(app.secrets, "token_hex", return_value=token), redirect_stdout(output):
                    result = app.main(
                        [
                            "--profile",
                            str(PROFILE),
                            "--skill",
                            "rotate_button",
                            "--runtime-root",
                            directory,
                            "--plan",
                        ]
                    )
                self.assertEqual(result, 0)
                values.append(json.loads(output.getvalue())["run_id"])

        self.assertNotEqual(values[0], values[1])
        self.assertTrue(all(value.startswith("rotate_button_") for value in values))


class ApprovalTests(unittest.TestCase):
    def test_enter_approves_and_prompt_displays_plan_and_run_id(self) -> None:
        prompts: list[str] = []
        gate = ConsoleApprovalGate(execute_enabled=True, read=lambda prompt: prompts.append(prompt) or "")

        self.assertTrue(gate.approve_run("rotate_button_20260913_182731_a3f91c", ("rotate_button",)))
        self.assertEqual(
            prompts,
            [
                "Plan: rotate_button\n"
                "Run ID: rotate_button_20260913_182731_a3f91c\n"
                "Press Enter to authorize every declared reset and rollout.\n"
                "Type anything else to cancel: "
            ],
        )

    def test_nonempty_input_including_space_rejects(self) -> None:
        for answer in ("no", "q", "abc", " "):
            with self.subTest(answer=answer):
                gate = ConsoleApprovalGate(execute_enabled=True, read=lambda prompt, value=answer: value)
                self.assertFalse(gate.approve_run("fixed_test", ("rotate_button",)))

    def test_eof_fails_closed(self) -> None:
        def end_of_input(prompt: str) -> str:
            del prompt
            raise EOFError

        gate = ConsoleApprovalGate(execute_enabled=True, read=end_of_input)
        self.assertFalse(gate.approve_run("fixed_test", ("rotate_button",)))


if __name__ == "__main__":
    unittest.main()
