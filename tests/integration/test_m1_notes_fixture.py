from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from pathlib import Path

from firstrun.domain.outcomes import Outcome
from firstrun.verification.local import verify_known_oracle, verify_local


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures" / "notes-app"
TARGET = FIXTURE / ".firstrun" / "target.json"


@unittest.skipUnless(
    os.environ.get("FIRSTRUN_RUN_LIVE_M1") == "1",
    "set FIRSTRUN_RUN_LIVE_M1=1 to authorize live controlled Docker acceptance",
)
class NotesFixtureM1AcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.oracle = verify_known_oracle(FIXTURE, TARGET)
        cls.after_proof_baseline = verify_local(FIXTURE, TARGET)

    def test_actual_broken_baseline_is_not_a_health_only_false_positive(self) -> None:
        baseline = self.oracle.baseline
        self.assertIsNotNone(baseline, self.oracle.message)
        assert baseline is not None
        self.assertIs(baseline.outcome, Outcome.FAILED)
        self.assertTrue(baseline.readiness.succeeded)
        self.assertEqual(baseline.readiness.observed_status, 200)
        self.assertIs(baseline.acceptance_probe.outcome, Outcome.FAILED)
        self.assertFalse(baseline.verified)
        self.assertTrue(baseline.cleanup.succeeded)

    def test_known_candidate_passes_exact_independent_proof(self) -> None:
        self.assertIs(self.oracle.outcome, Outcome.PASSED, self.oracle.message)
        proof = self.oracle.proof
        self.assertIsNotNone(proof)
        assert proof is not None
        self.assertIs(proof.outcome, Outcome.PASSED)
        self.assertTrue(proof.readiness.succeeded)
        self.assertTrue(proof.acceptance_probe.succeeded)
        self.assertTrue(proof.cleanup.succeeded)
        self.assertTrue(proof.verified)
        self.assertEqual(str(proof.candidate_digest), self.oracle.candidate_digest)
        self.assertEqual(
            str(proof.candidate_tree_digest), self.oracle.candidate_tree_digest
        )

    def test_target_runtime_and_verifier_are_frozen_while_state_is_fresh(self) -> None:
        baseline = self.oracle.baseline
        proof = self.oracle.proof
        assert baseline is not None and proof is not None
        self.assertEqual(baseline.base_commit, proof.base_commit)
        self.assertEqual(baseline.base_git_tree, proof.base_git_tree)
        self.assertEqual(baseline.source_git_tree, proof.source_git_tree)
        self.assertEqual(baseline.target_digest, proof.target_digest)
        self.assertEqual(baseline.verifier_digest, proof.verifier_digest)
        self.assertEqual(baseline.app_runtime, proof.app_runtime)
        self.assertNotEqual(baseline.run_id, proof.run_id)
        self.assertNotEqual(baseline.attempt_id, proof.attempt_id)
        self.assertNotEqual(baseline.workspace_id, proof.workspace_id)
        self.assertNotEqual(baseline.app_container_id, proof.app_container_id)
        self.assertNotEqual(baseline.verifier_container_id, proof.verifier_container_id)

    def test_proof_state_cannot_make_a_later_broken_baseline_pass(self) -> None:
        baseline = self.after_proof_baseline.baseline
        self.assertIs(self.after_proof_baseline.outcome, Outcome.FAILED)
        self.assertIsNotNone(baseline, self.after_proof_baseline.message)
        assert baseline is not None
        self.assertTrue(baseline.readiness.succeeded)
        self.assertIs(baseline.acceptance_probe.outcome, Outcome.FAILED)
        self.assertNotEqual(baseline.workspace_id, self.oracle.proof.workspace_id)

    def test_exact_cleanup_left_no_m1_container_residue(self) -> None:
        docker = shutil.which("docker")
        self.assertIsNotNone(docker)
        assert docker is not None
        completed = subprocess.run(
            (
                docker,
                "container",
                "ls",
                "--all",
                "--quiet",
                "--filter",
                "label=firstrun.owner=m1-controlled-local",
            ),
            cwd=Path(docker).resolve().parent,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            shell=False,
            timeout=20,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
