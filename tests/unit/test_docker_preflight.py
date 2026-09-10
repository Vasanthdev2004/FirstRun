from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch

from firstrun.domain.outcomes import Outcome
from firstrun.preflight.docker import (
    CommandResult,
    DockerPreflightConfig,
    ROLE_LABEL,
    RUN_LABEL,
    SubprocessDockerCliRunner,
    _NETWORK_GUARD,
    run_docker_preflight,
)


APP_ID = "a" * 64
VERIFIER_ID = "b" * 64
IMAGE_ID = "sha256:" + "c" * 64
APP_DIGEST = "example/app@sha256:" + "d" * 64
VERIFIER_DIGEST = "example/verifier@sha256:" + "e" * 64


class FakeDockerRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.raw_calls: list[tuple[str, ...]] = []
        self.containers: dict[str, dict[str, object]] = {}
        self.image_digests = {
            "example/app:22": [APP_DIGEST],
            "example/verifier:22": [VERIFIER_DIGEST],
        }
        self.fail_image_inspect = False
        self.endpoint = "npipe:////./pipe/docker_engine"
        self.daemon_os = "linux"
        self.security_options = ["name=seccomp,profile=builtin"]
        self.verifier_exit = 0
        self.lose_create_response_for_role: str | None = None
        self.invalidate_create_response_for_role: str | None = None
        self.mutate_app_label_during_cleanup = False
        self.tamper_app_memory_limit = False
        self.inspect_counts: dict[str, int] = {}

    def run(self, argv: Sequence[str], *, timeout_seconds: float) -> CommandResult:
        self.assert_argv(argv, timeout_seconds)
        raw_command = tuple(argv)
        self.raw_calls.append(raw_command)
        command = (
            (raw_command[0], *raw_command[3:])
            if len(raw_command) >= 3 and raw_command[1] == "--host"
            else raw_command
        )
        self.calls.append(command)

        if command[1:3] == ("context", "show"):
            return CommandResult(0, "default\n")

        if command[1:3] == ("context", "inspect"):
            return CommandResult(0, json.dumps(self.endpoint))

        if command[1:3] == ("info", "--format"):
            return CommandResult(
                0,
                json.dumps(
                    {
                        "OSType": self.daemon_os,
                        "SecurityOptions": self.security_options,
                        "ServerVersion": "29.7.2",
                    }
                ),
            )

        if command[1:3] == ("image", "inspect"):
            if self.fail_image_inspect:
                return CommandResult(1, stderr="daemon unavailable")
            reference = command[-1]
            value = {
                "Id": IMAGE_ID,
                "RepoDigests": self.image_digests.get(reference, []),
                "Os": "linux",
                "Architecture": "amd64",
            }
            return CommandResult(0, json.dumps(value))

        if command[1:3] == ("image", "pull"):
            return CommandResult(0, "pulled")

        if command[1:3] == ("container", "ls"):
            label_filter = command[-1]
            run_id = label_filter.rsplit("=", 1)[-1]
            matching = [
                container_id
                for container_id, inspection in self.containers.items()
                if inspection["Config"]["Labels"].get(RUN_LABEL) == run_id  # type: ignore[index]
            ]
            return CommandResult(0, "\n".join(matching))

        if command[1:3] == ("container", "create"):
            role = self.option(command, "--label", occurrence=1).split("=", 1)[1]
            run_id = self.option(command, "--label", occurrence=0).split("=", 1)[1]
            container_id = APP_ID if role == "app" else VERIFIER_ID
            self.containers[container_id] = self.inspection(command, run_id, role)
            if role == self.lose_create_response_for_role:
                return CommandResult(124, stderr="Docker CLI timed out")
            if role == self.invalidate_create_response_for_role:
                return CommandResult(0, "not-a-container-id\n")
            return CommandResult(0, container_id + "\n")

        if command[1:3] == ("container", "inspect"):
            identifier = command[-1]
            container_id = next(
                (
                    candidate_id
                    for candidate_id, inspection in self.containers.items()
                    if identifier == candidate_id
                    or inspection.get("Name") in (identifier, f"/{identifier}")
                ),
                None,
            )
            if container_id is None:
                return CommandResult(1, stderr="No such container")
            self.inspect_counts[container_id] = self.inspect_counts.get(container_id, 0) + 1
            inspection = self.containers[container_id]
            if container_id == APP_ID and self.tamper_app_memory_limit:
                inspection["HostConfig"]["Memory"] = 1  # type: ignore[index]
            if (
                container_id == APP_ID
                and self.mutate_app_label_during_cleanup
                and self.inspect_counts[container_id] > 1
            ):
                inspection["Config"]["Labels"][RUN_LABEL] = "someone-else"  # type: ignore[index]
            return CommandResult(0, json.dumps(inspection))

        if command[1:3] == ("container", "start"):
            return CommandResult(0, command[3])

        if command[1:3] == ("container", "wait"):
            return CommandResult(0, f"{self.verifier_exit}\n")

        if command[1:3] == ("container", "logs"):
            return CommandResult(
                0,
                json.dumps({
                    "health": True,
                    "onlyLoopbackInterfaces": True,
                    "noNonLoopbackRoutes": True,
                    "egressBlocked": True,
                }) + "\n",
                "verifier failed" if self.verifier_exit else "",
            )

        if command[1:3] == ("container", "rm"):
            self.containers.pop(command[-1], None)
            return CommandResult(0, command[-1])

        raise AssertionError(f"unexpected command: {command!r}")

    def inspection(self, command: tuple[str, ...], run_id: str, role: str) -> dict[str, object]:
        image = command[command.index("-e") - 1]
        inline_program = command[command.index("-e") + 1]
        container_id = APP_ID if role == "app" else VERIFIER_ID
        return {
            "Id": container_id,
            "Name": f"/{self.option(command, '--name')}",
            "Image": IMAGE_ID,
            "Config": {
                "User": self.option(command, "--user"),
                "Labels": {RUN_LABEL: run_id, ROLE_LABEL: role},
                "Image": image,
                "Entrypoint": [self.option(command, "--entrypoint")],
                "Cmd": ["-e", inline_program],
            },
            "HostConfig": {
                "NetworkMode": self.option(command, "--network"),
                "CapDrop": [self.option(command, "--cap-drop")],
                "CapAdd": None,
                "SecurityOpt": [
                    self.option(command, "--security-opt", occurrence=0),
                    self.option(command, "--security-opt", occurrence=1),
                ],
                "ReadonlyRootfs": "--read-only" in command,
                "Memory": 134_217_728,
                "MemorySwap": 134_217_728,
                "NanoCpus": 250_000_000,
                "PidsLimit": int(self.option(command, "--pids-limit")),
                "ShmSize": 16_777_216,
                "Tmpfs": {"/tmp": self.option(command, "--tmpfs").split(":", 1)[1]},
                "LogConfig": {
                    "Type": self.option(command, "--log-driver"),
                    "Config": {"max-size": "1m", "max-file": "1"},
                },
                "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
                "Privileged": False,
                "PidMode": "",
                "IpcMode": "private",
                "UTSMode": "",
                "Binds": None,
                "PortBindings": {},
                "Devices": None,
                "DeviceRequests": None,
            },
            "Mounts": [{"Type": "tmpfs", "Destination": "/tmp", "RW": True}],
        }

    @staticmethod
    def option(command: tuple[str, ...], name: str, occurrence: int = 0) -> str:
        indexes = [index for index, value in enumerate(command) if value == name]
        return command[indexes[occurrence] + 1]

    @staticmethod
    def assert_argv(argv: Sequence[str], timeout_seconds: float) -> None:
        if isinstance(argv, str):
            raise AssertionError("runner received shell text instead of argv")
        if not all(isinstance(value, str) for value in argv):
            raise AssertionError("runner received a non-string argv item")
        if timeout_seconds <= 0:
            raise AssertionError("runner received no timeout")


class DockerPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = DockerPreflightConfig(
            app_image="example/app:22",
            verifier_image="example/verifier:22",
        )

    def test_constructs_isolated_digest_pinned_containers_and_cleans_exact_ids(self) -> None:
        runner = FakeDockerRunner()

        result = run_docker_preflight(self.config, runner=runner, run_id="test-run")

        self.assertEqual(Outcome.PASSED, result.outcome)
        self.assertEqual("default", result.docker_context)
        self.assertEqual("npipe:////./pipe/docker_engine", result.docker_endpoint)
        self.assertEqual("29.7.2", result.docker_server_version)
        self.assertTrue(result.cleanup_attempted)
        self.assertTrue(result.cleanup_completed)
        self.assertRegex(result.verifier_program_digest, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(APP_DIGEST, result.app_image.repository_digest)
        self.assertEqual(VERIFIER_DIGEST, result.verifier_image.repository_digest)
        self.assertFalse(any(call[1:3] == ("image", "pull") for call in runner.calls))

        creates = [call for call in runner.calls if call[1:3] == ("container", "create")]
        self.assertEqual(2, len(creates))
        app, verifier = creates
        for command, digest in ((app, APP_DIGEST), (verifier, VERIFIER_DIGEST)):
            self.assertIn(digest, command)
            self.assertEqual("never", runner.option(command, "--pull"))
            self.assertEqual("65534:65534", runner.option(command, "--user"))
            self.assertEqual("ALL", runner.option(command, "--cap-drop"))
            self.assertEqual("no-new-privileges=true", runner.option(command, "--security-opt"))
            self.assertEqual(
                "seccomp=builtin",
                runner.option(command, "--security-opt", occurrence=1),
            )
            self.assertIn("--read-only", command)
            self.assertIn("--memory", command)
            self.assertIn("--cpus", command)
            self.assertIn("--pids-limit", command)
        self.assertEqual("none", runner.option(app, "--network"))
        self.assertEqual(f"container:{APP_ID}", runner.option(verifier, "--network"))
        self.assertIn("/health", verifier[-1])
        self.assertIn("/sys/class/net", verifier[-1])
        self.assertIn("/proc/net/route", verifier[-1])
        self.assertIn("/proc/net/ipv6_route", verifier[-1])
        self.assertIn("hasOnlyLoopbackInterface", verifier[-1])
        self.assertIn("routesUseOnlyLoopback", verifier[-1])
        self.assertIn("1.1.1.1", verifier[-1])
        self.assertTrue(
            {
                "loopback_health",
                "only_loopback_interfaces",
                "no_non_loopback_routes",
                "egress_blocked",
            }.issubset(result.checks)
        )
        self.assertTrue(
            all(
                call[1:3] == ("--host", "npipe:////./pipe/docker_engine")
                for call in runner.raw_calls[2:]
            )
        )

        removals = [call for call in runner.calls if call[1:4] == ("container", "rm", "--force")]
        self.assertEqual([VERIFIER_ID, APP_ID], [call[-1] for call in removals])
        self.assertTrue(all("--volumes" in call for call in removals))
        self.assertEqual({}, runner.containers)
        self.assertNotIn("prune", " ".join(" ".join(call) for call in runner.calls))

    def test_network_guard_rejects_specific_routes_and_extra_interfaces(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.fail("Node is required to execute the M0 trusted verifier parser test")
        node_path = Path(node).resolve()
        repository = Path.cwd().resolve()
        if node_path == repository or repository in node_path.parents:
            self.fail("refusing to execute a repository-local Node binary")
        ipv4_header = (
            "Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT"
        )
        ipv4_loopback = (
            f"{ipv4_header}\n"
            "lo 0000007F 00000000 0001 0 0 0 000000FF 0 0 0\n"
        )
        ipv4_specific_external = (
            f"{ipv4_header}\n"
            "eth0 FE00A9FE 00000000 0001 0 0 1000 FFFFFFFF 0 0 0\n"
        )
        ipv6_loopback = (
            f"{'0' * 31}1 80 {'0' * 32} 00 {'0' * 32} "
            "00000000 00000004 00000000 80200001 lo\n"
        )
        ipv6_specific_external = (
            f"20010db8{'0' * 24} 40 {'0' * 32} 00 {'0' * 32} "
            "00000000 00000001 00000000 00000001 eth0\n"
        )
        script = _NETWORK_GUARD + f"""
const results = {{
  loopbackAccepted: hasOnlyLoopbackInterface(['lo'])
    && routesUseOnlyLoopback({json.dumps(ipv4_loopback)}, {json.dumps(ipv6_loopback)}),
  extraInterfaceRejected: !hasOnlyLoopbackInterface(['lo', 'eth0']),
  ipv4SpecificRouteRejected: !routesUseOnlyLoopback(
    {json.dumps(ipv4_specific_external)}, {json.dumps(ipv6_loopback)}),
  ipv6SpecificRouteRejected: !routesUseOnlyLoopback(
    {json.dumps(ipv4_loopback)}, {json.dumps(ipv6_specific_external)}),
  malformedRouteRejected: !routesUseOnlyLoopback('not a route table', ''),
}};
console.log(JSON.stringify(results));
"""

        completed = subprocess.run(
            [str(node_path), "-e", script],
            cwd=str(node_path.parent),
            stdin=subprocess.DEVNULL,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            timeout=5,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(
            {
                "loopbackAccepted": True,
                "extraInterfaceRejected": True,
                "ipv4SpecificRouteRejected": True,
                "ipv6SpecificRouteRejected": True,
                "malformedRouteRejected": True,
            },
            json.loads(completed.stdout),
        )

    def test_refuses_an_image_without_repository_digest_before_container_creation(self) -> None:
        runner = FakeDockerRunner()
        runner.image_digests["example/app:22"] = []

        result = run_docker_preflight(self.config, runner=runner, run_id="no-digest")

        self.assertEqual(Outcome.INFRASTRUCTURE_ERROR, result.outcome)
        self.assertIn("no immutable repository digest", result.errors[0])
        self.assertFalse(any(call[1:3] == ("container", "create") for call in runner.calls))

    def test_refuses_a_digest_alias_from_a_different_repository(self) -> None:
        runner = FakeDockerRunner()
        runner.image_digests["example/app:22"] = [
            "different/repository@sha256:" + "d" * 64
        ]

        result = run_docker_preflight(self.config, runner=runner, run_id="wrong-repo")

        self.assertEqual(Outcome.INFRASTRUCTURE_ERROR, result.outcome)
        self.assertIn("no immutable repository digest", result.errors[0])
        self.assertFalse(any(call[1:3] == ("container", "create") for call in runner.calls))

    def test_requires_an_explicit_numeric_non_root_uid_and_gid(self) -> None:
        for user in ("0:0", "00:00", "root:root", "nobody:nogroup", "65534"):
            with self.subTest(user=user), self.assertRaises(ValueError):
                DockerPreflightConfig(
                    app_image="example/app:22",
                    verifier_image="example/verifier:22",
                    container_user=user,
                )

    def test_refuses_cleanup_when_recorded_container_ownership_changed(self) -> None:
        runner = FakeDockerRunner()
        runner.mutate_app_label_during_cleanup = True

        result = run_docker_preflight(self.config, runner=runner, run_id="ownership")

        self.assertEqual(Outcome.CLEANUP_FAILED, result.outcome)
        self.assertTrue(any("ownership label changed" in error for error in result.cleanup_errors))
        app_removals = [call for call in runner.calls if call[-1:] == (APP_ID,) and "rm" in call]
        self.assertEqual([], app_removals)

    def test_daemon_failure_is_infrastructure_error_without_false_cleanup_failure(self) -> None:
        runner = FakeDockerRunner()
        runner.fail_image_inspect = True

        result = run_docker_preflight(self.config, runner=runner, run_id="daemon-down")

        self.assertEqual(Outcome.INFRASTRUCTURE_ERROR, result.outcome)
        self.assertEqual((), result.cleanup_errors)
        self.assertIn("daemon unavailable", result.errors[0])

    def test_refuses_remote_docker_endpoint_before_daemon_access(self) -> None:
        runner = FakeDockerRunner()
        runner.endpoint = "tcp://build-host.example:2376"

        result = run_docker_preflight(self.config, runner=runner, run_id="remote-daemon")

        self.assertEqual(Outcome.POLICY_BLOCKED, result.outcome)
        self.assertIn("approved local", result.errors[0])
        self.assertEqual(2, len(runner.calls))
        self.assertFalse(any(call[1:3] == ("image", "inspect") for call in runner.calls))

    def test_requires_linux_daemon_with_seccomp_before_image_access(self) -> None:
        for os_type, options, expected in (
            ("windows", ["name=seccomp,profile=builtin"], "expected 'linux'"),
            ("linux", [], "enabled seccomp"),
        ):
            with self.subTest(os_type=os_type, options=options):
                runner = FakeDockerRunner()
                runner.daemon_os = os_type
                runner.security_options = options

                result = run_docker_preflight(
                    self.config,
                    runner=runner,
                    run_id=f"daemon-{os_type}-{len(options)}",
                )

                self.assertEqual(Outcome.UNSUPPORTED, result.outcome)
                self.assertIn(expected, result.errors[0])
                self.assertFalse(
                    any(call[1:3] == ("image", "inspect") for call in runner.calls)
                )

    def test_rejects_a_resource_limit_that_differs_from_the_requested_budget(self) -> None:
        runner = FakeDockerRunner()
        runner.tamper_app_memory_limit = True

        result = run_docker_preflight(self.config, runner=runner, run_id="wrong-budget")

        self.assertEqual(Outcome.INFRASTRUCTURE_ERROR, result.outcome)
        self.assertIn("memory limit", result.errors[0])
        self.assertEqual({}, runner.containers)

    def test_verifier_failure_is_infrastructure_error_and_still_cleans_up(self) -> None:
        runner = FakeDockerRunner()
        runner.verifier_exit = 7

        result = run_docker_preflight(self.config, runner=runner, run_id="probe-fails")

        self.assertEqual(Outcome.INFRASTRUCTURE_ERROR, result.outcome)
        self.assertIn("trusted verifier exited 7", result.errors[0])
        self.assertEqual((), result.cleanup_errors)
        self.assertTrue(result.cleanup_attempted)
        self.assertTrue(result.cleanup_completed)
        self.assertIn("cleanup_complete", result.checks)
        self.assertEqual({}, runner.containers)

    def test_recovers_and_removes_containers_after_create_response_loss(self) -> None:
        for role, expected_removals in (
            ("app", [APP_ID]),
            ("verifier", [VERIFIER_ID, APP_ID]),
        ):
            with self.subTest(role=role):
                runner = FakeDockerRunner()
                runner.lose_create_response_for_role = role
                run_id = f"{role}-create-loss"

                result = run_docker_preflight(self.config, runner=runner, run_id=run_id)

                self.assertEqual(Outcome.INFRASTRUCTURE_ERROR, result.outcome)
                self.assertIn("recovered the owned container for cleanup", result.errors[0])
                self.assertTrue(result.cleanup_attempted)
                self.assertTrue(result.cleanup_completed)
                self.assertEqual((), result.cleanup_errors)
                self.assertEqual({}, runner.containers)
                recovered_id = APP_ID if role == "app" else VERIFIER_ID
                self.assertEqual(
                    recovered_id,
                    result.app_container_id if role == "app" else result.verifier_container_id,
                )
                recovery_name = f"firstrun-m0-{run_id[:24]}-{role}"
                recovery_inspections = [
                    call
                    for call in runner.calls
                    if call[1:3] == ("container", "inspect") and call[-1] == recovery_name
                ]
                self.assertEqual(1, len(recovery_inspections))
                removals = [
                    call[-1]
                    for call in runner.calls
                    if call[1:4] == ("container", "rm", "--force")
                ]
                self.assertEqual(expected_removals, removals)

    def test_recovers_and_removes_container_after_invalid_create_response(self) -> None:
        runner = FakeDockerRunner()
        runner.invalidate_create_response_for_role = "app"

        result = run_docker_preflight(self.config, runner=runner, run_id="invalid-create-id")

        self.assertEqual(Outcome.INFRASTRUCTURE_ERROR, result.outcome)
        self.assertIn("invalid app container ID", result.errors[0])
        self.assertIn("recovered the owned container for cleanup", result.errors[0])
        self.assertTrue(result.cleanup_completed)
        self.assertEqual({}, runner.containers)

    def test_pull_is_explicitly_opt_in(self) -> None:
        runner = FakeDockerRunner()
        config = DockerPreflightConfig(
            app_image=self.config.app_image,
            verifier_image=self.config.verifier_image,
            allow_pull=True,
        )

        result = run_docker_preflight(config, runner=runner, run_id="allow-pull")

        self.assertEqual(Outcome.PASSED, result.outcome)
        pulls = [call for call in runner.calls if call[1:3] == ("image", "pull")]
        self.assertEqual(2, len(pulls))
        self.assertTrue(all("linux/amd64" in call for call in pulls))

    @patch("firstrun.preflight.docker.subprocess.run")
    def test_subprocess_runner_is_non_interactive_and_bounds_output(self, run) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=["docker", "version"],
            returncode=7,
            stdout="x" * 500,
            stderr="y" * 500,
        )

        trusted_docker = r"C:\Trusted\docker.exe"
        with patch(
            "firstrun.preflight.docker.shutil.which", return_value=trusted_docker
        ):
            result = SubprocessDockerCliRunner(max_output_chars=128).run(
                ("docker", "version"), timeout_seconds=3
            )

        self.assertEqual(7, result.returncode)
        self.assertLessEqual(len(result.stdout), 128)
        self.assertLessEqual(len(result.stderr), 128)
        self.assertTrue(result.stdout.endswith("...[truncated]"))
        trusted_path = Path(trusted_docker).resolve()
        run.assert_called_once_with(
            [str(trusted_path), "version"],
            cwd=str(trusted_path.parent),
            stdin=subprocess.DEVNULL,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            timeout=3,
        )

    @patch("firstrun.preflight.docker.subprocess.run")
    def test_subprocess_runner_refuses_a_repo_local_docker_binary(self, run) -> None:
        repo = Path("test-repository").resolve()
        runner = SubprocessDockerCliRunner(forbidden_roots=(repo,))

        with patch(
            "firstrun.preflight.docker.shutil.which",
            return_value=str(repo / "docker.exe"),
        ):
            result = runner.run(("docker", "version"), timeout_seconds=3)

        self.assertEqual(126, result.returncode)
        self.assertIn("untrusted root", result.stderr)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
