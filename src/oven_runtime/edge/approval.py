from __future__ import annotations

from collections.abc import Callable


class ConsoleApprovalGate:
    def __init__(self, *, execute_enabled: bool, read: Callable[[str], str] = input) -> None:
        self.execute_enabled = execute_enabled
        self._read = read

    def approve_run(self, run_id: str, skills: tuple[str, ...]) -> bool:
        if not self.execute_enabled:
            return False
        expected = f"YES RUN {run_id}"
        plan = " -> ".join(skills)
        answer = self._read(f"Plan: {plan}\nType '{expected}' to authorize every declared reset and rollout: ")
        return answer.strip() == expected
