from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from firstrun.cli import run
from firstrun.domain import Outcome
from firstrun.preflight.docker import DockerPreflightResult, ResolvedImage
from firstrun.preflight.strands import StrandsPreflightResult
from firstrun.verification.local import LocalVerificationResult


class CliTests(unittest.TestCase):
    @patch("firstrun.cli.verify_local")
    def test_verify_local_uses_required_paths_and_stable_outcome_code(
        self, verify
    ) -> None:
        verify.return_value = LocalVerificationResult(
            outcome=Outcome.FAILED,
            source_revision="a" * 40,
            baseline=None,
            message="functional probe failed",
        )
        output = io.StringIO()

        with redirect_stdout(output):
            code = run(
                (
                    "verify-local",
                    "--repo",
                    "fixtures/notes-app",
                    "--target",
                    "fixtures/notes-app/.firstrun/target.json",
                    "--json",
                )
            )

        self.assertEqual(code, 10)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["outcome"], "failed")
        self.assertEqual(payload["exit_code"], 10)
        selected_repo, selected_target = verify.call_args.args
        self.assertEqual(str(selected_repo).replace("\\", "/"), "fixtures/notes-app")
        self.assertEqual(
            str(selected_target).replace("\\", "/"),
            "fixtures/notes-app/.firstrun/target.json",
        )

    @patch("firstrun.cli.run_doctor")
    def test_doctor_json_returns_authoritative_exit_code(self, doctor) -> None:
        doctor.return_value = {
            "schema_version": 1,
            "repo_path": "C:/repo",
            "outcome": "infrastructure_error",
            "exit_code": 12,
            "checks": {"docker_server": {"status": "failed", "message": "down"}},
        }
        output = io.StringIO()

        with redirect_stdout(output):
            code = run(("doctor", "--repo", ".", "--json"))

        self.assertEqual(code, 12)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["outcome"], "infrastructure_error")
        doctor.assert_called_once()

    @patch("firstrun.cli.run_doctor")
    def test_human_output_contains_each_check_without_details(self, doctor) -> None:
        doctor.return_value = {
            "schema_version": 1,
            "repo_path": "C:/repo",
            "outcome": "passed",
            "exit_code": 0,
            "checks": {
                "python": {
                    "status": "passed",
                    "message": "Python is supported",
                    "details": {"version": "3.12.13"},
                }
            },
        }
        output = io.StringIO()

        with redirect_stdout(output):
            code = run(("doctor",))

        self.assertEqual(code, 0)
        self.assertIn("FirstRun doctor: passed", output.getvalue())
        self.assertIn("python: passed", output.getvalue())
        self.assertNotIn("3.12.13", output.getvalue())

    @patch("firstrun.cli.run_docker_preflight")
    def test_docker_preflight_requires_explicit_container_acknowledgement(
        self, preflight
    ) -> None:
        output = io.StringIO()

        with redirect_stdout(output):
            code = run(("preflight-docker", "--json"))

        self.assertEqual(13, code)
        payload = json.loads(output.getvalue())
        self.assertEqual("policy_blocked", payload["outcome"])
        preflight.assert_not_called()

    @patch("firstrun.cli.run_docker_preflight")
    def test_docker_preflight_uses_controller_selected_images_and_exit_code(
        self, preflight
    ) -> None:
        image = ResolvedImage(
            supplied_reference="node:22-bookworm-slim",
            repository_digest="node@sha256:" + "a" * 64,
            image_id="sha256:" + "b" * 64,
            os="linux",
            architecture="amd64",
        )
        preflight.return_value = DockerPreflightResult(
            outcome=Outcome.INFRASTRUCTURE_ERROR,
            run_id="test-run",
            docker_context="default",
            docker_endpoint="npipe:////./pipe/docker_engine",
            docker_server_version="29.7.2",
            app_image=image,
            verifier_image=image,
            app_program_digest="sha256:" + "c" * 64,
            verifier_program_digest="sha256:" + "d" * 64,
            app_container_id=None,
            verifier_container_id=None,
            checks=("app_image_pinned",),
            errors=("daemon unavailable",),
            cleanup_errors=(),
            cleanup_attempted=False,
            cleanup_completed=False,
        )
        output = io.StringIO()

        with redirect_stdout(output):
            code = run(
                (
                    "preflight-docker",
                    "--acknowledge-container-changes",
                    "--allow-pull",
                    "--json",
                )
            )

        self.assertEqual(12, code)
        payload = json.loads(output.getvalue())
        self.assertEqual("infrastructure_error", payload["outcome"])
        self.assertEqual(12, payload["exit_code"])
        config = preflight.call_args.args[0]
        self.assertEqual("node:22-bookworm-slim", config.app_image)
        self.assertEqual(config.app_image, config.verifier_image)
        self.assertTrue(config.allow_pull)

    @patch("firstrun.cli.run_strands_preflight")
    def test_strands_preflight_requires_explicit_cost_acknowledgement(
        self, preflight
    ) -> None:
        output = io.StringIO()

        with redirect_stdout(output):
            code = run(
                (
                    "preflight-strands",
                    "--aws-profile",
                    "firstrun-dev",
                    "--region",
                    "us-west-2",
                    "--model-id",
                    "us.example.model-v1:0",
                    "--json",
                )
            )

        self.assertEqual(13, code)
        payload = json.loads(output.getvalue())
        self.assertEqual("policy_blocked", payload["outcome"])
        self.assertFalse(payload["provider_cost_acknowledged"])
        self.assertFalse(payload["credential_identity_verified"])
        preflight.assert_not_called()

    @patch("firstrun.cli.run_strands_preflight")
    def test_strands_preflight_requires_verified_temporary_non_root_identity(
        self, preflight
    ) -> None:
        output = io.StringIO()

        with redirect_stdout(output):
            code = run(
                (
                    "preflight-strands",
                    "--aws-profile",
                    "firstrun-dev",
                    "--region",
                    "us-west-2",
                    "--model-id",
                    "us.example.model-v1:0",
                    "--acknowledge-provider-cost",
                    "--json",
                )
            )

        self.assertEqual(13, code)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["provider_cost_acknowledged"])
        self.assertFalse(payload["credential_identity_verified"])
        preflight.assert_not_called()

    @patch("firstrun.cli.run_strands_preflight")
    def test_strands_preflight_passes_only_explicit_provider_selection(
        self, preflight
    ) -> None:
        preflight.return_value = StrandsPreflightResult(
            outcome=Outcome.PASSED,
            provider="amazon-bedrock",
            aws_profile="firstrun-dev",
            region="us-west-2",
            model_id="us.example.model-v1:0",
            sdk_version="1.55.1",
            provider_endpoint="https://bedrock-runtime.us-west-2.amazonaws.com",
            tool_invocation_count=1,
            tool_nonce_matched=True,
            structured_nonce_matched=True,
            nonce_matched=True,
            timeout_seconds=60.0,
            max_turns=4,
            max_output_tokens=256,
            max_total_tokens=1024,
            provider_total_attempts=2,
            strands_model_attempts_per_turn=1,
            provider_cost_acknowledged=True,
            credential_identity_verified=True,
            isolated_python=True,
            evidence_source="live_provider_subprocess",
            live_provider_invoked=True,
            milestone_eligible=True,
            error_code=None,
            message="probe passed",
        )
        output = io.StringIO()

        with redirect_stdout(output):
            code = run(
                (
                    "preflight-strands",
                    "--aws-profile",
                    "firstrun-dev",
                    "--region",
                    "us-west-2",
                    "--model-id",
                    "us.example.model-v1:0",
                    "--acknowledge-provider-cost",
                    "--confirm-verified-temporary-non-root-credentials",
                    "--json",
                )
            )

        self.assertEqual(0, code)
        payload = json.loads(output.getvalue())
        self.assertEqual("passed", payload["outcome"])
        self.assertTrue(payload["milestone_eligible"])
        self.assertEqual("live_provider_subprocess", payload["evidence_source"])
        selected = preflight.call_args.args[0]
        self.assertEqual("firstrun-dev", selected.aws_profile)
        self.assertEqual("us-west-2", selected.region)
        self.assertEqual("us.example.model-v1:0", selected.model_id)
        self.assertTrue(selected.provider_cost_acknowledged)
        self.assertTrue(selected.credential_identity_verified)


if __name__ == "__main__":
    unittest.main()
