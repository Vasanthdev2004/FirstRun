"""Read-only environment diagnostics for the supported local FirstRun lane.

The doctor intentionally limits itself to version and status queries.  It does not
start containers, install software, inspect provider credentials, or execute any
repository-owned command.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

from firstrun.domain import Outcome, exit_code_for


CheckRecord = dict[str, object]
DoctorResult = dict[str, object]

_COMMAND_TIMEOUT_SECONDS = 5.0
_SUMMARY_LIMIT = 240
_NODE_MINIMUM = (22, 16, 0)
_OUTCOME_PRECEDENCE = (
    Outcome.UNSUPPORTED,
    Outcome.INFRASTRUCTURE_ERROR,
    Outcome.POLICY_BLOCKED,
)
_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Bounded result from one controller-owned command invocation."""

    argv: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    error: str | None = None


class CommandRunner(Protocol):
    """Small injectable seam used by the doctor and its unit tests."""

    def run(
        self, argv: Sequence[str], *, timeout_seconds: float
    ) -> CommandResult: ...


class SubprocessRunner:
    """Run explicit argv without a shell and retain only bounded output."""

    def __init__(
        self,
        *,
        max_output_chars: int = 4096,
        forbidden_roots: Sequence[str | Path] | None = None,
    ) -> None:
        if max_output_chars < 32:
            raise ValueError("max_output_chars must be at least 32")
        self._max_output_chars = max_output_chars
        roots = forbidden_roots if forbidden_roots is not None else (Path.cwd(),)
        self._forbidden_roots = tuple(Path(root).resolve() for root in roots)

    def run(
        self, argv: Sequence[str], *, timeout_seconds: float
    ) -> CommandResult:
        if isinstance(argv, (str, bytes)) or not argv:
            raise ValueError("argv must be a non-empty sequence of strings")
        if any(not isinstance(part, str) or not part for part in argv):
            raise ValueError("argv entries must be non-empty strings")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        explicit_argv = tuple(argv)
        resolved_program = shutil.which(explicit_argv[0])
        if resolved_program is None:
            return CommandResult(
                argv=explicit_argv,
                returncode=None,
                stdout="",
                stderr="",
                error=f"trusted executable not found: {explicit_argv[0]}",
            )
        program_path = Path(resolved_program).resolve()
        if any(_is_within(program_path, root) for root in self._forbidden_roots):
            return CommandResult(
                argv=explicit_argv,
                returncode=None,
                stdout="",
                stderr="",
                error=f"refused executable beneath an untrusted root: {program_path}",
            )
        execution_argv = (str(program_path), *explicit_argv[1:])
        try:
            completed = subprocess.run(
                execution_argv,
                cwd=str(program_path.parent),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                argv=execution_argv,
                returncode=None,
                stdout=self._clip(exc.stdout),
                stderr=self._clip(exc.stderr),
                timed_out=True,
                error=f"timed out after {timeout_seconds:g} seconds",
            )
        except OSError as exc:
            return CommandResult(
                argv=execution_argv,
                returncode=None,
                stdout="",
                stderr="",
                error=self._clip(str(exc)),
            )

        return CommandResult(
            argv=execution_argv,
            returncode=completed.returncode,
            stdout=self._clip(completed.stdout),
            stderr=self._clip(completed.stderr),
        )

    def _clip(self, value: str | bytes | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if len(value) <= self._max_output_chars:
            return value
        marker = "\n...[truncated]"
        return value[: self._max_output_chars - len(marker)] + marker


def run_doctor(
    repo_path: str | Path = ".",
    *,
    runner: CommandRunner | None = None,
    python_version: tuple[int, int, int] | None = None,
) -> DoctorResult:
    """Inspect local M0 prerequisites without changing local or remote state."""

    resolved_repo = Path(repo_path).resolve()
    command_runner = runner or SubprocessRunner(
        forbidden_roots=(Path.cwd().resolve(), resolved_repo)
    )
    current_python = python_version or (
        sys.version_info.major,
        sys.version_info.minor,
        sys.version_info.micro,
    )

    checks: dict[str, CheckRecord] = {}
    failures: list[Outcome] = []

    def record(
        check_id: str,
        value: CheckRecord,
        failure_outcome: Outcome | None = None,
    ) -> None:
        checks[check_id] = value
        if value["status"] == "failed" and failure_outcome is not None:
            failures.append(failure_outcome)

    python_text = ".".join(str(part) for part in current_python)
    if (3, 12, 0) <= current_python < (3, 13, 0):
        record(
            "python",
            _passed(
                f"Python {python_text} is supported",
                version=python_text,
                required=">=3.12,<3.13",
            ),
        )
    else:
        record(
            "python",
            _failed(
                f"Python {python_text} is outside the supported range",
                version=python_text,
                required=">=3.12,<3.13",
            ),
            Outcome.UNSUPPORTED,
        )

    git_cli_result = command_runner.run(
        ("git", "--version"), timeout_seconds=_COMMAND_TIMEOUT_SECONDS
    )
    if _command_succeeded(git_cli_result):
        record(
            "git_cli",
            _passed(
                "Git CLI is available",
                version=_single_line(git_cli_result.stdout),
            ),
        )
        _record_git_repository(
            checks,
            failures,
            command_runner,
            resolved_repo,
        )
    else:
        record(
            "git_cli",
            _command_failed("Git CLI is unavailable", git_cli_result),
            Outcome.INFRASTRUCTURE_ERROR,
        )
        record(
            "git_repository",
            _unknown("Repository status was not checked because Git is unavailable"),
        )

    _record_simple_command(
        checks,
        failures,
        command_runner,
        check_id="uv",
        argv=("uv", "--version"),
        available_message="uv is available",
        unavailable_message="uv is unavailable",
    )

    node_result = command_runner.run(
        ("node", "--version"), timeout_seconds=_COMMAND_TIMEOUT_SECONDS
    )
    if not _command_succeeded(node_result):
        record(
            "node",
            _command_failed("Node.js is unavailable", node_result),
            Outcome.INFRASTRUCTURE_ERROR,
        )
    else:
        node_text = _single_line(node_result.stdout)
        parsed_node = _parse_semver(node_text)
        if parsed_node is None:
            record(
                "node",
                _failed(
                    "Node.js returned an unrecognized version",
                    version=node_text,
                    required=">=22.16",
                ),
                Outcome.UNSUPPORTED,
            )
        elif parsed_node < _NODE_MINIMUM:
            record(
                "node",
                _failed(
                    f"Node.js {node_text} is below the supported minimum",
                    version=node_text,
                    required=">=22.16",
                ),
                Outcome.UNSUPPORTED,
            )
        else:
            record(
                "node",
                _passed(
                    f"Node.js {node_text} is supported",
                    version=node_text,
                    required=">=22.16",
                ),
            )

    _record_simple_command(
        checks,
        failures,
        command_runner,
        check_id="npm",
        argv=("npm", "--version"),
        available_message="npm is available",
        unavailable_message="npm is unavailable",
    )

    docker_client_result = command_runner.run(
        ("docker", "--version"), timeout_seconds=_COMMAND_TIMEOUT_SECONDS
    )
    if _command_succeeded(docker_client_result):
        record(
            "docker_client",
            _passed(
                "Docker client is available",
                version=_single_line(docker_client_result.stdout),
            ),
        )
        docker_server_ready = _record_docker_server(
            checks, failures, command_runner
        )
        if docker_server_ready:
            _record_docker_engine(checks, failures, command_runner)
        else:
            record(
                "docker_engine",
                _unknown(
                    "Docker engine type was not checked because the server is unavailable"
                ),
            )
    else:
        record(
            "docker_client",
            _command_failed("Docker client is unavailable", docker_client_result),
            Outcome.INFRASTRUCTURE_ERROR,
        )
        record(
            "docker_server",
            _unknown("Docker server was not checked because the client is unavailable"),
        )
        record(
            "docker_engine",
            _unknown(
                "Docker engine type was not checked because the client is unavailable"
            ),
        )

    record(
        "provider",
        _unknown(
            "Provider readiness was not checked; doctor does not access credentials",
            required=False,
            check="not_checked",
            credentials_accessed=False,
        ),
    )

    outcome = _overall_outcome(failures)
    return {
        "schema_version": 1,
        "repo_path": str(resolved_repo),
        "outcome": outcome.value,
        "exit_code": exit_code_for(outcome),
        "checks": checks,
    }


def _record_git_repository(
    checks: dict[str, CheckRecord],
    failures: list[Outcome],
    runner: CommandRunner,
    repo_path: Path,
) -> None:
    inside_result = runner.run(
        (
            "git",
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(repo_path),
            "rev-parse",
            "--is-inside-work-tree",
        ),
        timeout_seconds=_COMMAND_TIMEOUT_SECONDS,
    )
    if not _command_succeeded(inside_result) or (
        _single_line(inside_result.stdout).lower() != "true"
    ):
        checks["git_repository"] = _command_failed(
            "Path is not a readable Git working tree", inside_result
        )
        failures.append(Outcome.POLICY_BLOCKED)
        return

    status_result = runner.run(
        (
            "git",
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(repo_path),
            "status",
            "--porcelain=v1",
            "--untracked-files=normal",
        ),
        timeout_seconds=_COMMAND_TIMEOUT_SECONDS,
    )
    if not _command_completed(status_result):
        checks["git_repository"] = _command_failed(
            "Git repository status could not be read", status_result
        )
        failures.append(Outcome.POLICY_BLOCKED)
        return

    changed_entries = [line for line in status_result.stdout.splitlines() if line]
    if changed_entries:
        checks["git_repository"] = _failed(
            "Git working tree is not clean",
            clean=False,
            changed_entry_count=len(changed_entries),
        )
        failures.append(Outcome.POLICY_BLOCKED)
        return

    checks["git_repository"] = _passed(
        "Git working tree is clean", clean=True, changed_entry_count=0
    )


def _record_simple_command(
    checks: dict[str, CheckRecord],
    failures: list[Outcome],
    runner: CommandRunner,
    *,
    check_id: str,
    argv: tuple[str, ...],
    available_message: str,
    unavailable_message: str,
) -> None:
    result = runner.run(argv, timeout_seconds=_COMMAND_TIMEOUT_SECONDS)
    if _command_succeeded(result):
        checks[check_id] = _passed(
            available_message, version=_single_line(result.stdout)
        )
        return
    checks[check_id] = _command_failed(unavailable_message, result)
    failures.append(Outcome.INFRASTRUCTURE_ERROR)


def _record_docker_server(
    checks: dict[str, CheckRecord],
    failures: list[Outcome],
    runner: CommandRunner,
) -> bool:
    result = runner.run(
        ("docker", "version", "--format", "{{json .Server}}"),
        timeout_seconds=_COMMAND_TIMEOUT_SECONDS,
    )
    if not _command_succeeded(result):
        checks["docker_server"] = _command_failed(
            "Docker server is unavailable", result
        )
        failures.append(Outcome.INFRASTRUCTURE_ERROR)
        return False

    try:
        server = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        server = None
    if not isinstance(server, dict) or not server:
        checks["docker_server"] = _failed(
            "Docker server returned invalid version information"
        )
        failures.append(Outcome.INFRASTRUCTURE_ERROR)
        return False

    checks["docker_server"] = _passed(
        "Docker server is reachable",
        version=str(server.get("Version", "unknown")),
        api_version=str(server.get("ApiVersion", "unknown")),
    )
    return True


def _record_docker_engine(
    checks: dict[str, CheckRecord],
    failures: list[Outcome],
    runner: CommandRunner,
) -> None:
    result = runner.run(
        ("docker", "info", "--format", "{{.OSType}}"),
        timeout_seconds=_COMMAND_TIMEOUT_SECONDS,
    )
    if not _command_succeeded(result):
        checks["docker_engine"] = _command_failed(
            "Docker engine type could not be determined", result
        )
        failures.append(Outcome.INFRASTRUCTURE_ERROR)
        return

    engine_type = _single_line(result.stdout).lower()
    if engine_type != "linux":
        checks["docker_engine"] = _failed(
            "Docker is not using the supported Linux engine",
            engine_type=engine_type or "unknown",
            required="linux",
        )
        failures.append(Outcome.UNSUPPORTED)
        return

    checks["docker_engine"] = _passed(
        "Docker is using the Linux engine", engine_type="linux"
    )


def _overall_outcome(failures: Sequence[Outcome]) -> Outcome:
    for candidate in _OUTCOME_PRECEDENCE:
        if candidate in failures:
            return candidate
    return Outcome.PASSED


def _parse_semver(value: str) -> tuple[int, int, int] | None:
    match = _SEMVER_RE.fullmatch(value.strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _command_succeeded(result: CommandResult) -> bool:
    return _command_completed(result) and bool(result.stdout.strip())


def _command_completed(result: CommandResult) -> bool:
    return (
        result.returncode == 0
        and not result.timed_out
        and result.error is None
    )


def _command_failed(message: str, result: CommandResult) -> CheckRecord:
    details: dict[str, object] = {
        "return_code": result.returncode,
        "timed_out": result.timed_out,
    }
    diagnostic = result.error or result.stderr.strip()
    if diagnostic:
        details["diagnostic"] = _single_line(diagnostic)
    return _failed(message, **details)


def _passed(message: str, **details: object) -> CheckRecord:
    return _check("passed", message, True, details)


def _failed(message: str, **details: object) -> CheckRecord:
    return _check("failed", message, True, details)


def _unknown(
    message: str, *, required: bool = True, **details: object
) -> CheckRecord:
    return _check("unknown", message, required, details)


def _check(
    status: str, message: str, required: bool, details: dict[str, object]
) -> CheckRecord:
    return {
        "status": status,
        "required": required,
        "message": message,
        "details": details,
    }


def _single_line(value: str) -> str:
    compact = " ".join(value.strip().split())
    if len(compact) <= _SUMMARY_LIMIT:
        return compact
    return compact[: _SUMMARY_LIMIT - 3] + "..."


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
