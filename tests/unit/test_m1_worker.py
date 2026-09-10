from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from firstrun.domain.contracts import Recipe, load_recipe, load_target
from firstrun.domain.evidence import ControllerBinding
from firstrun.domain.outcomes import Outcome
from firstrun.preflight.docker import CommandResult, DockerPreflightConfig, ResolvedImage
from firstrun.verification.policy import CONTROLLED_NODE_FIXTURE_POLICY
from firstrun.verification.source import SourcePolicyError, capture_approved_source
from firstrun.worker.docker import (
    PreparedDockerRuntime,
    WorkerProblem,
    _assert_recipe_authorized,
    _create_app,
    _parse_verifier_result,
    prepare_docker_runtime,
)


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures" / "notes-app"


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...], *, timeout_seconds: float) -> CommandResult:
        self.calls.append(tuple(argv))
        return CommandResult(0, "a" * 64 + "\n")


def runtime(runner: RecordingRunner) -> PreparedDockerRuntime:
    image_id = "sha256:" + "b" * 64
    digest = "node@sha256:" + "c" * 64
    config = DockerPreflightConfig(
        app_image="node:22-bookworm-slim",
        verifier_image="node:22-bookworm-slim",
        container_user=CONTROLLED_NODE_FIXTURE_POLICY.docker.user,
    )
    return PreparedDockerRuntime(
        docker_context="desktop-linux",
        docker_endpoint="npipe:////./pipe/dockerDesktopLinuxEngine",
        docker_server_version="test",
        image=ResolvedImage(
            "node:22-bookworm-slim", digest, image_id, "linux", "amd64"
        ),
        runner=runner,
        config=config,
    )


class M1WorkerPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.snapshot = capture_approved_source(FIXTURE)
        cls.baseline = load_recipe(FIXTURE / ".firstrun" / "recipe.json")
        cls.fixed = load_recipe(ROOT / "examples" / "recipe.fixed.json")
        cls.target = load_target(FIXTURE / ".firstrun" / "target.json")

    def test_baseline_and_existing_script_candidate_are_authorized(self) -> None:
        _assert_recipe_authorized(
            self.baseline, phase="baseline", source_files=self.snapshot.files
        )
        _assert_recipe_authorized(
            self.fixed, phase="proof", source_files=self.snapshot.files
        )

    def test_host_and_unknown_commands_are_denied(self) -> None:
        for argv in (
            ("powershell", "-Command", "Write-Output pwned"),
            ("cmd.exe", "/c", "echo pwned"),
            ("/bin/sh", "-c", "echo pwned"),
            ("npm", "run", "missing-script"),
        ):
            value = self.fixed.model_dump(mode="python")
            value["steps"][1]["argv"] = list(argv)
            candidate = Recipe.model_validate(value)
            with self.subTest(argv=argv), self.assertRaises(SourcePolicyError):
                _assert_recipe_authorized(
                    candidate, phase="proof", source_files=self.snapshot.files
                )

    def test_app_creation_has_no_host_mount_secret_or_privilege(self) -> None:
        runner = RecordingRunner()
        authority = ControllerBinding(
            run_id=UUID("11111111-1111-4111-8111-111111111111"),
            attempt_id=UUID("22222222-2222-4222-8222-222222222222"),
            workspace_id=UUID("33333333-3333-4333-8333-333333333333"),
        )
        with patch.dict(os.environ, {"AWS_SECRET_ACCESS_KEY": "host-sentinel"}):
            _create_app(runtime(runner), authority)

        argv = runner.calls[0]
        self.assertEqual(argv[0], "docker")
        self.assertEqual(argv[argv.index("--network") + 1], "none")
        self.assertEqual(
            argv[argv.index("--user") + 1],
            CONTROLLED_NODE_FIXTURE_POLICY.docker.user,
        )
        self.assertIn("--read-only", argv)
        self.assertIn("ALL", argv)
        self.assertNotIn("--mount", argv)
        self.assertNotIn("--volume", argv)
        self.assertFalse(any("host-sentinel" in item for item in argv))

    def test_functional_probe_nonzero_is_failed_not_infrastructure(self) -> None:
        authority = ControllerBinding(
            run_id=UUID("11111111-1111-4111-8111-111111111111"),
            attempt_id=UUID("22222222-2222-4222-8222-222222222222"),
            workspace_id=UUID("33333333-3333-4333-8333-333333333333"),
        )
        payload = {
            "schemaVersion": 1,
            "runId": str(authority.run_id),
            "attemptId": str(authority.attempt_id),
            "verifierId": "notes-create-read-v1",
            "outcome": "failed",
            "readinessAttempts": 2,
            "observedStatus": 200,
            "passedChecks": [],
            "nonceDigest": "sha256:" + "d" * 64,
            "error": "create_note failed with HTTP 500",
        }
        readiness, acceptance, outcome = _parse_verifier_result(
            CommandResult(10, json.dumps(payload)), authority, self.target
        )
        self.assertIs(outcome, Outcome.FAILED)
        self.assertTrue(readiness.succeeded)
        self.assertIs(acceptance.outcome, Outcome.FAILED)

    def test_prior_owned_residue_quarantines_worker(self) -> None:
        runner = RecordingRunner()
        resolved = ResolvedImage(
            "node:22-bookworm-slim",
            "node@sha256:" + "c" * 64,
            "sha256:" + "b" * 64,
            "linux",
            "amd64",
        )
        with (
            patch(
                "firstrun.worker.docker._resolve_local_docker_endpoint",
                return_value=("desktop-linux", "npipe:////./pipe/dockerDesktopLinuxEngine"),
            ),
            patch(
                "firstrun.worker.docker._assert_daemon_security",
                return_value="test-server",
            ),
            patch("firstrun.worker.docker._resolve_image", return_value=resolved),
            patch(
                "firstrun.worker.docker._list_containers_by_label",
                return_value=("d" * 64,),
            ),
        ):
            with self.assertRaises(WorkerProblem) as raised:
                prepare_docker_runtime(self.target, runner=runner)

        self.assertIs(raised.exception.outcome, Outcome.CLEANUP_FAILED)
        self.assertIn("quarantined", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
