from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from firstrun.doctor import CommandResult, SubprocessRunner, run_doctor


class FakeRunner:
    def __init__(
        self, responses: dict[tuple[str, ...], CommandResult]
    ) -> None:
        self.responses = responses
        self.calls: list[tuple[tuple[str, ...], float]] = []

    def run(
        self, argv: tuple[str, ...], *, timeout_seconds: float
    ) -> CommandResult:
        explicit_argv = tuple(argv)
        self.calls.append((explicit_argv, timeout_seconds))
        try:
            return self.responses[explicit_argv]
        except KeyError as exc:
            raise AssertionError(f"unexpected command: {explicit_argv!r}") from exc


def command(
    argv: tuple[str, ...],
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int | None = 0,
    timed_out: bool = False,
    error: str | None = None,
) -> CommandResult:
    return CommandResult(
        argv=argv,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        error=error,
    )


def healthy_responses(repo: Path) -> dict[tuple[str, ...], CommandResult]:
    resolved_repo = str(repo.resolve())
    values = {
        ("git", "--version"): "git version 2.54.0",
        (
            "git",
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            "-C",
            resolved_repo,
            "rev-parse",
            "--is-inside-work-tree",
        ): "true\n",
        (
            "git",
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            "-C",
            resolved_repo,
            "status",
            "--porcelain=v1",
            "--untracked-files=normal",
        ): "",
        ("uv", "--version"): "uv 0.12.9",
        ("node", "--version"): "v22.16.0",
        ("npm", "--version"): "11.0.0",
        ("docker", "--version"): "Docker version 29.7.2, build 123",
        (
            "docker",
            "version",
            "--format",
            "{{json .Server}}",
        ): '{"Version":"29.7.2","ApiVersion":"1.55"}',
        ("docker", "info", "--format", "{{.OSType}}"): "linux\n",
    }
    return {argv: command(argv, stdout=stdout) for argv, stdout in values.items()}


class DoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Path("test-repository")

    def check(self, result: dict[str, object], name: str) -> dict[str, object]:
        checks = result["checks"]
        self.assertIsInstance(checks, dict)
        return checks[name]  # type: ignore[index,return-value]

    def test_healthy_environment_passes_and_provider_is_not_checked(self) -> None:
        runner = FakeRunner(healthy_responses(self.repo))

        result = run_doctor(
            self.repo, runner=runner, python_version=(3, 12, 13)
        )

        self.assertEqual(result["outcome"], "passed")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(self.check(result, "provider")["status"], "unknown")
        self.assertEqual(
            self.check(result, "provider")["details"],
            {"check": "not_checked", "credentials_accessed": False},
        )
        self.assertNotIn("aws", [call[0][0] for call in runner.calls])
        self.assertNotIn("strands", [call[0][0] for call in runner.calls])
        json.dumps(result)

    @patch("firstrun.doctor.SubprocessRunner")
    def test_default_runner_rejects_tools_from_cwd_and_selected_repo(self, factory) -> None:
        factory.return_value = FakeRunner(healthy_responses(self.repo))

        result = run_doctor(self.repo, python_version=(3, 12, 13))

        self.assertEqual("passed", result["outcome"])
        roots = factory.call_args.kwargs["forbidden_roots"]
        self.assertEqual((Path.cwd().resolve(), self.repo.resolve()), roots)

    def test_python_outside_312_is_unsupported(self) -> None:
        runner = FakeRunner(healthy_responses(self.repo))

        result = run_doctor(
            self.repo, runner=runner, python_version=(3, 13, 0)
        )

        self.assertEqual(self.check(result, "python")["status"], "failed")
        self.assertEqual(result["outcome"], "unsupported")
        self.assertEqual(result["exit_code"], 14)

    def test_old_node_is_unsupported(self) -> None:
        responses = healthy_responses(self.repo)
        responses[("node", "--version")] = command(
            ("node", "--version"), stdout="v22.15.9"
        )

        result = run_doctor(
            self.repo,
            runner=FakeRunner(responses),
            python_version=(3, 12, 13),
        )

        self.assertEqual(self.check(result, "node")["status"], "failed")
        self.assertEqual(result["outcome"], "unsupported")
        self.assertEqual(result["exit_code"], 14)

    def test_unreachable_docker_server_is_infrastructure_error(self) -> None:
        responses = healthy_responses(self.repo)
        server_argv = (
            "docker",
            "version",
            "--format",
            "{{json .Server}}",
        )
        responses[server_argv] = command(
            server_argv,
            returncode=1,
            stderr="daemon is not running",
        )
        runner = FakeRunner(responses)

        result = run_doctor(
            self.repo, runner=runner, python_version=(3, 12, 13)
        )

        self.assertEqual(self.check(result, "docker_server")["status"], "failed")
        self.assertEqual(self.check(result, "docker_engine")["status"], "unknown")
        self.assertEqual(result["outcome"], "infrastructure_error")
        self.assertEqual(result["exit_code"], 12)
        self.assertNotIn(
            ("docker", "info", "--format", "{{.OSType}}"),
            [call[0] for call in runner.calls],
        )

    def test_non_linux_docker_engine_is_unsupported(self) -> None:
        responses = healthy_responses(self.repo)
        engine_argv = ("docker", "info", "--format", "{{.OSType}}")
        responses[engine_argv] = command(engine_argv, stdout="windows")

        result = run_doctor(
            self.repo,
            runner=FakeRunner(responses),
            python_version=(3, 12, 13),
        )

        self.assertEqual(self.check(result, "docker_engine")["status"], "failed")
        self.assertEqual(result["outcome"], "unsupported")
        self.assertEqual(result["exit_code"], 14)

    def test_dirty_git_repository_is_policy_blocked(self) -> None:
        responses = healthy_responses(self.repo)
        status_argv = (
            "git",
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(self.repo.resolve()),
            "status",
            "--porcelain=v1",
            "--untracked-files=normal",
        )
        responses[status_argv] = command(
            status_argv, stdout=" M backend/firstrun/doctor.py\n"
        )

        result = run_doctor(
            self.repo,
            runner=FakeRunner(responses),
            python_version=(3, 12, 13),
        )

        repository = self.check(result, "git_repository")
        self.assertEqual(repository["status"], "failed")
        self.assertEqual(repository["details"], {"clean": False, "changed_entry_count": 1})
        self.assertEqual(result["outcome"], "policy_blocked")
        self.assertEqual(result["exit_code"], 13)

    @patch("firstrun.doctor.shutil.which", return_value="C:/tools/tool.exe")
    @patch("firstrun.doctor.subprocess.run")
    def test_subprocess_runner_resolves_explicit_argv_and_bounds_output(
        self, run, which
    ) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=("tool", "--version"),
            returncode=0,
            stdout="x" * 200,
            stderr="",
        )
        runner = SubprocessRunner(max_output_chars=64)

        result = runner.run(("tool", "--version"), timeout_seconds=2.5)

        self.assertLessEqual(len(result.stdout), 64)
        self.assertTrue(result.stdout.endswith("...[truncated]"))
        called_args, called_kwargs = run.call_args
        self.assertEqual(
            called_args[0],
            (str(Path("C:/tools/tool.exe").resolve()), "--version"),
        )
        self.assertIs(called_kwargs["shell"], False)
        self.assertEqual(called_kwargs["timeout"], 2.5)
        self.assertEqual(
            called_kwargs["cwd"], str(Path("C:/tools/tool.exe").resolve().parent)
        )
        which.assert_called_once_with("tool")

    @patch("firstrun.doctor.subprocess.run")
    def test_subprocess_runner_reports_timeout_with_bounded_output(self, run) -> None:
        run.side_effect = subprocess.TimeoutExpired(
            cmd=("slow-tool",), timeout=1, output=b"y" * 200
        )
        runner = SubprocessRunner(max_output_chars=64)

        with patch(
            "firstrun.doctor.shutil.which", return_value="C:/tools/slow-tool.exe"
        ):
            result = runner.run(("slow-tool",), timeout_seconds=1)

        self.assertTrue(result.timed_out)
        self.assertIsNone(result.returncode)
        self.assertLessEqual(len(result.stdout), 64)

    @patch("firstrun.doctor.subprocess.run")
    def test_subprocess_runner_refuses_repo_local_executable(self, run) -> None:
        repo = Path("test-repository").resolve()
        local_tool = repo / "docker.exe"
        runner = SubprocessRunner(forbidden_roots=(repo,))

        with patch("firstrun.doctor.shutil.which", return_value=str(local_tool)):
            result = runner.run(("docker", "--version"), timeout_seconds=1)

        self.assertIsNone(result.returncode)
        self.assertIn("untrusted root", result.error or "")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
