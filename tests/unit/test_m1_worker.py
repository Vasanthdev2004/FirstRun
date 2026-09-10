from __future__ import annotations

import json
import os
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import UUID

from firstrun.domain.contracts import Recipe, RecipeStep, load_recipe, load_target
from firstrun.domain.evidence import CommandEvidence, ContentDigest, ControllerBinding
from firstrun.domain.outcomes import Outcome
from firstrun.preflight.docker import (
    CommandResult,
    DockerPreflightConfig,
    ResolvedImage,
    _PolicyProblem,
    _UnsupportedProblem,
)
from firstrun.verification.policy import CONTROLLED_NODE_FIXTURE_POLICY
from firstrun.verification.readme import replace_block_bytes
from firstrun.verification.source import (
    SourcePolicyError,
    build_candidate_patch,
    capture_approved_source,
)
from firstrun.worker.docker import (
    PreparedDockerRuntime,
    WorkerProblem,
    _DeadlineDockerCliRunner,
    _assert_candidate_authorized,
    _assert_recipe_authorized,
    _cleanup,
    _create_app,
    _quarantine_endpoint,
    _parse_verifier_result,
    _read_start_result,
    _start_evidence,
    prepare_docker_runtime,
    run_docker_phase,
)


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures" / "notes-app"


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.timeouts: list[float] = []

    def run(self, argv: tuple[str, ...], *, timeout_seconds: float) -> CommandResult:
        self.calls.append(tuple(argv))
        self.timeouts.append(timeout_seconds)
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

    @staticmethod
    def authority() -> ControllerBinding:
        return ControllerBinding(
            run_id=UUID("11111111-1111-4111-8111-111111111111"),
            attempt_id=UUID("22222222-2222-4222-8222-222222222222"),
            workspace_id=UUID("33333333-3333-4333-8333-333333333333"),
        )

    @staticmethod
    def command_evidence(step: RecipeStep, outcome: Outcome) -> CommandEvidence:
        return CommandEvidence(
            step_id=step.id,
            kind="foreground",
            argv=step.argv,
            cwd=step.cwd,
            timeout_seconds=step.timeout_seconds,
            outcome=outcome,
            exit_code=0 if outcome is Outcome.PASSED else 1,
        )

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

    def test_candidate_cannot_change_readme_outside_managed_block(self) -> None:
        recipe_bytes = (ROOT / "examples" / "recipe.fixed.json").read_bytes()
        expected_readme = replace_block_bytes(
            self.snapshot.files["README.md"], self.fixed
        )
        for changed in (b"unexpected prefix\n" + expected_readme, expected_readme + b"suffix"):
            candidate = build_candidate_patch(
                self.snapshot,
                {
                    ".firstrun/recipe.json": recipe_bytes,
                    "README.md": changed,
                },
            )
            with self.subTest(changed=changed[:20]), self.assertRaises(SourcePolicyError):
                _assert_candidate_authorized(self.snapshot, candidate, self.fixed)

    def test_candidate_digest_cannot_be_forged(self) -> None:
        candidate = build_candidate_patch(
            self.snapshot,
            {
                ".firstrun/recipe.json": (ROOT / "examples" / "recipe.fixed.json").read_bytes(),
                "README.md": replace_block_bytes(
                    self.snapshot.files["README.md"], self.fixed
                ),
            },
        )
        forged_patch = replace(candidate, patch_digest="sha256:" + "0" * 64)
        forged_tree = replace(candidate, candidate_tree_digest="sha256:" + "0" * 64)

        for forged in (forged_patch, forged_tree):
            with self.subTest(digest=forged.patch_digest), self.assertRaises(
                SourcePolicyError
            ):
                _assert_candidate_authorized(self.snapshot, forged, self.fixed)

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

    def test_incomplete_verifier_pass_is_rejected(self) -> None:
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
            "outcome": "passed",
            "readinessAttempts": 1,
            "observedStatus": 200,
            "passedChecks": [],
            "nonceDigest": None,
            "error": None,
        }
        with self.assertRaises(WorkerProblem) as raised:
            _parse_verifier_result(
                CommandResult(0, json.dumps(payload)), authority, self.target
            )
        self.assertIs(raised.exception.outcome, Outcome.INFRASTRUCTURE_ERROR)

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
            patch("firstrun.worker.docker._quarantine_endpoint") as quarantine,
        ):
            with self.assertRaises(WorkerProblem) as raised:
                prepare_docker_runtime(
                    self.target,
                    controller_repository_root=self.snapshot.repository_root,
                    runner=runner,
                )

        self.assertIs(raised.exception.outcome, Outcome.CLEANUP_FAILED)
        self.assertIn("quarantined", str(raised.exception))
        quarantine.assert_called_once_with("npipe:////./pipe/dockerDesktopLinuxEngine")

    def test_default_runner_refuses_controller_owned_docker_from_outside_cwd(self) -> None:
        fake_docker = ROOT / "tools" / "docker.exe"
        with (
            patch("firstrun.worker.docker.Path.cwd", return_value=ROOT.parent),
            patch(
                "firstrun.preflight.docker.shutil.which",
                return_value=str(fake_docker),
            ),
            patch("firstrun.preflight.docker.subprocess.Popen") as host_process,
            self.assertRaises(WorkerProblem) as raised,
        ):
            prepare_docker_runtime(
                self.target,
                controller_repository_root=self.snapshot.repository_root,
            )

        self.assertIs(raised.exception.outcome, Outcome.INFRASTRUCTURE_ERROR)
        self.assertIn("untrusted root", str(raised.exception))
        host_process.assert_not_called()

    def test_quarantined_endpoint_blocks_subsequent_prepare(self) -> None:
        endpoint = "npipe:////./pipe/dockerDesktopLinuxEngine"
        with (
            patch("firstrun.worker.docker._QUARANTINED_ENDPOINTS", set()),
            patch(
                "firstrun.worker.docker._resolve_local_docker_endpoint",
                return_value=("desktop-linux", endpoint),
            ),
            patch("firstrun.worker.docker._assert_daemon_security") as daemon_check,
        ):
            _quarantine_endpoint(endpoint)
            with self.assertRaises(WorkerProblem) as raised:
                prepare_docker_runtime(
                    self.target,
                    controller_repository_root=self.snapshot.repository_root,
                    runner=RecordingRunner(),
                )

        self.assertIs(raised.exception.outcome, Outcome.CLEANUP_FAILED)
        daemon_check.assert_not_called()

    def test_quarantined_endpoint_blocks_already_prepared_runtime(self) -> None:
        runner = RecordingRunner()
        selected_runtime = runtime(runner)
        with patch("firstrun.worker.docker._QUARANTINED_ENDPOINTS", set()):
            _quarantine_endpoint(selected_runtime.docker_endpoint)
            result = run_docker_phase(
                selected_runtime,
                self.snapshot,
                self.target,
                self.baseline,
                phase="baseline",
                binding=self.authority(),
            )

        self.assertIs(result.outcome, Outcome.CLEANUP_FAILED)
        self.assertIn("quarantined", result.error or "")
        self.assertEqual([], runner.calls)

    def test_preflight_policy_and_unsupported_errors_keep_their_taxonomy(self) -> None:
        for problem, expected in (
            (_PolicyProblem("blocked"), Outcome.POLICY_BLOCKED),
            (_UnsupportedProblem("unsupported"), Outcome.UNSUPPORTED),
        ):
            with (
                self.subTest(expected=expected),
                patch(
                    "firstrun.worker.docker._resolve_local_docker_endpoint",
                    side_effect=problem,
                ),
                self.assertRaises(WorkerProblem) as raised,
            ):
                prepare_docker_runtime(
                    self.target,
                    controller_repository_root=self.snapshot.repository_root,
                    runner=RecordingRunner(),
                )
            self.assertIs(raised.exception.outcome, expected)

    def test_phase_runner_caps_each_call_to_one_wall_deadline(self) -> None:
        runner = RecordingRunner()
        bounded = _DeadlineDockerCliRunner(runner, 120.0, clock=lambda: 119.25)

        bounded.run(("docker", "version"), timeout_seconds=600)

        self.assertEqual(runner.calls, [("docker", "version")])
        self.assertEqual(runner.timeouts, [0.75])
        expired = _DeadlineDockerCliRunner(runner, 120.0, clock=lambda: 120.01)
        with self.assertRaises(WorkerProblem) as raised:
            expired.run(("docker", "version"), timeout_seconds=600)
        self.assertIs(raised.exception.outcome, Outcome.TIMED_OUT)

        late_clock = iter((119.0, 120.01))
        late = _DeadlineDockerCliRunner(runner, 120.0, clock=lambda: next(late_clock))
        with self.assertRaises(WorkerProblem) as raised:
            late.run(("docker", "version"), timeout_seconds=1)
        self.assertIs(raised.exception.outcome, Outcome.TIMED_OUT)

    def test_malformed_start_observation_is_infrastructure_not_app_failure(self) -> None:
        runner = Mock()
        runner.run.return_value = CommandResult(
            0, json.dumps({"exists": True, "malformed": True})
        )
        with self.assertRaises(WorkerProblem) as raised:
            _read_start_result(runtime(runner), "a" * 64, wait_milliseconds=0)
        self.assertIs(raised.exception.outcome, Outcome.INFRASTRUCTURE_ERROR)

    def test_observed_start_exit_is_failed_command_evidence(self) -> None:
        evidence = _start_evidence(
            self.baseline.start,
            {"exists": True, "exitCode": 1, "signal": None, "error": None},
        )
        self.assertIs(evidence.outcome, Outcome.FAILED)
        self.assertEqual(evidence.exit_code, 1)
        self.assertFalse(evidence.succeeded)

    def test_early_command_failure_retains_typed_attempt_and_cleanup(self) -> None:
        app_id = "a" * 64
        failed_command = self.command_evidence(self.baseline.steps[0], Outcome.FAILED)
        marker_digest = ContentDigest("sha256:" + "e" * 64)
        with (
            patch("firstrun.worker.docker._create_app", return_value=app_id),
            patch("firstrun.worker.docker._assert_container"),
            patch("firstrun.worker.docker._start_container"),
            patch(
                "firstrun.worker.docker._seed_fresh_workspace",
                return_value=marker_digest,
            ),
            patch("firstrun.worker.docker._write_workspace"),
            patch(
                "firstrun.worker.docker._execute_foreground",
                return_value=failed_command,
            ),
            patch(
                "firstrun.worker.docker._cleanup",
                return_value=([], {app_id}, {}),
            ),
        ):
            result = run_docker_phase(
                runtime(RecordingRunner()),
                self.snapshot,
                self.target,
                self.baseline,
                phase="baseline",
                binding=self.authority(),
            )

        self.assertIs(result.outcome, Outcome.FAILED)
        self.assertIsNone(result.evidence)
        self.assertIsNotNone(result.attempt)
        assert result.attempt is not None
        self.assertEqual(result.attempt.app_container_id, app_id)
        self.assertIsNone(result.attempt.verifier_container_id)
        self.assertEqual(result.attempt.workspace_marker_digest, marker_digest)
        self.assertEqual(result.attempt.base_git_tree, self.snapshot.base_tree)
        self.assertEqual(result.attempt.source_git_tree, self.snapshot.source_tree)
        self.assertTrue(result.attempt.cleanup.succeeded)
        self.assertEqual(result.attempt.commands, (failed_command,))

    def test_early_start_exit_stops_before_verifier_and_is_retained(self) -> None:
        app_id = "a" * 64
        passed_command = self.command_evidence(self.baseline.steps[0], Outcome.PASSED)
        marker_digest = ContentDigest("sha256:" + "e" * 64)
        with (
            patch("firstrun.worker.docker._create_app", return_value=app_id),
            patch("firstrun.worker.docker._assert_container"),
            patch("firstrun.worker.docker._start_container"),
            patch(
                "firstrun.worker.docker._seed_fresh_workspace",
                return_value=marker_digest,
            ),
            patch("firstrun.worker.docker._write_workspace"),
            patch(
                "firstrun.worker.docker._execute_foreground",
                return_value=passed_command,
            ),
            patch("firstrun.worker.docker._launch_start"),
            patch(
                "firstrun.worker.docker._read_start_result",
                return_value={
                    "exists": True,
                    "exitCode": 1,
                    "signal": None,
                    "error": None,
                },
            ),
            patch("firstrun.worker.docker._create_verifier") as create_verifier,
            patch(
                "firstrun.worker.docker._cleanup",
                return_value=([], {app_id}, {}),
            ),
        ):
            result = run_docker_phase(
                runtime(RecordingRunner()),
                self.snapshot,
                self.target,
                self.baseline,
                phase="baseline",
                binding=self.authority(),
            )

        create_verifier.assert_not_called()
        self.assertIs(result.outcome, Outcome.FAILED)
        self.assertIsNone(result.evidence)
        assert result.attempt is not None
        self.assertEqual(result.attempt.commands[-1].kind, "start")
        self.assertFalse(result.attempt.commands[-1].succeeded)

    def test_current_run_cleanup_failure_is_evidenced_and_quarantined(self) -> None:
        app_id = "a" * 64
        failed_command = self.command_evidence(self.baseline.steps[0], Outcome.FAILED)
        selected_runtime = runtime(RecordingRunner())
        with (
            patch("firstrun.worker.docker._create_app", return_value=app_id),
            patch("firstrun.worker.docker._assert_container"),
            patch("firstrun.worker.docker._start_container"),
            patch(
                "firstrun.worker.docker._seed_fresh_workspace",
                return_value=ContentDigest("sha256:" + "e" * 64),
            ),
            patch("firstrun.worker.docker._write_workspace"),
            patch("firstrun.worker.docker._execute_foreground", return_value=failed_command),
            patch(
                "firstrun.worker.docker._cleanup",
                return_value=(["forced cleanup failure"], set(), {}),
            ),
            patch("firstrun.worker.docker._quarantine_endpoint") as quarantine,
        ):
            result = run_docker_phase(
                selected_runtime,
                self.snapshot,
                self.target,
                self.baseline,
                phase="baseline",
                binding=self.authority(),
            )

        self.assertIs(result.outcome, Outcome.CLEANUP_FAILED)
        assert result.attempt is not None
        self.assertFalse(result.attempt.cleanup.succeeded)
        self.assertTrue(result.attempt.cleanup.worker_quarantined)
        self.assertIn("forced cleanup failure", result.attempt.cleanup.sanitized_errors)
        quarantine.assert_called_once_with(selected_runtime.docker_endpoint)

    def test_cleanup_handles_malformed_inspection_without_escaping(self) -> None:
        app_id = "a" * 64
        with (
            patch("firstrun.worker.docker._inspect", return_value={"Config": []}),
            patch(
                "firstrun.worker.docker._list_containers_by_label",
                side_effect=((app_id,), (app_id,)),
            ),
        ):
            errors, removed, _ = _cleanup(
                runtime(RecordingRunner()), self.authority(), ((app_id, "app"),)
            )
        self.assertTrue(errors)
        self.assertFalse(removed)


if __name__ == "__main__":
    unittest.main()
