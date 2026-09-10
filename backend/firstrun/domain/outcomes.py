"""Authoritative process outcomes and stable CLI exit codes."""

from __future__ import annotations

from enum import StrEnum


class Outcome(StrEnum):
    """Controller-owned outcomes; repository or model output cannot set these."""

    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    POLICY_BLOCKED = "policy_blocked"
    UNSUPPORTED = "unsupported"
    CLEANUP_FAILED = "cleanup_failed"


EXIT_CODES: dict[Outcome, int] = {
    Outcome.PASSED: 0,
    Outcome.FAILED: 10,
    Outcome.TIMED_OUT: 11,
    Outcome.INFRASTRUCTURE_ERROR: 12,
    Outcome.POLICY_BLOCKED: 13,
    Outcome.UNSUPPORTED: 14,
    Outcome.CLEANUP_FAILED: 15,
}


def exit_code_for(outcome: Outcome) -> int:
    """Return the stable CLI exit code for an authoritative outcome."""

    return EXIT_CODES[outcome]
