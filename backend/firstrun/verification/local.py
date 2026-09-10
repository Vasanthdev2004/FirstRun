"""M1 orchestration for the single approved local fixture lane."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from firstrun.domain.contracts import Recipe, Target
from firstrun.domain.evidence import AttemptEvidence, ContentDigest, RunEvidence
from firstrun.domain.outcomes import Outcome
from firstrun.verification.policy import (
    CONTROLLED_NODE_FIXTURE_POLICY,
    CONTROLLED_NODE_FIXTURE_POLICY_DIGEST,
    classify_target_tuple,
)
from firstrun.verification.readme import check_block_bytes, replace_block_bytes
from firstrun.verification.source import (
    SourceInfrastructureError,
    SourcePolicyError,
    SourceSnapshot,
    build_candidate_patch,
    capture_approved_source,
    default_approved_repo_path,
    read_committed_controller_file,
)
from firstrun.worker.docker import (
    DockerPhaseResult,
    WorkerProblem,
    prepare_docker_runtime,
    run_docker_phase,
)


@dataclass(frozen=True)
class LocalVerificationResult:
    outcome: Outcome
    source_revision: str | None
    baseline: RunEvidence | None
    proof: RunEvidence | None = None
    baseline_attempt: AttemptEvidence | None = None
    proof_attempt: AttemptEvidence | None = None
    candidate_digest: str | None = None
    candidate_tree_digest: str | None = None
    message: str | None = None
    schema_version: int = 1

    @property
    def passed(self) -> bool:
        return self.outcome is Outcome.PASSED

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "outcome": self.outcome.value,
            "source_revision": self.source_revision,
            "candidate_digest": self.candidate_digest,
            "candidate_tree_digest": self.candidate_tree_digest,
            "message": self.message,
            "baseline": (
                self.baseline.model_dump(mode="json") if self.baseline is not None else None
            ),
            "proof": self.proof.model_dump(mode="json") if self.proof is not None else None,
            "baseline_attempt": (
                self.baseline_attempt.model_dump(mode="json")
                if self.baseline_attempt is not None
                else None
            ),
            "proof_attempt": (
                self.proof_attempt.model_dump(mode="json")
                if self.proof_attempt is not None
                else None
            ),
        }


def verify_local(repo: Path, target_path: Path) -> LocalVerificationResult:
    """Run the committed recipe as a clean baseline in the Docker sandbox."""

    prepared = _prepare_inputs(repo, target_path)
    if isinstance(prepared, LocalVerificationResult):
        return prepared
    snapshot, target, baseline_recipe = prepared
    try:
        runtime = prepare_docker_runtime(
            target, controller_repository_root=snapshot.repository_root
        )
    except WorkerProblem as exc:
        return _early_result(exc.outcome, snapshot, str(exc))
    baseline = run_docker_phase(
        runtime,
        snapshot,
        target,
        baseline_recipe,
        phase="baseline",
    )
    return _from_phase(snapshot, baseline)


def verify_known_oracle(
    repo: Path,
    target_path: Path,
) -> LocalVerificationResult:
    """Acceptance harness: prove the known candidate after a broken baseline.

    This function is intentionally not exposed as an end-user repair command.  M2
    will propose candidates through Strands; M1 uses a trusted fixed candidate only
    to prove the engine's freshness and acceptance boundaries.
    """

    prepared = _prepare_inputs(repo, target_path)
    if isinstance(prepared, LocalVerificationResult):
        return prepared
    snapshot, target, baseline_recipe = prepared
    try:
        fixed_recipe_bytes = read_committed_controller_file(
            snapshot, "examples/recipe.fixed.json"
        )
        fixed_recipe = Recipe.model_validate(_load_unique_json(fixed_recipe_bytes))
        original_readme = snapshot.content(CONTROLLED_NODE_FIXTURE_POLICY.readme_path)
        fixed_readme = replace_block_bytes(original_readme, fixed_recipe)
        candidate = build_candidate_patch(
            snapshot,
            {
                CONTROLLED_NODE_FIXTURE_POLICY.recipe_path: fixed_recipe_bytes,
                CONTROLLED_NODE_FIXTURE_POLICY.readme_path: fixed_readme,
            },
        )
        runtime = prepare_docker_runtime(
            target, controller_repository_root=snapshot.repository_root
        )
    except (ContractFileError, ValidationError, SourcePolicyError) as exc:
        return _early_result(Outcome.POLICY_BLOCKED, snapshot, str(exc))
    except (OSError, SourceInfrastructureError) as exc:
        return _early_result(Outcome.INFRASTRUCTURE_ERROR, snapshot, str(exc))
    except WorkerProblem as exc:
        return _early_result(exc.outcome, snapshot, str(exc))

    baseline_phase = run_docker_phase(
        runtime,
        snapshot,
        target,
        baseline_recipe,
        phase="baseline",
    )
    if not _is_expected_known_broken_baseline(baseline_phase):
        inconsistent_result = baseline_phase.outcome in {Outcome.PASSED, Outcome.FAILED}
        return LocalVerificationResult(
            outcome=(
                Outcome.INFRASTRUCTURE_ERROR
                if inconsistent_result
                else baseline_phase.outcome
            ),
            source_revision=snapshot.base_commit,
            baseline=baseline_phase.evidence,
            baseline_attempt=baseline_phase.attempt,
            candidate_digest=candidate.patch_digest,
            candidate_tree_digest=candidate.candidate_tree_digest,
            message=(
                baseline_phase.error
                or "known broken baseline did not produce the required functional failure"
            ),
        )

    proof_phase = run_docker_phase(
        runtime,
        snapshot,
        target,
        fixed_recipe,
        phase="proof",
        candidate=candidate,
    )
    proof = proof_phase.evidence
    if proof_phase.outcome is not Outcome.PASSED or proof is None or not proof.verified:
        inconsistent_pass = proof_phase.outcome is Outcome.PASSED
        return LocalVerificationResult(
            outcome=(
                Outcome.INFRASTRUCTURE_ERROR
                if inconsistent_pass
                else proof_phase.outcome
            ),
            source_revision=snapshot.base_commit,
            baseline=baseline_phase.evidence,
            proof=proof,
            baseline_attempt=baseline_phase.attempt,
            proof_attempt=proof_phase.attempt,
            candidate_digest=candidate.patch_digest,
            candidate_tree_digest=candidate.candidate_tree_digest,
            message=proof_phase.error or "fresh proof did not satisfy the verified predicate",
        )

    baseline_evidence = baseline_phase.evidence
    assert baseline_evidence is not None
    if (
        baseline_evidence.run_id == proof.run_id
        or baseline_evidence.attempt_id == proof.attempt_id
        or baseline_evidence.workspace_id == proof.workspace_id
        or baseline_evidence.app_container_id == proof.app_container_id
        or baseline_evidence.verifier_container_id == proof.verifier_container_id
    ):
        return LocalVerificationResult(
            outcome=Outcome.INFRASTRUCTURE_ERROR,
            source_revision=snapshot.base_commit,
            baseline=baseline_evidence,
            proof=proof,
            baseline_attempt=baseline_phase.attempt,
            proof_attempt=proof_phase.attempt,
            candidate_digest=candidate.patch_digest,
            candidate_tree_digest=candidate.candidate_tree_digest,
            message="baseline and proof did not have independent mutable identities",
        )
    frozen_baseline = (
        baseline_evidence.base_commit,
        baseline_evidence.base_git_tree,
        baseline_evidence.source_git_tree,
        baseline_evidence.base_tree_digest,
        baseline_evidence.base_archive_digest,
        baseline_evidence.target_reference,
        baseline_evidence.target_digest,
        baseline_evidence.verifier_id,
        baseline_evidence.verifier_digest,
        baseline_evidence.policy_revision,
        baseline_evidence.policy_digest,
        baseline_evidence.app_runtime,
        baseline_evidence.verifier_runtime,
    )
    frozen_proof = (
        proof.base_commit,
        proof.base_git_tree,
        proof.source_git_tree,
        proof.base_tree_digest,
        proof.base_archive_digest,
        proof.target_reference,
        proof.target_digest,
        proof.verifier_id,
        proof.verifier_digest,
        proof.policy_revision,
        proof.policy_digest,
        proof.app_runtime,
        proof.verifier_runtime,
    )
    candidate_matches = (
        str(proof.candidate_digest) == candidate.patch_digest
        and str(proof.candidate_tree_digest) == candidate.candidate_tree_digest
    )
    if frozen_baseline != frozen_proof or not candidate_matches:
        return LocalVerificationResult(
            outcome=Outcome.INFRASTRUCTURE_ERROR,
            source_revision=snapshot.base_commit,
            baseline=baseline_evidence,
            proof=proof,
            baseline_attempt=baseline_phase.attempt,
            proof_attempt=proof_phase.attempt,
            candidate_digest=candidate.patch_digest,
            candidate_tree_digest=candidate.candidate_tree_digest,
            message="proof evidence differs from the frozen case or exact candidate",
        )
    return LocalVerificationResult(
        outcome=Outcome.PASSED,
        source_revision=snapshot.base_commit,
        baseline=baseline_evidence,
        proof=proof,
        baseline_attempt=baseline_phase.attempt,
        proof_attempt=proof_phase.attempt,
        candidate_digest=candidate.patch_digest,
        candidate_tree_digest=candidate.candidate_tree_digest,
        message="broken baseline reproduced and exact candidate passed an independent fresh proof",
    )


def _is_expected_known_broken_baseline(phase: DockerPhaseResult) -> bool:
    """Require a complete, trusted functional failure before attempting proof."""

    evidence = phase.evidence
    return bool(
        phase.outcome is Outcome.FAILED
        and evidence is not None
        and evidence.phase == "baseline"
        and evidence.outcome is Outcome.FAILED
        and not evidence.sanitized_errors
        and evidence.policy_authorized
        and evidence.fresh_state.satisfied
        and evidence.target_digest == evidence.observed_target_digest
        and evidence.recipe_digest == evidence.executed_recipe_digest
        and evidence.readme_digest == evidence.rendered_readme_digest
        and evidence.verifier_digest == evidence.observed_verifier_digest
        and evidence.policy_digest == evidence.observed_policy_digest
        and all(command.succeeded for command in evidence.commands)
        and evidence.readiness.succeeded
        and evidence.acceptance_probe.outcome is Outcome.FAILED
        and not evidence.acceptance_probe.succeeded
        and evidence.acceptance_probe.verifier_id == evidence.verifier_id
        and evidence.acceptance_probe.verifier_digest == evidence.verifier_digest
        and evidence.cleanup.app_container_created
        and evidence.cleanup.verifier_container_created
        and evidence.cleanup.workspace_created
        and evidence.cleanup.succeeded
    )


def _prepare_inputs(
    repo: Path, target_path: Path
) -> tuple[SourceSnapshot, Target, Recipe] | LocalVerificationResult:
    snapshot: SourceSnapshot | None = None
    try:
        approved_repo = default_approved_repo_path().absolute()
        requested_repo = repo.absolute()
        expected_target = requested_repo / CONTROLLED_NODE_FIXTURE_POLICY.target_path
        requested_target = target_path.absolute()
        if requested_target != expected_target:
            raise SourcePolicyError(
                "--target must name <approved-repo>/.firstrun/target.json exactly"
            )
        snapshot = capture_approved_source(requested_repo, approved_repo=approved_repo)
        captured_target = snapshot.content(CONTROLLED_NODE_FIXTURE_POLICY.target_path)
        target = Target.model_validate(_load_unique_json(captured_target))
        baseline_recipe = Recipe.model_validate(
            _load_unique_json(snapshot.content(CONTROLLED_NODE_FIXTURE_POLICY.recipe_path))
        )
        classification = classify_target_tuple(
            image_reference=target.runtime.image_ref,
            platform=target.runtime.platform,
            verifier_id=target.acceptance.verifier_id,
        )
        if classification is Outcome.UNSUPPORTED:
            return _early_result(Outcome.UNSUPPORTED, snapshot, "target is schema-valid but unsupported")
        readme = snapshot.content(CONTROLLED_NODE_FIXTURE_POLICY.readme_path)
        if not check_block_bytes(readme, baseline_recipe):
            raise SourcePolicyError(
                "README managed block differs from the committed recipe; execution was blocked"
            )
        return snapshot, target, baseline_recipe
    except (ValidationError, SourcePolicyError, UnicodeError, ValueError) as exc:
        return _early_result(Outcome.POLICY_BLOCKED, snapshot, str(exc))
    except (OSError, SourceInfrastructureError) as exc:
        return _early_result(Outcome.INFRASTRUCTURE_ERROR, snapshot, str(exc))


def _from_phase(
    snapshot: SourceSnapshot, phase: DockerPhaseResult
) -> LocalVerificationResult:
    if phase.outcome is Outcome.PASSED and (
        phase.evidence is None or not phase.evidence.verified
    ):
        return LocalVerificationResult(
            outcome=Outcome.INFRASTRUCTURE_ERROR,
            source_revision=snapshot.base_commit,
            baseline=phase.evidence,
            baseline_attempt=phase.attempt,
            message="worker reported pass without complete verified evidence",
        )
    return LocalVerificationResult(
        outcome=phase.outcome,
        source_revision=snapshot.base_commit,
        baseline=phase.evidence,
        baseline_attempt=phase.attempt,
        message=phase.error,
    )


def _early_result(
    outcome: Outcome,
    snapshot: SourceSnapshot | None,
    message: str,
) -> LocalVerificationResult:
    return LocalVerificationResult(
        outcome=outcome,
        source_revision=snapshot.base_commit if snapshot else None,
        baseline=None,
        message=message[:4096],
    )


def _load_unique_json(content: bytes) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise SourcePolicyError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(
        content.decode("utf-8"),
        object_pairs_hook=unique,
        parse_constant=lambda value: (_ for _ in ()).throw(
            SourcePolicyError(f"unsupported JSON constant: {value}")
        ),
    )


def prepare_local_case_inputs(
    repo: Path, target_path: Path
) -> tuple[SourceSnapshot, Target, Recipe] | LocalVerificationResult:
    """Expose the M1 input gate for the trusted M2 controller."""

    return _prepare_inputs(repo, target_path)


def is_complete_functional_baseline_failure(phase: DockerPhaseResult) -> bool:
    """Return whether a failed baseline is complete enough for investigation."""

    return _is_expected_known_broken_baseline(phase)


def is_safe_repair_baseline_failure(
    phase: DockerPhaseResult,
    snapshot: SourceSnapshot,
    target: Target,
    recipe: Recipe,
) -> bool:
    """Accept only controller-bound repository failures as M2 evidence.

    Unlike the M1 known-oracle predicate, this also permits an intact partial
    attempt when a recipe command or managed start fails before the verifier can
    run. Infrastructure, timeout, policy, and cleanup failures remain terminal.
    """

    observed = phase.evidence or phase.attempt
    if phase.outcome is not Outcome.FAILED or observed is None:
        return False
    policy = CONTROLLED_NODE_FIXTURE_POLICY
    common = bool(
        observed.phase == "baseline"
        and observed.outcome is Outcome.FAILED
        and not observed.sanitized_errors
        and observed.candidate_digest is None
        and observed.candidate_tree_digest is None
        and observed.base_commit == snapshot.base_commit
        and observed.base_git_tree == snapshot.base_tree
        and observed.source_git_tree == snapshot.source_tree
        and observed.base_tree_digest == ContentDigest(snapshot.content_tree_digest)
        and observed.base_archive_digest == ContentDigest(snapshot.archive_digest)
        and observed.target_reference == policy.target_path
        and observed.target_digest
        == ContentDigest.from_bytes(snapshot.content(policy.target_path))
        and observed.recipe_reference == policy.recipe_path
        and observed.recipe_digest
        == ContentDigest.from_bytes(snapshot.content(policy.recipe_path))
        and observed.readme_reference == policy.readme_path
        and observed.readme_digest
        == ContentDigest.from_bytes(snapshot.content(policy.readme_path))
        and observed.verifier_id == target.acceptance.verifier_id
        and observed.policy_revision == policy.revision
        and observed.policy_digest == CONTROLLED_NODE_FIXTURE_POLICY_DIGEST
        and observed.app_runtime.requested_reference == target.runtime.image_ref
        and observed.app_runtime.platform == target.runtime.platform
        and observed.app_runtime == observed.verifier_runtime
        and observed.app_container_id is not None
        and (
            observed.fresh_state.workspace_marker_digest is not None
            if isinstance(observed, RunEvidence)
            else observed.workspace_marker_digest is not None
        )
        and observed.cleanup.app_container_created
        and observed.cleanup.workspace_created
        and observed.cleanup.succeeded
        and bool(observed.commands)
    )
    if not common:
        return False
    if isinstance(observed, RunEvidence):
        return bool(
            observed.policy_authorized
            and observed.fresh_state.satisfied
            and observed.target_digest == observed.observed_target_digest
            and observed.recipe_digest == observed.executed_recipe_digest
            and observed.readme_digest == observed.rendered_readme_digest
            and observed.verifier_digest == observed.observed_verifier_digest
            and observed.policy_digest == observed.observed_policy_digest
            and (
                any(not command.succeeded for command in observed.commands)
                or not observed.readiness.succeeded
                or not observed.acceptance_probe.succeeded
            )
        )
    return any(not command.succeeded for command in observed.commands)


__all__ = [
    "LocalVerificationResult",
    "is_complete_functional_baseline_failure",
    "is_safe_repair_baseline_failure",
    "prepare_local_case_inputs",
    "verify_known_oracle",
    "verify_local",
]
