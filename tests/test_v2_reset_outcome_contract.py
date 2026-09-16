"""Offline validation for the versioned reset outcome wire contract."""

import pytest

from oven_runtime.v2.hitl_ros import parse_reset_outcome


def test_reset_outcome_parser_accepts_active_and_terminal_attempts() -> None:
    assert parse_reset_outcome('{"schema_version": 1, "attempt_id": 2, "outcome": "RESET_COMPLETE"}') == (
        2,
        'RESET_COMPLETE',
    )
    assert parse_reset_outcome('{"schema_version": 1, "attempt_id": 0, "outcome": "IDLE"}') == (
        0,
        'IDLE',
    )


@pytest.mark.parametrize(
    'payload',
    (
        'not-json',
        '{"schema_version": 2, "attempt_id": 1, "outcome": "RESET_COMPLETE"}',
        '{"schema_version": 1, "attempt_id": 0, "outcome": "RESET_INCOMPLETE"}',
        '{"schema_version": 1, "attempt_id": 1, "outcome": "unknown"}',
    ),
)
def test_reset_outcome_parser_fails_closed(payload: str) -> None:
    with pytest.raises(ValueError):
        parse_reset_outcome(payload)
