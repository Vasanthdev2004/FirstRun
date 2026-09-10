from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from unittest.mock import Mock, patch
from uuid import uuid4

from firstrun.domain.contracts import load_recipe, load_target
from firstrun.domain.evidence import ControllerBinding
from firstrun.domain.outcomes import Outcome
from firstrun.verification.local import verify_known_oracle, verify_local
from firstrun.verification.source import capture_approved_source
from firstrun.worker.docker import DockerPhaseResult


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures" / "notes-app"
TARGET = FIXTURE / ".firstrun" / "target.json"


class LocalVerificationPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.snapshot = capture_approved_source(FIXTURE)
        cls.target = load_target(TARGET)
        cls.recipe = load_recipe(FIXTURE / ".firstrun" / "recipe.json")

    @staticmethod
    def binding() -> ControllerBinding:
        return ControllerBinding(
            run_id=uuid4(), attempt_id=uuid4(), workspace_id=uuid4()
        )

    def test_wrong_target_path_is_blocked_before_source_or_docker(self) -> None:
        with (
            patch("firstrun.verification.local.capture_approved_source") as capture,
            patch("firstrun.verification.local.prepare_docker_runtime") as docker,
        ):
            result = verify_local(FIXTURE, FIXTURE / "target.json")

        self.assertIs(result.outcome, Outcome.POLICY_BLOCKED)
        capture.assert_not_called()
        docker.assert_not_called()

    def test_docs_mismatch_is_blocked_before_any_docker_call(self) -> None:
        snapshot = self.snapshot
        files = dict(snapshot.files)
        files["README.md"] = files["README.md"].replace(b"npm run dev", b"npm run other")
        mismatched = replace(snapshot, files=MappingProxyType(files))
        with (
            patch(
                "firstrun.verification.local.capture_approved_source",
                return_value=mismatched,
            ),
            patch("firstrun.verification.local.prepare_docker_runtime") as docker,
        ):
            result = verify_local(FIXTURE, TARGET)

        self.assertIs(result.outcome, Outcome.POLICY_BLOCKED)
        self.assertIn("README managed block", result.message or "")
        docker.assert_not_called()

    def test_oracle_cannot_report_pass_when_broken_baseline_passes(self) -> None:
        phase = DockerPhaseResult(Outcome.PASSED, self.binding(), None)
        with (
            patch(
                "firstrun.verification.local._prepare_inputs",
                return_value=(self.snapshot, self.target, self.recipe),
            ),
            patch("firstrun.verification.local.prepare_docker_runtime", return_value=object()),
            patch("firstrun.verification.local.run_docker_phase", return_value=phase),
        ):
            result = verify_known_oracle(FIXTURE, TARGET)

        self.assertIs(result.outcome, Outcome.INFRASTRUCTURE_ERROR)
        self.assertIsNone(result.proof)

    def test_public_verification_cannot_pass_without_verified_evidence(self) -> None:
        phase = DockerPhaseResult(Outcome.PASSED, self.binding(), None)
        with (
            patch(
                "firstrun.verification.local._prepare_inputs",
                return_value=(self.snapshot, self.target, self.recipe),
            ),
            patch("firstrun.verification.local.prepare_docker_runtime", return_value=object()),
            patch("firstrun.verification.local.run_docker_phase", return_value=phase),
        ):
            result = verify_local(FIXTURE, TARGET)

        self.assertIs(result.outcome, Outcome.INFRASTRUCTURE_ERROR)
        self.assertIn("verified evidence", result.message or "")

    def test_oracle_cannot_report_pass_for_unverified_proof(self) -> None:
        baseline_evidence = Mock()
        proof_evidence = Mock(verified=False)
        phases = (
            DockerPhaseResult(Outcome.FAILED, self.binding(), baseline_evidence),
            DockerPhaseResult(Outcome.PASSED, self.binding(), proof_evidence),
        )
        with (
            patch(
                "firstrun.verification.local._prepare_inputs",
                return_value=(self.snapshot, self.target, self.recipe),
            ),
            patch("firstrun.verification.local.prepare_docker_runtime", return_value=object()),
            patch("firstrun.verification.local.run_docker_phase", side_effect=phases),
            patch(
                "firstrun.verification.local._is_expected_known_broken_baseline",
                return_value=True,
            ),
        ):
            result = verify_known_oracle(FIXTURE, TARGET)

        self.assertIs(result.outcome, Outcome.INFRASTRUCTURE_ERROR)
        self.assertIs(result.proof, proof_evidence)


if __name__ == "__main__":
    unittest.main()
