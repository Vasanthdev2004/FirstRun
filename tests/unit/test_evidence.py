from __future__ import annotations

import unittest
from uuid import uuid4

from pydantic import ValidationError

from firstrun.domain.evidence import (
    AcceptanceProbeEvidence,
    CleanupEvidence,
    CommandEvidence,
    ContentDigest,
    ControllerBinding,
    ControllerObservation,
    EvidenceBindingError,
    FreshStateEvidence,
    ReadinessEvidence,
    RunEvidence,
    RuntimeImageEvidence,
    build_run_evidence,
)
from firstrun.domain.outcomes import Outcome


def digest(character: str) -> ContentDigest:
    return ContentDigest(f"sha256:{character * 64}")


def binding() -> ControllerBinding:
    return ControllerBinding(run_id=uuid4(), attempt_id=uuid4(), workspace_id=uuid4())


def passing_observation(authority: ControllerBinding) -> ControllerObservation:
    runtime = RuntimeImageEvidence(
        requested_reference="node:22-bookworm-slim",
        resolved_reference=f"node@{digest('a')}",
        repository_digest=digest("a"),
        image_id=digest("b"),
        platform="linux/amd64",
    )
    verifier_runtime = RuntimeImageEvidence(
        requested_reference="node:22-bookworm-slim",
        resolved_reference=f"node@{digest('a')}",
        repository_digest=digest("a"),
        image_id=digest("b"),
        platform="linux/amd64",
    )
    return ControllerObservation(
        binding=authority,
        phase="baseline",
        base_commit="1" * 40,
        base_git_tree="2" * 40,
        source_git_tree="3" * 40,
        base_tree_digest=digest("1"),
        base_archive_digest=digest("2"),
        candidate_digest=None,
        candidate_tree_digest=None,
        target_reference=".firstrun/target.json",
        target_digest=digest("3"),
        observed_target_digest=digest("3"),
        recipe_reference=".firstrun/recipe.json",
        recipe_digest=digest("4"),
        executed_recipe_digest=digest("4"),
        readme_reference="README.md",
        readme_digest=digest("5"),
        rendered_readme_digest=digest("5"),
        verifier_id="notes-create-read-v1",
        verifier_digest=digest("6"),
        observed_verifier_digest=digest("6"),
        policy_revision="m1-policy-v1",
        policy_digest=digest("7"),
        observed_policy_digest=digest("7"),
        app_runtime=runtime,
        verifier_runtime=verifier_runtime,
        app_container_id="c" * 64,
        verifier_container_id="d" * 64,
        fresh_state=FreshStateEvidence(
            workspace_created_for_attempt=True,
            mutable_state_reused=False,
            shared_mutable_resource_ids=(),
            only_immutable_image_layers_reused=True,
        ),
        policy_authorized=True,
        commands=(
            CommandEvidence(
                step_id="install",
                kind="foreground",
                argv=("npm", "ci", "--offline", "--no-audit", "--no-fund"),
                cwd=".",
                timeout_seconds=60,
                outcome=Outcome.PASSED,
                exit_code=0,
            ),
            CommandEvidence(
                step_id="start",
                kind="start",
                argv=("npm", "run", "dev"),
                cwd=".",
                timeout_seconds=60,
                outcome=Outcome.PASSED,
                exit_code=None,
            ),
        ),
        readiness=ReadinessEvidence(
            path="/health",
            expected_status=200,
            observed_status=200,
            timeout_seconds=30,
            attempts=2,
            outcome=Outcome.PASSED,
        ),
        acceptance_probe=AcceptanceProbeEvidence(
            verifier_id="notes-create-read-v1",
            verifier_digest=digest("6"),
            timeout_seconds=15,
            outcome=Outcome.PASSED,
            nonce_digest=digest("8"),
            required_checks=("create_note", "read_back_same_note"),
            passed_checks=("create_note", "read_back_same_note"),
        ),
        cleanup=CleanupEvidence(
            attempted=True,
            app_container_removed=True,
            verifier_container_removed=True,
            workspace_removed=True,
            run_owned_resources_only=True,
            worker_quarantined=False,
        ),
        outcome=Outcome.PASSED,
    )


