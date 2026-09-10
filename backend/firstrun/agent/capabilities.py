"""Case-bound, bounded capabilities exposed to the M2 repair agent.

The broker runs in the trusted controller.  The credentialed Strands child sends
typed requests over a private pipe; it never receives a host path, Docker endpoint,
or an unrestricted command primitive.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping

from firstrun.domain.evidence import AttemptEvidence, CommandEvidence, RunEvidence
from firstrun.verification.source import SourcePolicyError, SourceSnapshot
from firstrun.worker.docker import _load_unique_json


MAX_TOOL_RESPONSE_BYTES = 16_384
MAX_TOOL_REQUEST_BYTES = 8_192
MAX_READ_CHARS = 12_000
MAX_READ_LINES = 200
MAX_SEARCH_HITS = 30
MAX_TOOL_CALLS = 64
_CASE_TOKEN = re.compile(r"^[0-9a-f]{32}$")
_CALL_ID = re.compile(r"^[0-9a-f]{32}$")
_SCRIPT_NAME = re.compile(r"^[A-Za-z0-9:_-]{1,80}$")
_LITERAL_QUERY = re.compile(r"^[^\x00\r\n]{1,128}$")
_TOOL_NAMES = frozenset(
    {
        "read_source_file",
        "search_source",
        "inspect_pinned_diff",
        "read_baseline_evidence",
        "run_diagnostic",
    }
)


class CapabilityProtocolError(RuntimeError):
    """A child request did not satisfy the private capability protocol."""


@dataclass(frozen=True)
class CapabilityRecord:
    agent_attempt: int
    sequence: int
    call_id: str
    tool_name: str
    request_digest: str
    response_digest: str
    outcome: Literal["succeeded", "denied", "failed"]
    diagnostic_index: int | None
    elapsed_ms: int
    evidence_ref: str
    sanitized_summary: str
    sanitized_result: str
    output_truncated: bool


DiagnosticRunner = Callable[[str], CommandEvidence]


class RepairCapabilityBroker:
    """Dispatch narrow agent tools against one immutable case snapshot."""

    def __init__(
        self,
        *,
        case_token: str,
        snapshot: SourceSnapshot,
        baseline: RunEvidence | AttemptEvidence,
        diagnostic_runner: DiagnosticRunner,
        max_diagnostics: int,
        agent_attempt: int,
        prior_attempt_feedback: Mapping[str, object] | None = None,
        max_tool_calls: int = MAX_TOOL_CALLS,
    ) -> None:
        if _CASE_TOKEN.fullmatch(case_token) is None:
            raise ValueError("case_token must be a controller-generated hex identifier")
        if not 0 <= max_diagnostics <= 8:
            raise ValueError("max_diagnostics must be between zero and eight")
        if baseline.base_commit != snapshot.base_commit:
            raise ValueError("baseline evidence is not bound to the source snapshot")
        if agent_attempt not in {1, 2}:
            raise ValueError("agent_attempt must be one or two")
        if type(max_tool_calls) is not int or not 1 <= max_tool_calls <= MAX_TOOL_CALLS:
            raise ValueError("max_tool_calls must be between one and 64")
        self.case_token = case_token
        self.agent_attempt = agent_attempt
        self._snapshot = snapshot
        self._baseline = baseline
        self._diagnostic_runner = diagnostic_runner
        self._max_diagnostics = max_diagnostics
        self._max_tool_calls = max_tool_calls
        if prior_attempt_feedback is not None:
            encoded_feedback = _canonical_json(prior_attempt_feedback)
            if len(encoded_feedback) > 8_192:
                raise ValueError("prior attempt feedback exceeds its controller limit")
            self._prior_attempt_feedback = dict(prior_attempt_feedback)
        else:
            self._prior_attempt_feedback = None
        self._diagnostic_count = 0
        self._records: list[CapabilityRecord] = []
        self._call_ids: set[str] = set()
        self._evidence_refs: set[str] = set()
        self._package_scripts = _package_scripts(snapshot)

    @property
    def records(self) -> tuple[CapabilityRecord, ...]:
        return tuple(self._records)

    @property
    def evidence_refs(self) -> frozenset[str]:
        return frozenset(self._evidence_refs)

    @property
    def diagnostic_count(self) -> int:
        return self._diagnostic_count

    def dispatch(self, message: object) -> dict[str, object]:
        """Validate one private RPC request and return a bounded response."""

        if len(self._records) >= self._max_tool_calls:
            raise CapabilityProtocolError("tool-call budget is exhausted")
        if not isinstance(message, dict) or set(message) != {
            "kind",
            "case_token",
            "call_id",
            "tool",
            "arguments",
        }:
            raise CapabilityProtocolError("invalid capability request envelope")
        try:
            encoded_request = _canonical_json(message)
        except (TypeError, ValueError) as exc:
            raise CapabilityProtocolError("capability request is not JSON-safe") from exc
        if len(encoded_request) > MAX_TOOL_REQUEST_BYTES:
            raise CapabilityProtocolError("capability request exceeds its byte limit")
        if message.get("kind") != "tool_request":
            raise CapabilityProtocolError("invalid capability request kind")
        if message.get("case_token") != self.case_token:
            raise CapabilityProtocolError("capability request is not bound to this case")
        call_id = message.get("call_id")
        tool_name = message.get("tool")
        arguments = message.get("arguments")
        if not isinstance(call_id, str) or _CALL_ID.fullmatch(call_id) is None:
            raise CapabilityProtocolError("invalid capability call identifier")
        if call_id in self._call_ids:
            raise CapabilityProtocolError("capability call identifier was replayed")
        self._call_ids.add(call_id)
        if not isinstance(tool_name, str) or tool_name not in _TOOL_NAMES:
            raise CapabilityProtocolError("unsupported capability tool")
        if not isinstance(arguments, dict):
            raise CapabilityProtocolError("capability arguments must be an object")

        started = time.monotonic()
        request_digest = _digest_json({"tool": tool_name, "arguments": arguments})
        outcome: Literal["succeeded", "denied", "failed"] = "succeeded"
        diagnostics_before = self._diagnostic_count
        try:
            data = self._invoke(tool_name, arguments)
        except (CapabilityProtocolError, ValueError) as exc:
            outcome = "denied"
            data = {"error": _bounded_text(str(exc), 512)}
        except Exception:
            outcome = "failed"
            data = {"error": "trusted capability failed"}

        sequence = len(self._records) + 1
        response_digest = _digest_json(data)
        evidence_ref = (
            f"tool_a{self.agent_attempt}_{sequence:03d}_"
            f"{response_digest.split(':', 1)[1][:12]}"
        )
        response: dict[str, object] = {
            "kind": "tool_response",
            "call_id": call_id,
            "outcome": outcome,
            "evidence_ref": evidence_ref,
            "data": data,
        }
        output_truncated = bool(
            isinstance(data, dict) and data.get("truncated") is True
        )
        encoded = _canonical_json(response)
        if len(encoded) > MAX_TOOL_RESPONSE_BYTES:
            outcome = "failed"
            data = {"error": "tool response exceeded its controller limit"}
            output_truncated = True
            response_digest = _digest_json(data)
            evidence_ref = (
                f"tool_a{self.agent_attempt}_{sequence:03d}_"
                f"{response_digest.split(':', 1)[1][:12]}"
            )
            response = {
                "kind": "tool_response",
                "call_id": call_id,
                "outcome": outcome,
                "evidence_ref": evidence_ref,
                "data": data,
            }
        elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
        diagnostic_index = (
            self._diagnostic_count
            if tool_name == "run_diagnostic"
            and self._diagnostic_count > diagnostics_before
            else None
        )
        summary = _summary(tool_name, arguments, outcome)
        sanitized_result = _canonical_json(data).decode("ascii")
        self._records.append(
            CapabilityRecord(
                agent_attempt=self.agent_attempt,
                sequence=sequence,
                call_id=call_id,
                tool_name=tool_name,
                request_digest=request_digest,
                response_digest=response_digest,
                outcome=outcome,
                diagnostic_index=diagnostic_index,
                elapsed_ms=min(elapsed_ms, 600_000),
                evidence_ref=evidence_ref,
                sanitized_summary=summary,
                sanitized_result=sanitized_result,
                output_truncated=output_truncated,
            )
        )
        self._evidence_refs.add(evidence_ref)
        return response

    def _invoke(self, tool_name: str, arguments: Mapping[str, object]) -> object:
        if tool_name == "inspect_pinned_diff":
            _require_keys(arguments, set())
            return {
                "base_commit": self._snapshot.base_commit,
                "source_tree": self._snapshot.source_tree,
                "paths": sorted(self._snapshot.files),
                "working_tree_used": False,
            }
        if tool_name == "read_source_file":
            _require_keys(arguments, {"path", "start_line", "max_lines"})
            path = _required_text(arguments.get("path"), "path", 256)
            start_line = _required_int(arguments.get("start_line"), "start_line", 1, 100_000)
            max_lines = _required_int(arguments.get("max_lines"), "max_lines", 1, MAX_READ_LINES)
            content = self._source_text(path)
            lines = content.splitlines()
            selected = "\n".join(lines[start_line - 1 : start_line - 1 + max_lines])
            return {
                "path": path,
                "start_line": start_line,
                "text": _bounded_text(selected, MAX_READ_CHARS),
                "truncated": len(selected) > MAX_READ_CHARS,
            }
        if tool_name == "search_source":
            _require_keys(arguments, {"query"})
            query = _required_text(arguments.get("query"), "query", 128)
            if _LITERAL_QUERY.fullmatch(query) is None:
                raise CapabilityProtocolError("query must be one bounded literal line")
            hits: list[dict[str, object]] = []
            folded = query.casefold()
            for path in sorted(self._snapshot.files):
                for number, line in enumerate(self._source_text(path).splitlines(), 1):
                    if folded in line.casefold():
                        hits.append(
                            {
                                "path": path,
                                "line": number,
                                "text": _bounded_text(line, 320),
                            }
                        )
                        if len(hits) == MAX_SEARCH_HITS:
                            return {"query": query, "hits": hits, "truncated": True}
            return {"query": query, "hits": hits, "truncated": False}
        if tool_name == "read_baseline_evidence":
            _require_keys(arguments, set())
            projection = _baseline_projection(self._baseline)
            if self._prior_attempt_feedback is not None:
                projection["prior_attempt_feedback"] = self._prior_attempt_feedback
            return projection
        if tool_name == "run_diagnostic":
            _require_keys(arguments, {"script_name"})
            script_name = _required_text(arguments.get("script_name"), "script_name", 80)
            if _SCRIPT_NAME.fullmatch(script_name) is None:
                raise CapabilityProtocolError("script name is not supported")
            if script_name not in self._package_scripts:
                raise CapabilityProtocolError("script is not declared by immutable package.json")
            if self._diagnostic_count >= self._max_diagnostics:
                raise CapabilityProtocolError("diagnostic command budget is exhausted")
            self._diagnostic_count += 1
            return _command_projection(self._diagnostic_runner(script_name))
        raise CapabilityProtocolError("unsupported capability tool")

    def _source_text(self, path: str) -> str:
        if path not in self._snapshot.files:
            raise CapabilityProtocolError("source path is not in the pinned case snapshot")
        try:
            return self._snapshot.files[path].decode("utf-8", errors="strict")
        except UnicodeError as exc:  # defensive: source capture already enforces UTF-8
            raise CapabilityProtocolError("source file is not readable text") from exc


def _package_scripts(snapshot: SourceSnapshot) -> frozenset[str]:
    try:
        package = _load_unique_json(snapshot.content("package.json"))
    except (SourcePolicyError, UnicodeError, ValueError) as exc:
        raise ValueError("pinned package.json is invalid") from exc
    scripts = package.get("scripts") if isinstance(package, dict) else None
    if not isinstance(scripts, dict) or any(
        not isinstance(name, str) or not isinstance(value, str)
        for name, value in scripts.items()
    ):
        raise ValueError("pinned package.json scripts are invalid")
    return frozenset(scripts)


def _baseline_projection(
    evidence: RunEvidence | AttemptEvidence,
) -> dict[str, object]:
    projection: dict[str, object] = {
        "phase": evidence.phase,
        "base_commit": evidence.base_commit,
        "outcome": evidence.outcome.value,
        "commands": [_command_projection(command) for command in evidence.commands],
        "errors": list(evidence.sanitized_errors),
    }
    projection["readiness"] = (
        {
            "outcome": evidence.readiness.outcome.value,
            "expected_status": evidence.readiness.expected_status,
            "observed_status": evidence.readiness.observed_status,
            "error": evidence.readiness.sanitized_error,
        }
        if evidence.readiness is not None
        else None
    )
    projection["acceptance"] = (
        {
            "verifier_id": evidence.acceptance_probe.verifier_id,
            "outcome": evidence.acceptance_probe.outcome.value,
            "required_checks": list(evidence.acceptance_probe.required_checks),
            "passed_checks": list(evidence.acceptance_probe.passed_checks),
            "error": evidence.acceptance_probe.sanitized_error,
        }
        if evidence.acceptance_probe is not None
        else None
    )
    return projection


def _command_projection(command: CommandEvidence) -> dict[str, object]:
    return {
        "step_id": command.step_id,
        "kind": command.kind,
        "argv": list(command.argv),
        "cwd": command.cwd,
        "outcome": command.outcome.value,
        "exit_code": command.exit_code,
        "stdout_tail": command.sanitized_stdout_tail,
        "stderr_tail": command.sanitized_stderr_tail,
    }


def _require_keys(arguments: Mapping[str, object], expected: set[str]) -> None:
    if set(arguments) != expected:
        raise CapabilityProtocolError("tool arguments do not match the exact schema")


def _required_text(value: object, name: str, max_length: int) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise CapabilityProtocolError(f"{name} must be bounded text")
    if any(character in value for character in "\x00\r\n"):
        raise CapabilityProtocolError(f"{name} contains unsupported characters")
    return value


def _required_int(value: object, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise CapabilityProtocolError(f"{name} is outside its controller limit")
    return value


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest_json(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value)).hexdigest()


def _bounded_text(value: str, limit: int) -> str:
    cleaned = value.replace("\x00", "?").replace("\r", " ").strip()
    return cleaned[:limit]


def _summary(
    tool_name: str,
    arguments: Mapping[str, object],
    outcome: str,
) -> str:
    subject = ""
    for key in ("path", "query", "script_name"):
        value = arguments.get(key)
        if isinstance(value, str):
            subject = ":" + _bounded_text(value, 128)
            break
    return _bounded_text(f"{tool_name}{subject}:{outcome}", 256)


__all__ = [
    "CapabilityProtocolError",
    "CapabilityRecord",
    "RepairCapabilityBroker",
]
