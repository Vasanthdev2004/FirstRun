"""M1 orchestration for the single approved local fixture lane."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from firstrun.domain.contracts import ContractFileError, Recipe, Target, load_target
from firstrun.domain.evidence import RunEvidence
from firstrun.domain.outcomes import Outcome
from firstrun.verification.policy import (
    CONTROLLED_NODE_FIXTURE_POLICY,
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
        }


def verify_local(repo: Path, target_path: Path) -> LocalVerificationResult:
    """Run the committed recipe as a clean baseline in the Docker sandbox."""

    prepared = _prepare_inputs(repo, target_path)
    if isinstance(prepared, LocalVerificationResult):
        return prepared
    snapshot, target, baseline_recipe = prepared
    try:
        runtime = prepare_docker_runtime(target)
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
        runtime = prepare_docker_runtime(target)
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
    if baseline_phase.outcome is not Outcome.FAILED or baseline_phase.evidence is None:
        return LocalVerificationResult(
            outcome=baseline_phase.outcome,
            source_revision=snapshot.base_commit,
            baseline=baseline_phase.evidence,
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
        return LocalVerificationResult(
            outcome=proof_phase.outcome,
            source_revision=snapshot.base_commit,
            baseline=baseline_phase.evidence,
            proof=proof,
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
            candidate_digest=candidate.patch_digest,
            candidate_tree_digest=candidate.candidate_tree_digest,
            message="baseline and proof did not have independent mutable identities",
        )
    if baseline_evidence.target_digest != proof.target_digest:
        return LocalVerificationResult(
            outcome=Outcome.POLICY_BLOCKED,
            source_revision=snapshot.base_commit,
            baseline=baseline_evidence,
            proof=proof,
            candidate_digest=candidate.patch_digest,
            candidate_tree_digest=candidate.candidate_tree_digest,
            message="proof target differs from the immutable baseline target",
        )
    return LocalVerificationResult(
        outcome=Outcome.PASSED,
        source_revision=snapshot.base_commit,
        baseline=baseline_evidence,
        proof=proof,
        candidate_digest=candidate.patch_digest,
        candidate_tree_digest=candidate.candidate_tree_digest,
        message="broken baseline reproduced and exact candidate passed an independent fresh proof",
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
        _assert_regular_plain_file(requested_target)
        snapshot = capture_approved_source(requested_repo, approved_repo=approved_repo)
        target = load_target(requested_target)
        checked_out_target = _read_exact_regular_file(requested_target)
        captured_target = snapshot.content(CONTROLLED_NODE_FIXTURE_POLICY.target_path)
        if checked_out_target != captured_target:
            raise SourcePolicyError("requested target bytes differ from the captured Git target")
        baseline_recipe = Recipe.model_validate(
            _load_unique_json(snapshot.content(CONTROLLED_NODE_FIXTURE_POLICY.recipe_path))
        )
        captured_target_model = Target.model_validate(_load_unique_json(captured_target))
        if target != captured_target_model:
            raise SourcePolicyError("requested target does not match the captured target model")
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
    except (ContractFileError, ValidationError, SourcePolicyError, UnicodeError, ValueError) as exc:
        return _early_result(Outcome.POLICY_BLOCKED, snapshot, str(exc))
    except (OSError, SourceInfrastructureError) as exc:
        return _early_result(Outcome.INFRASTRUCTURE_ERROR, snapshot, str(exc))


def _from_phase(
    snapshot: SourceSnapshot, phase: DockerPhaseResult
) -> LocalVerificationResult:
    return LocalVerificationResult(
        outcome=phase.outcome,
        source_revision=snapshot.base_commit,
        baseline=phase.evidence,
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


def _assert_regular_plain_file(path: Path) -> None:
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise SourcePolicyError(f"target file could not be inspected: {path}") from exc
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(before.st_mode) or getattr(before, "st_file_attributes", 0) & reparse:
        raise SourcePolicyError("target file must not be a link or reparse point")
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise SourcePolicyError("target file must be one ordinary regular file")


def _read_exact_regular_file(path: Path, *, max_bytes: int = 1_048_576) -> bytes:
    _assert_regular_plain_file(path)
    content = path.read_bytes()
    if len(content) > max_bytes:
        raise SourcePolicyError("trusted input file exceeds its byte limit")
    return content


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


__all__ = ["LocalVerificationResult", "verify_known_oracle", "verify_local"]