class ContentDigestTests(unittest.TestCase):
    def test_digest_is_strict_lowercase_sha256(self) -> None:
        self.assertEqual(str(digest("a")), "sha256:" + "a" * 64)
        for invalid in (
            "a" * 64,
            "sha256:" + "a" * 63,
            "sha256:" + "A" * 64,
            "sha1:" + "a" * 64,
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                ContentDigest(invalid)

    def test_digest_is_frozen(self) -> None:
        value = digest("a")
        with self.assertRaises(ValidationError):
            value.root = "sha256:" + "b" * 64


class RunEvidenceTests(unittest.TestCase):
    def test_complete_controller_observation_satisfies_verified_predicate(self) -> None:
        authority = binding()
        evidence = build_run_evidence(
            binding=authority,
            observation=passing_observation(authority),
        )

        self.assertTrue(evidence.verified)
        self.assertNotIn("verified", RunEvidence.model_fields)
        self.assertEqual(evidence.evidence_source, "trusted_local_controller")
        self.assertEqual(evidence.run_id, authority.run_id)

    def test_verified_is_derived_from_every_required_observation(self) -> None:
        authority = binding()
        original = passing_observation(authority)
        failed_command = CommandEvidence(
            step_id="install",
            kind="foreground",
            argv=("npm", "ci", "--offline", "--no-audit", "--no-fund"),
            cwd=".",
            timeout_seconds=60,
            outcome=Outcome.FAILED,
            exit_code=1,
        )
        incomplete_cleanup = original.cleanup.model_copy(
            update={"workspace_removed": False, "worker_quarantined": True}
        )
        changes = (
            {"rendered_readme_digest": digest("9")},
            {"observed_target_digest": digest("9")},
            {"executed_recipe_digest": digest("9")},
            {"observed_verifier_digest": digest("9")},
            {"observed_policy_digest": digest("9")},
            {"policy_authorized": False},
            {"commands": (failed_command, original.commands[1])},
            {"cleanup": incomplete_cleanup},
        )

        for update in changes:
            with self.subTest(update=next(iter(update))):
                observation = original.model_copy(update=update)
                evidence = build_run_evidence(binding=authority, observation=observation)
                self.assertFalse(evidence.verified)

    def test_proof_with_exact_candidate_can_satisfy_verified_predicate(self) -> None:
        authority = binding()
        observation = passing_observation(authority).model_copy(
            update={
                "phase": "proof",
                "candidate_digest": digest("9"),
                "candidate_tree_digest": digest("a"),
            }
        )

        evidence = build_run_evidence(binding=authority, observation=observation)

        self.assertTrue(evidence.verified)
        self.assertEqual(evidence.candidate_digest, digest("9"))
        self.assertEqual(evidence.candidate_tree_digest, digest("a"))

    def test_missing_reference_cannot_be_exported_or_claimed_verified(self) -> None:
        authority = binding()
        evidence = build_run_evidence(
            binding=authority,
            observation=passing_observation(authority),
        )
        payload = evidence.model_dump(mode="python")
        del payload["target_digest"]

        with self.assertRaises(ValidationError):
            RunEvidence.model_validate(payload)

        payload = evidence.model_dump(mode="python")
        payload["verified"] = True
        with self.assertRaises(ValidationError):
            RunEvidence.model_validate(payload)

    def test_fake_or_stale_attempt_binding_is_rejected(self) -> None:
        observed_binding = binding()
        mismatched_bindings = (
            ControllerBinding(
                run_id=observed_binding.run_id,
                attempt_id=uuid4(),
                workspace_id=observed_binding.workspace_id,
            ),
            ControllerBinding(
                run_id=uuid4(),
                attempt_id=observed_binding.attempt_id,
                workspace_id=observed_binding.workspace_id,
            ),
        )

        for expected_binding in mismatched_bindings:
            with self.subTest(expected_binding=expected_binding), self.assertRaises(
                EvidenceBindingError
            ):
                build_run_evidence(
                    binding=expected_binding,
                    observation=passing_observation(observed_binding),
                )

    def test_plain_payload_is_not_a_controller_observation(self) -> None:
        authority = binding()
        payload = passing_observation(authority).model_dump(mode="python")

        with self.assertRaises(TypeError):
            build_run_evidence(binding=authority, observation=payload)  # type: ignore[arg-type]

    def test_proof_requires_candidate_digest(self) -> None:
        authority = binding()
        payload = passing_observation(authority).model_dump(mode="python")
        payload["phase"] = "proof"

        with self.assertRaises(ValidationError):
            ControllerObservation.model_validate(payload)

    def test_identifiers_and_container_ids_must_be_distinct(self) -> None:
        identifier = uuid4()
        with self.assertRaises(ValidationError):
            ControllerBinding(
                run_id=identifier,
                attempt_id=identifier,
                workspace_id=uuid4(),
            )

        authority = binding()
        payload = passing_observation(authority).model_dump(mode="python")
        payload["verifier_container_id"] = payload["app_container_id"]
        with self.assertRaises(ValidationError):
            ControllerObservation.model_validate(payload)

    def test_logs_are_bounded_and_models_forbid_coercion_and_extras(self) -> None:
        with self.assertRaises(ValidationError):
            CommandEvidence(
                step_id="install",
                kind="foreground",
                argv=("npm", "ci"),
                cwd=".",
                timeout_seconds="60",  # type: ignore[arg-type]
                outcome=Outcome.PASSED,
                exit_code=0,
            )
        with self.assertRaises(ValidationError):
            CommandEvidence(
                step_id="install",
                kind="foreground",
                argv=("npm", "ci"),
                cwd=".",
                timeout_seconds=60,
                outcome=Outcome.PASSED,
                exit_code=0,
                unexpected=True,  # type: ignore[call-arg]
            )

        authority = binding()
        payload = passing_observation(authority).model_dump(mode="python")
        payload["sanitized_controller_log_tail"] = "x" * 16_385
        with self.assertRaises(ValidationError):
            ControllerObservation.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
