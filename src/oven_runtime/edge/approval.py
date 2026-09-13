from __future__ import annotations

from collections.abc import Callable


class ConsoleApprovalGate:
    def __init__(self, *, execute_enabled: bool, read: Callable[[str], str] = input) -> None:
        self.execute_enabled = execute_enabled
        self._read = read

    def approve_run(self, run_id: str, skills: tuple[str, ...]) -> bool:
        if not self.execute_enabled:
            return False
        plan = " -> ".join(skills)
        prompt = (
            f"Plan: {plan}\n"
            f"Run ID: {run_id}\n"
            "Press Enter to authorize every declared reset and rollout.\n"
            "Type anything else to cancel: "
        )
        try:
            answer = self._read(prompt)
        except (EOFError, OSError):
            return False
        return answer == ""
