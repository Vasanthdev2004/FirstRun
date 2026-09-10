"""Trusted M2 baseline, investigation, candidate, and proof orchestration.

The model can inspect bounded evidence and propose a replacement recipe.  It
cannot mutate the case or declare success: this controller renders README,
constructs the exact candidate, destroys investigation state, and launches the
independent proof.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sys
import time
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Literal

from firstrun.agent.capabilities import CapabilityRecord, RepairCapabilityBroker
from firstrun.agent.strands import (
    LiveRepairAgentObservation,
    run_live_repair_agent,
)
from firstrun.domain.contracts import Recipe, Target
from firstrun.domain.evidence import (
    AttemptEvidence,
    ContentDigest,
    ControllerBinding,
    RunEvidence,
)
from firstrun.domain.outcomes import Outcome, exit_code_for
from firstrun.domain.repair import (
    AgentAttemptResult,
    AgentRunEvidence,
    Blocked,
    NeedsInput,
    RepairOutcomeValue,
    RepairProposal,
    RepairProviderConfig,
    ToolCallEvidence,
)
from firstrun.verification.local import (
    LocalVerificationResult,
    is_safe_repair_baseline_failure,
    prepare_local_case_inputs,
)
from firstrun.verification.policy import (
    CONTROLLED_NODE_FIXTURE_POLICY,
    classify_target_tuple,
)
from firstrun.verification.readme import check_block_bytes, replace_block_bytes
from firstrun.verification.source import (
    MAX_FILE_BYTES,
    MAX_SOURCE_BYTES,
    MAX_SOURCE_FILES,
    CandidatePatch,
    SourcePolicyError,
    SourceSnapshot,
    build_candidate_patch,
    content_tree_digest,
)
from firstrun.worker.docker import (
    DockerPhaseResult,
    PreparedDockerRuntime,
    WorkerProblem,
    _DeadlineDockerCliRunner,
    _assert_recipe_authorized,
    prepare_docker_runtime,
    run_docker_phase,
)
from firstrun.worker.investigation import (
    DockerInvestigationSession,
    InvestigationEvidence,
)


NEEDS_INPUT_EXIT_CODE = 16
_CLEANUP_GRACE_SECONDS = 30
_SOURCE_TOOL_NAMES = frozenset({"read_source_file", "search_source"})
_GIT_OBJECT_ID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


class RepairCaseState(StrEnum):
    """Product state, kept separate from authoritative run outcomes."""

    VERIFIED = "verified"
    REPAIR_READY = "repair_ready"
    NEEDS_INPUT = "needs_input"
    UNRESOLVED = "unresolved"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class ProofAttemptRecord:
    attempt_number: int
    outcome: Outcome
    binding: ControllerBinding
    candidate_digest: str
    candidate_tree_digest: str
    evidence: RunEvidence | None = None
    partial_evidence: AttemptEvidence | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_number": self.attempt_number,
            "outcome": self.outcome.value,
            "binding": self.binding.model_dump(mode="json"),
            "candidate_digest": self.candidate_digest,
            "candidate_tree_digest": self.candidate_tree_digest,
            "evidence": (
                self.evidence.model_dump(mode="json")
                if self.evidence is not None
                else None
            ),
            "partial_evidence": (
                self.partial_evidence.model_dump(mode="json")
                if self.partial_evidence is not None
                else None
            ),
            "error": self.error,
        }


@dataclass(frozen=True)
class RepairLocalResult:
    """Controller-owned result of one bounded local repair case."""

    state: RepairCaseState
    outcome: Outcome
    source_revision: str | None
    baseline: RunEvidence | None = None
    baseline_attempt: AttemptEvidence | None = None
    agent: AgentRunEvidence | None = None
    investigations: tuple[InvestigationEvidence, ...] = ()
    proofs: tuple[ProofAttemptRecord, ...] = ()
    candidate: CandidatePatch | None = None
    message: str = ""
    schema_version: int = 1

    @property
    def exit_code(self) -> int:
        if self.state is RepairCaseState.NEEDS_INPUT:
            return NEEDS_INPUT_EXIT_CODE
        return exit_code_for(self.outcome)

    @property
    def repair_ready(self) -> bool:
        return self.state is RepairCaseState.REPAIR_READY

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "state": self.state.value,
            "outcome": self.outcome.value,
            "exit_code": self.exit_code,
            "source_revision": self.source_revision,
            "message": self.message,
            "baseline": (
                self.baseline.model_dump(mode="json")
                if self.baseline is not None
                else None
            ),
            "baseline_attempt": (
                self.baseline_attempt.model_dump(mode="json")
                if self.baseline_attempt is not None
                else None
            ),
            "agent": (
                self.agent.model_dump(mode="json") if self.agent is not None else None
            ),
            "investigations": [
                _investigation_to_dict(value) for value in self.investigations
            ],
            "proofs": [value.to_dict() for value in self.proofs],
            "candidate": _candidate_to_dict(self.candidate),
        }


RepairAgentRunner = Callable[
    [RepairProviderConfig, RepairCapabilityBroker], LiveRepairAgentObservation
]
RepairObserver = Callable[[str, dict[str, object]], None]


def repair_local(
    repo: Path,
    provider_config: RepairProviderConfig,
) -> RepairLocalResult:
    """Run the live M2 workflow for the currently approved local fixture lane."""

    return _run_repair_local(
        repo,
        provider_config,
        agent_runner=run_live_repair_agent,
        proposer_kind="live_strands",
    )


def repair_snapshot(
    snapshot: SourceSnapshot,
    provider_config: RepairProviderConfig,
    *,
    observer: RepairObserver | None = None,
    expected_runtime_digest: str | None = None,
    expected_runtime_repository_digest: str | None = None,
) -> RepairLocalResult:
    """Repair one already-authorized, credential-free exact source snapshot.

    The GitHub ingestion boundary owns installation/repository authorization and
    construction of ``snapshot``.  This boundary independently revalidates its
    byte identities and the registered target/recipe lane; it never consults the
    local teaching fixture or a known repair oracle.
    """

    started_at = time.monotonic()
    prerequisite = _repair_prerequisite_failure(
        provider_config,
        proposer_kind="live_strands",
        started_at=started_at,
        source_revision=_snapshot_revision(snapshot),
    )
    if prerequisite is not None:
        return prerequisite
    try:
        snapshot, target, recipe = _prepare_snapshot_inputs(snapshot)
    except (SourcePolicyError, ValueError, UnicodeError) as exc:
        return RepairLocalResult(
            state=RepairCaseState.BLOCKED,
            outcome=Outcome.POLICY_BLOCKED,
            source_revision=_snapshot_revision(snapshot),
            message=_bounded_error(str(exc)),
        )
    return _run_prepared_repair(
        snapshot,
        target,
        recipe,
        provider_config,
        agent_runner=run_live_repair_agent,
        proposer_kind="live_strands",
        started_at=started_at,
        observer=observer,
        authorized_baseline_recipe=True,
        expected_runtime_digest=expected_runtime_digest,
        expected_runtime_repository_digest=expected_runtime_repository_digest,
    )


def verify_snapshot(
    snapshot: SourceSnapshot,
    *,
    observer: RepairObserver | None = None,
    expected_runtime_digest: str | None = None,
    expected_runtime_repository_digest: str | None = None,
) -> DockerPhaseResult:
    """Verify the reviewed recipe at one exact prepared revision without a model."""

    binding = _new_binding()
    try:
        snapshot, target, recipe = _prepare_snapshot_inputs(snapshot)
        runtime = prepare_docker_runtime(
            target, controller_repository_root=snapshot.repository_root
        )
        _assert_expected_runtime(
            runtime,
            expected_runtime_digest,
            expected_runtime_repository_digest,
        )
    except (SourcePolicyError, ValueError, UnicodeError) as exc:
        return DockerPhaseResult(
            Outcome.POLICY_BLOCKED, binding, None, _bounded_error(str(exc))
        )
    except WorkerProblem as exc:
        return DockerPhaseResult(
            exc.outcome, binding, None, _bounded_error(str(exc))
        )

    observer_error = _observe(
        observer,
        "verification_running",
        _running_payload("verification", binding),
    )
    if observer_error is not None:
        return DockerPhaseResult(
            Outcome.INFRASTRUCTURE_ERROR, binding, None, observer_error
        )
    phase = run_docker_phase(
        runtime,
        snapshot,
        target,
        recipe,
        phase="baseline",
        binding=binding,
        authorized_recipe=True,
    )
    observer_error = _observe(
        observer,
        "verification_completed",
        _phase_payload("verification", phase),
    )
    if observer_error is not None:
        return DockerPhaseResult(
            Outcome.INFRASTRUCTURE_ERROR,
            binding,
            phase.evidence,
            observer_error,
            phase.attempt,
        )
    if phase.outcome is Outcome.PASSED and (
        phase.evidence is None or not phase.evidence.verified
    ):
        return DockerPhaseResult(
            Outcome.INFRASTRUCTURE_ERROR,
            binding,
            phase.evidence,
            "worker reported pass without complete verified evidence",
            phase.attempt,
        )
    return phase


def _repair_local_with_test_agent(
    repo: Path,
    provider_config: RepairProviderConfig,
    *,
    agent_runner: RepairAgentRunner,
) -> RepairLocalResult:
    """Internal deterministic seam; evidence is permanently marked fake_test."""

    return _run_repair_local(
        repo,
        provider_config,
        agent_runner=agent_runner,
        proposer_kind="fake_test",
    )


def _run_repair_local(
    repo: Path,
    provider_config: RepairProviderConfig,
    *,
    agent_runner: RepairAgentRunner,
    proposer_kind: Literal["live_strands", "fake_test"],
) -> RepairLocalResult:
    """Shared controller implementation with an explicitly derived proposer class."""

    started_at = time.monotonic()
    prerequisite = _repair_prerequisite_failure(
        provider_config,
        proposer_kind=proposer_kind,
        started_at=started_at,
        source_revision=None,
    )
    if prerequisite is not None:
        return prerequisite

    repo = Path(repo)
    target_path = repo / CONTROLLED_NODE_FIXTURE_POLICY.target_path
    prepared = prepare_local_case_inputs(repo, target_path)
    if isinstance(prepared, LocalVerificationResult):
        return _from_input_failure(prepared)
    snapshot, target, baseline_recipe = prepared

    return _run_prepared_repair(
        snapshot,
        target,
        baseline_recipe,
        provider_config,
        agent_runner=agent_runner,
        proposer_kind=proposer_kind,
        started_at=started_at,
        observer=None,
        authorized_baseline_recipe=False,
        expected_runtime_digest=None,
        expected_runtime_repository_digest=None,
    )


def _run_prepared_repair(
    snapshot: SourceSnapshot,
    target: Target,
    baseline_recipe: Recipe,
    provider_config: RepairProviderConfig,
    *,
    agent_runner: RepairAgentRunner,
    proposer_kind: Literal["live_strands", "fake_test"],
    started_at: float,
    observer: RepairObserver | None,
    authorized_baseline_recipe: bool,
    expected_runtime_digest: str | None,
    expected_runtime_repository_digest: str | None,
) -> RepairLocalResult:
    """Execute the M2 loop against validated, credential-free source bytes."""

    try:
        runtime = prepare_docker_runtime(
            target, controller_repository_root=snapshot.repository_root
        )
        _assert_expected_runtime(
            runtime,
            expected_runtime_digest,
            expected_runtime_repository_digest,
        )
    except SourcePolicyError as exc:
        return RepairLocalResult(
            state=RepairCaseState.BLOCKED,
            outcome=Outcome.POLICY_BLOCKED,
            source_revision=snapshot.base_commit,
            message=_bounded_error(str(exc)),
        )
    except WorkerProblem as exc:
        return RepairLocalResult(
            state=RepairCaseState.BLOCKED,
            outcome=exc.outcome,
            source_revision=snapshot.base_commit,
            message=_bounded_error(str(exc)),
        )

    # All sandbox operations are nested under a run-wide ceiling with cleanup
    # grace. Individual phase and cleanup ceilings remain enforced by the worker.
    run_deadline = started_at + provider_config.wall_time_seconds
    runtime = replace(
        runtime,
        runner=_DeadlineDockerCliRunner(
            runtime.runner,
            run_deadline + _CLEANUP_GRACE_SECONDS,
            budget_name="phase",
        ),
    )
    baseline_binding = _new_binding()
    observer_error = _observe(
        observer,
        "baseline_running",
        _running_payload("baseline", baseline_binding),
    )
    if observer_error is not None:
        return RepairLocalResult(
            state=RepairCaseState.BLOCKED,
            outcome=Outcome.INFRASTRUCTURE_ERROR,
            source_revision=snapshot.base_commit,
            message=observer_error,
        )
    baseline_phase = run_docker_phase(
        runtime,
        snapshot,
        target,
        baseline_recipe,
        phase="baseline",
        binding=baseline_binding,
        authorized_recipe=authorized_baseline_recipe,
    )
    observer_error = _observe(
        observer,
        "baseline_completed",
        _phase_payload("baseline", baseline_phase),
    )
    if observer_error is not None:
        return RepairLocalResult(
            state=RepairCaseState.BLOCKED,
            outcome=Outcome.INFRASTRUCTURE_ERROR,
            source_revision=snapshot.base_commit,
            baseline=baseline_phase.evidence,
            baseline_attempt=baseline_phase.attempt,
            message=observer_error,
        )
    if baseline_phase.outcome is Outcome.PASSED:
        if baseline_phase.evidence is None or not baseline_phase.evidence.verified:
            return RepairLocalResult(
                state=RepairCaseState.BLOCKED,
                outcome=Outcome.INFRASTRUCTURE_ERROR,
                source_revision=snapshot.base_commit,
                baseline=baseline_phase.evidence,
                baseline_attempt=baseline_phase.attempt,
                message="worker reported baseline pass without complete verified evidence",
            )
        return RepairLocalResult(
            state=RepairCaseState.VERIFIED,
            outcome=Outcome.PASSED,
            source_revision=snapshot.base_commit,
            baseline=baseline_phase.evidence,
            baseline_attempt=baseline_phase.attempt,
            message="the committed recipe already satisfies the immutable target",
        )
    if not is_safe_repair_baseline_failure(
        baseline_phase, snapshot, target, baseline_recipe
    ):
        outcome = baseline_phase.outcome
        if outcome in {Outcome.PASSED, Outcome.FAILED}:
            outcome = Outcome.INFRASTRUCTURE_ERROR
        return RepairLocalResult(
            state=(
                RepairCaseState.UNRESOLVED
                if outcome is Outcome.FAILED
                else RepairCaseState.BLOCKED
            ),
            outcome=outcome,
            source_revision=snapshot.base_commit,
            baseline=baseline_phase.evidence,
            baseline_attempt=baseline_phase.attempt,
            message=_bounded_error(
                baseline_phase.error
                or "baseline failure was incomplete and is unsafe to investigate"
            ),
        )

    baseline = baseline_phase.evidence or baseline_phase.attempt
    assert baseline is not None
    baseline_ref = _baseline_reference(baseline)
    observations: list[LiveRepairAgentObservation] = []
    records: list[CapabilityRecord] = []
    investigations: list[InvestigationEvidence] = []
    proofs: list[ProofAttemptRecord] = []
    prior_feedback: dict[str, object] | None = None
    final_candidate: CandidatePatch | None = None
    final_decision: RepairOutcomeValue | None = None

    for attempt_number in range(1, provider_config.max_attempts + 1):
        remaining = run_deadline - time.monotonic()
        if remaining <= 0 or len(records) >= 64:
            message = "repair run exhausted its controller budget"
            agent = _agent_evidence(
                provider_config,
                proposer_kind=proposer_kind,
                observations=tuple(observations),
                records=tuple(records),
                baseline_refs=(baseline_ref,),
                status="budget_exhausted",
                result=None,
                error=message,
                elapsed_ms=_elapsed_ms(started_at),
            )
            return _result(
                state=RepairCaseState.UNRESOLVED,
                outcome=Outcome.TIMED_OUT,
                snapshot_revision=snapshot.base_commit,
                baseline_phase=baseline_phase,
                agent=agent,
                investigations=investigations,
                proofs=proofs,
                candidate=final_candidate,
                message=message,
            )

        attempt_config = _attempt_config(provider_config, remaining)
        try:
            investigation_binding = _new_binding()
            session = DockerInvestigationSession(
                runtime,
                snapshot,
                target,
                baseline_recipe,
                max_diagnostics=provider_config.max_diagnostic_commands_per_attempt,
                wall_time_seconds=max(1, min(120, int(remaining))),
                binding=investigation_binding,
            )
            broker = RepairCapabilityBroker(
                case_token=secrets.token_hex(16),
                snapshot=snapshot,
                baseline=baseline,
                diagnostic_runner=session.run_package_script,
                max_diagnostics=provider_config.max_diagnostic_commands_per_attempt,
                max_tool_calls=64 - len(records),
                agent_attempt=attempt_number,
                prior_attempt_feedback=prior_feedback,
            )
        except (SourcePolicyError, ValueError) as exc:
            message = _bounded_error(str(exc))
            agent = _agent_evidence(
                provider_config,
                proposer_kind=proposer_kind,
                observations=tuple(observations),
                records=tuple(records),
                baseline_refs=(baseline_ref,),
                status="policy_blocked",
                result=None,
                error=message,
                elapsed_ms=_elapsed_ms(started_at),
            )
            return _result(
                state=RepairCaseState.BLOCKED,
                outcome=Outcome.POLICY_BLOCKED,
                snapshot_revision=snapshot.base_commit,
                baseline_phase=baseline_phase,
                agent=agent,
                investigations=investigations,
                proofs=proofs,
                candidate=final_candidate,
                message=message,
            )

        observer_error = _observe(
            observer,
            "investigation_running",
            _running_payload(
                "investigation", investigation_binding, attempt_number=attempt_number
            ),
        )
        if observer_error is not None:
            agent = _agent_evidence(
                provider_config,
                proposer_kind=proposer_kind,
                observations=tuple(observations),
                records=tuple(records),
                baseline_refs=(baseline_ref,),
                status="infrastructure_error",
                result=None,
                error=observer_error,
                elapsed_ms=_elapsed_ms(started_at),
            )
            return _result(
                state=RepairCaseState.BLOCKED,
                outcome=Outcome.INFRASTRUCTURE_ERROR,
                snapshot_revision=snapshot.base_commit,
                baseline_phase=baseline_phase,
                agent=agent,
                investigations=investigations,
                proofs=proofs,
                candidate=final_candidate,
                message=observer_error,
            )

        try:
            session.open()
            observation = agent_runner(attempt_config, broker)
            if not isinstance(observation, LiveRepairAgentObservation):
                raise TypeError("agent runner returned an invalid observation")
        except Exception:
            observation = LiveRepairAgentObservation(
                decision=None,
                sdk_version=None,
                provider_endpoint=None,
                provider_invoked=False,
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                model_cycles=None,
                elapsed_ms=0,
                error_code="agent_runner_failed",
                message="the repair-agent runner failed",
            )
        finally:
            investigation = session.close()

        observations.append(observation)
        records.extend(broker.records)
        investigations.append(investigation)
        observer_error = _observe(
            observer,
            "investigation_completed",
            {
                "phase": "investigation",
                "attempt_number": attempt_number,
                **_investigation_to_dict(investigation),
            },
        )
        if observer_error is not None:
            agent = _agent_evidence(
                provider_config,
                proposer_kind=proposer_kind,
                observations=tuple(observations),
                records=tuple(records),
                baseline_refs=(baseline_ref,),
                status="infrastructure_error",
                result=None,
                error=observer_error,
                elapsed_ms=_elapsed_ms(started_at),
            )
            return _result(
                state=RepairCaseState.BLOCKED,
                outcome=Outcome.INFRASTRUCTURE_ERROR,
                snapshot_revision=snapshot.base_commit,
                baseline_phase=baseline_phase,
                agent=agent,
                investigations=investigations,
                proofs=proofs,
                candidate=final_candidate,
                message=observer_error,
            )
        if not investigation.proof_may_start:
            outcome = investigation.outcome
            if outcome is Outcome.PASSED:
                outcome = Outcome.INFRASTRUCTURE_ERROR
            message = (
                investigation.sanitized_errors[0]
                if investigation.sanitized_errors
                else "investigation cleanup or isolation failed"
            )
            agent = _agent_evidence(
                provider_config,
                proposer_kind=proposer_kind,
                observations=tuple(observations),
                records=tuple(records),
                baseline_refs=(baseline_ref,),
                status=_agent_status_for_outcome(outcome),
                result=None,
                error=message,
                elapsed_ms=_elapsed_ms(started_at),
            )
            return _result(
                state=RepairCaseState.BLOCKED,
                outcome=outcome,
                snapshot_revision=snapshot.base_commit,
                baseline_phase=baseline_phase,
                agent=agent,
                investigations=investigations,
                proofs=proofs,
                candidate=final_candidate,
                message=message,
            )
        if not observation.succeeded:
            status, outcome = _classify_agent_error(observation.error_code)
            message = _bounded_error(observation.message)
            agent = _agent_evidence(
                provider_config,
                proposer_kind=proposer_kind,
                observations=tuple(observations),
                records=tuple(records),
                baseline_refs=(baseline_ref,),
                status=status,
                result=None,
                error=message,
                elapsed_ms=_elapsed_ms(started_at),
            )
            return _result(
                state=RepairCaseState.BLOCKED,
                outcome=outcome,
                snapshot_revision=snapshot.base_commit,
                baseline_phase=baseline_phase,
                agent=agent,
                investigations=investigations,
                proofs=proofs,
                candidate=final_candidate,
                message=message,
            )

        if proposer_kind == "live_strands" and not _live_observations_coherent(
            tuple(observations)
        ):
            message = "live provider evidence was incomplete or changed between attempts"
            agent = _agent_evidence(
                provider_config,
                proposer_kind=proposer_kind,
                observations=tuple(observations),
                records=tuple(records),
                baseline_refs=(baseline_ref,),
                status="infrastructure_error",
                result=None,
                error=message,
                elapsed_ms=_elapsed_ms(started_at),
            )
            return _result(
                state=RepairCaseState.BLOCKED,
                outcome=Outcome.INFRASTRUCTURE_ERROR,
                snapshot_revision=snapshot.base_commit,
                baseline_phase=baseline_phase,
                agent=agent,
                investigations=investigations,
                proofs=proofs,
                candidate=final_candidate,
                message=message,
            )

        decision = observation.decision
        assert decision is not None
        final_decision = decision
        current_records = tuple(
            value for value in broker.records if value.agent_attempt == attempt_number
        )
        if not _used_required_evidence_tools(current_records):
            message = (
                "repair agent must successfully inspect baseline evidence and pinned source"
            )
            agent = _agent_evidence(
                provider_config,
                proposer_kind=proposer_kind,
                observations=tuple(observations),
                records=tuple(records),
                baseline_refs=(baseline_ref,),
                status="policy_blocked",
                result=None,
                error=message,
                elapsed_ms=_elapsed_ms(started_at),
            )
            return _result(
                state=RepairCaseState.BLOCKED,
                outcome=Outcome.POLICY_BLOCKED,
                snapshot_revision=snapshot.base_commit,
                baseline_phase=baseline_phase,
                agent=agent,
                investigations=investigations,
                proofs=proofs,
                candidate=final_candidate,
                message=message,
            )
        if not _decision_uses_available_evidence(
            decision,
            baseline_recipe=baseline_recipe,
            baseline_ref=baseline_ref,
            all_records=tuple(records),
            current_records=current_records,
        ):
            message = "repair decision cited unavailable or insufficient evidence"
            agent = _agent_evidence(
                provider_config,
                proposer_kind=proposer_kind,
                observations=tuple(observations),
                records=tuple(records),
                baseline_refs=(baseline_ref,),
                status="policy_blocked",
                result=None,
                error=message,
                elapsed_ms=_elapsed_ms(started_at),
            )
            return _result(
                state=RepairCaseState.BLOCKED,
                outcome=Outcome.POLICY_BLOCKED,
                snapshot_revision=snapshot.base_commit,
                baseline_phase=baseline_phase,
                agent=agent,
                investigations=investigations,
                proofs=proofs,
                candidate=final_candidate,
                message=message,
            )

        if isinstance(decision, NeedsInput):
            agent = _agent_evidence(
                provider_config,
                proposer_kind=proposer_kind,
                observations=tuple(observations),
                records=tuple(records),
                baseline_refs=(baseline_ref,),
                status="completed",
                result=decision,
                error=None,
                elapsed_ms=_elapsed_ms(started_at),
            )
            return _result(
                state=RepairCaseState.NEEDS_INPUT,
                outcome=Outcome.POLICY_BLOCKED,
                snapshot_revision=snapshot.base_commit,
                baseline_phase=baseline_phase,
                agent=agent,
                investigations=investigations,
                proofs=proofs,
                candidate=final_candidate,
                message=_needs_input_message(decision),
            )
        if isinstance(decision, Blocked):
            outcome = Outcome.POLICY_BLOCKED
            agent = _agent_evidence(
                provider_config,
                proposer_kind=proposer_kind,
                observations=tuple(observations),
                records=tuple(records),
                baseline_refs=(baseline_ref,),
                status="completed",
                result=decision,
                error=None,
                elapsed_ms=_elapsed_ms(started_at),
            )
            return _result(
                state=RepairCaseState.BLOCKED,
                outcome=outcome,
                snapshot_revision=snapshot.base_commit,
                baseline_phase=baseline_phase,
                agent=agent,
                investigations=investigations,
                proofs=proofs,
                candidate=final_candidate,
                message=_blocked_message(decision),
            )

        assert isinstance(decision, RepairProposal)
        try:
            candidate = _build_candidate(snapshot, decision.proposed_recipe)
        except (SourcePolicyError, UnicodeError, ValueError) as exc:
            prior_feedback = {
                "attempt": attempt_number,
                "candidate_outcome": Outcome.POLICY_BLOCKED.value,
                "error": _bounded_error(str(exc)),
            }
            if attempt_number < provider_config.max_attempts:
                continue
            message = str(prior_feedback["error"])
            agent = _agent_evidence(
                provider_config,
                proposer_kind=proposer_kind,
                observations=tuple(observations),
                records=tuple(records),
                baseline_refs=(baseline_ref,),
                status="completed",
                result=decision,
                error=None,
                elapsed_ms=_elapsed_ms(started_at),
            )
            return _result(
                state=RepairCaseState.UNRESOLVED,
                outcome=Outcome.POLICY_BLOCKED,
                snapshot_revision=snapshot.base_commit,
                baseline_phase=baseline_phase,
                agent=agent,
                investigations=investigations,
                proofs=proofs,
                candidate=final_candidate,
                message=message,
            )

        final_candidate = candidate
        if time.monotonic() >= run_deadline:
            message = "repair run exhausted its wall-time budget before proof"
            agent = _agent_evidence(
                provider_config,
                proposer_kind=proposer_kind,
                observations=tuple(observations),
                records=tuple(records),
                baseline_refs=(baseline_ref,),
                status="budget_exhausted",
                result=None,
                error=message,
                elapsed_ms=_elapsed_ms(started_at),
            )
            return _result(
                state=RepairCaseState.UNRESOLVED,
                outcome=Outcome.TIMED_OUT,
                snapshot_revision=snapshot.base_commit,
                baseline_phase=baseline_phase,
                agent=agent,
                investigations=investigations,
                proofs=proofs,
                candidate=candidate,
                message=message,
            )

        proof_binding = _new_binding()
        observer_error = _observe(
            observer,
            "proof_running",
            {
                **_running_payload(
                    "proof", proof_binding, attempt_number=attempt_number
                ),
                "candidate_digest": candidate.patch_digest,
                "candidate_tree_digest": candidate.candidate_tree_digest,
            },
        )
        if observer_error is not None:
            agent = _agent_evidence(
                provider_config,
                proposer_kind=proposer_kind,
                observations=tuple(observations),
                records=tuple(records),
                baseline_refs=(baseline_ref,),
                status="completed",
                result=decision,
                error=None,
                elapsed_ms=_elapsed_ms(started_at),
            )
            return _result(
                state=RepairCaseState.BLOCKED,
                outcome=Outcome.INFRASTRUCTURE_ERROR,
                snapshot_revision=snapshot.base_commit,
                baseline_phase=baseline_phase,
                agent=agent,
                investigations=investigations,
                proofs=proofs,
                candidate=candidate,
                message=observer_error,
            )
        proof_phase = run_docker_phase(
            runtime,
            snapshot,
            target,
            decision.proposed_recipe,
            phase="proof",
            candidate=candidate,
            binding=proof_binding,
        )
        proof_record = _proof_record(attempt_number, candidate, proof_phase)
        proofs.append(proof_record)
        observer_error = _observe(
            observer,
            "proof_completed",
            {"phase": "proof", **proof_record.to_dict()},
        )
        if observer_error is not None:
            agent = _agent_evidence(
                provider_config,
                proposer_kind=proposer_kind,
                observations=tuple(observations),
                records=tuple(records),
                baseline_refs=(baseline_ref,),
                status="completed",
                result=decision,
                error=None,
                elapsed_ms=_elapsed_ms(started_at),
            )
            return _result(
                state=RepairCaseState.BLOCKED,
                outcome=Outcome.INFRASTRUCTURE_ERROR,
                snapshot_revision=snapshot.base_commit,
                baseline_phase=baseline_phase,
                agent=agent,
                investigations=investigations,
                proofs=proofs,
                candidate=candidate,
                message=observer_error,
            )
        proof = proof_phase.evidence
        if (
            proof_phase.outcome is Outcome.PASSED
            and proof is not None
            and proof.verified
            and _case_and_candidate_match(baseline, proof, candidate)
            and _all_execution_identities_are_independent(
                baseline, tuple(investigations), tuple(proofs)
            )
        ):
            agent = _agent_evidence(
                provider_config,
                proposer_kind=proposer_kind,
                observations=tuple(observations),
                records=tuple(records),
                baseline_refs=(baseline_ref,),
                status="completed",
                result=decision,
                error=None,
                elapsed_ms=_elapsed_ms(started_at),
            )
            if proposer_kind == "live_strands" and not agent.milestone_eligible:
                return _result(
                    state=RepairCaseState.BLOCKED,
                    outcome=Outcome.INFRASTRUCTURE_ERROR,
                    snapshot_revision=snapshot.base_commit,
                    baseline_phase=baseline_phase,
                    agent=agent,
                    investigations=investigations,
                    proofs=proofs,
                    candidate=candidate,
                    message="live provider evidence was incomplete or incoherent",
                )
            return _result(
                state=RepairCaseState.REPAIR_READY,
                outcome=Outcome.PASSED,
                snapshot_revision=snapshot.base_commit,
                baseline_phase=baseline_phase,
                agent=agent,
                investigations=investigations,
                proofs=proofs,
                candidate=candidate,
                message=(
                    "exact recipe and README candidate passed an independent fresh proof"
                ),
            )

        if proof_phase.outcome is Outcome.PASSED:
            # A pass without a complete binding is never repair proof.
            message = "proof pass was not bound to the frozen case and exact candidate"
            outcome = Outcome.INFRASTRUCTURE_ERROR
        else:
            message = _bounded_error(
                proof_phase.error or "candidate did not satisfy independent proof"
            )
            outcome = proof_phase.outcome
        if outcome in {
            Outcome.INFRASTRUCTURE_ERROR,
            Outcome.TIMED_OUT,
            Outcome.CLEANUP_FAILED,
            Outcome.UNSUPPORTED,
        }:
            agent = _agent_evidence(
                provider_config,
                proposer_kind=proposer_kind,
                observations=tuple(observations),
                records=tuple(records),
                baseline_refs=(baseline_ref,),
                status="completed",
                result=decision,
                error=None,
                elapsed_ms=_elapsed_ms(started_at),
            )
            return _result(
                state=RepairCaseState.BLOCKED,
                outcome=outcome,
                snapshot_revision=snapshot.base_commit,
                baseline_phase=baseline_phase,
                agent=agent,
                investigations=investigations,
                proofs=proofs,
                candidate=candidate,
                message=message,
            )
        prior_feedback = _proof_feedback(attempt_number, candidate, proof_phase)

    message = "repair attempts were exhausted without an accepted candidate"
    agent = _agent_evidence(
        provider_config,
        proposer_kind=proposer_kind,
        observations=tuple(observations),
        records=tuple(records),
        baseline_refs=(baseline_ref,),
        status="completed" if final_decision is not None else "budget_exhausted",
        result=final_decision,
        error=None if final_decision is not None else message,
        elapsed_ms=_elapsed_ms(started_at),
    )
    final_outcome = proofs[-1].outcome if proofs else Outcome.FAILED
    if final_outcome is Outcome.PASSED:
        final_outcome = Outcome.INFRASTRUCTURE_ERROR
    return _result(
        state=RepairCaseState.UNRESOLVED,
        outcome=final_outcome,
        snapshot_revision=snapshot.base_commit,
        baseline_phase=baseline_phase,
        agent=agent,
        investigations=investigations,
        proofs=proofs,
        candidate=final_candidate,
        message=message,
    )


def _repair_prerequisite_failure(
    provider_config: RepairProviderConfig,
    *,
    proposer_kind: Literal["live_strands", "fake_test"],
    started_at: float,
    source_revision: str | None,
) -> RepairLocalResult | None:
    if proposer_kind != "live_strands":
        return None
    missing: list[str] = []
    if not provider_config.provider_cost_acknowledged:
        missing.append("--acknowledge-provider-cost")
    if not provider_config.credential_identity_verified:
        missing.append("--confirm-verified-temporary-non-root-credentials")
    if not sys.flags.isolated:
        missing.append("run the controller with python -I")
    if not missing:
        return None
    message = "live repair prerequisites are missing: " + ", ".join(missing)
    agent = _agent_evidence(
        provider_config,
        proposer_kind=proposer_kind,
        observations=(),
        records=(),
        baseline_refs=(),
        status="policy_blocked",
        result=None,
        error=message,
        elapsed_ms=_elapsed_ms(started_at),
    )
    return RepairLocalResult(
        state=RepairCaseState.BLOCKED,
        outcome=Outcome.POLICY_BLOCKED,
        source_revision=source_revision,
        agent=agent,
        message=message,
    )


def _prepare_snapshot_inputs(
    snapshot: SourceSnapshot,
) -> tuple[SourceSnapshot, Target, Recipe]:
    """Seal and validate prepared source without consulting a local checkout."""

    if type(snapshot) is not SourceSnapshot:
        raise SourcePolicyError("prepared source must be a SourceSnapshot")
    for name, value in (
        ("base commit", snapshot.base_commit),
        ("base tree", snapshot.base_tree),
        ("source tree", snapshot.source_tree),
    ):
        if not isinstance(value, str) or _GIT_OBJECT_ID.fullmatch(value) is None:
            raise SourcePolicyError(f"prepared source has an invalid {name}")
    if not isinstance(snapshot.archive_digest, str) or _SHA256.fullmatch(
        snapshot.archive_digest
    ) is None:
        raise SourcePolicyError("prepared source has an invalid archive digest")
    if not isinstance(snapshot.content_tree_digest, str) or _SHA256.fullmatch(
        snapshot.content_tree_digest
    ) is None:
        raise SourcePolicyError("prepared source has an invalid content-tree digest")
    if not isinstance(snapshot.files, Mapping):
        raise SourcePolicyError("prepared source files must be a mapping")
    if not 1 <= len(snapshot.files) <= MAX_SOURCE_FILES:
        raise SourcePolicyError("prepared source file count is outside policy")

    sealed_files: dict[str, bytes] = {}
    total_bytes = 0
    for path, content in snapshot.files.items():
        if not isinstance(path, str) or not path or len(path) > 256:
            raise SourcePolicyError("prepared source contains an invalid path")
        parsed = PurePosixPath(path)
        if (
            "\\" in path
            or ":" in path
            or any(character in path for character in "\x00\r\n")
            or parsed.is_absolute()
            or ".." in parsed.parts
            or str(parsed) != path
        ):
            raise SourcePolicyError("prepared source path is not normalized")
        if type(content) is not bytes or len(content) > MAX_FILE_BYTES:
            raise SourcePolicyError(f"prepared source file is invalid or oversized: {path}")
        sealed_files[path] = content
        total_bytes += len(content)
    if total_bytes > MAX_SOURCE_BYTES:
        raise SourcePolicyError("prepared source exceeds its byte limit")
    required = {
        CONTROLLED_NODE_FIXTURE_POLICY.target_path,
        CONTROLLED_NODE_FIXTURE_POLICY.recipe_path,
        CONTROLLED_NODE_FIXTURE_POLICY.readme_path,
        "package.json",
    }
    missing = sorted(required.difference(sealed_files))
    if missing:
        raise SourcePolicyError(
            "prepared source is missing required controlled files: " + ", ".join(missing)
        )
    observed_tree_digest = content_tree_digest(sealed_files)
    if observed_tree_digest != snapshot.content_tree_digest:
        raise SourcePolicyError("prepared source bytes do not match its content-tree digest")

    # A private byte copy closes mutation races between remote ingestion, the model
    # tools, candidate construction, and proof. SourceSnapshot remains credential-free.
    from types import MappingProxyType

    sealed = replace(snapshot, files=MappingProxyType(sealed_files))
    target = Target.model_validate(
        _load_unique_json(sealed.content(CONTROLLED_NODE_FIXTURE_POLICY.target_path))
    )
    recipe = Recipe.model_validate(
        _load_unique_json(sealed.content(CONTROLLED_NODE_FIXTURE_POLICY.recipe_path))
    )
    if (
        classify_target_tuple(
            image_reference=target.runtime.image_ref,
            platform=target.runtime.platform,
            verifier_id=target.acceptance.verifier_id,
        )
        is not Outcome.PASSED
    ):
        raise SourcePolicyError("prepared target is outside the registered fixture lane")
    if not check_block_bytes(
        sealed.content(CONTROLLED_NODE_FIXTURE_POLICY.readme_path), recipe
    ):
        raise SourcePolicyError("README managed block differs from the prepared recipe")
    # Prepared remote revisions may contain a previously reviewed repair. They do
    # not inherit the teaching fixture's deliberately broken baseline allowlist.
    _assert_recipe_authorized(recipe, phase="proof", source_files=sealed.files)
    return sealed, target, recipe


def _assert_expected_runtime(
    runtime: PreparedDockerRuntime,
    expected_image_id: str | None,
    expected_repository_digest: str | None,
) -> None:
    if expected_image_id is not None:
        if not isinstance(expected_image_id, str) or _SHA256.fullmatch(
            expected_image_id
        ) is None:
            raise SourcePolicyError("expected runtime image ID is invalid")
        if runtime.image.image_id != expected_image_id:
            raise SourcePolicyError("prepared runtime image ID differs from registration")
    if expected_repository_digest is None:
        return
    if not isinstance(expected_repository_digest, str):
        raise SourcePolicyError("expected runtime repository digest is invalid")
    actual_reference = runtime.image.repository_digest
    actual_digest = actual_reference.rsplit("@", 1)[-1]
    if _SHA256.fullmatch(expected_repository_digest) is not None:
        matches = actual_digest == expected_repository_digest
    else:
        matches = bool(
            "@" in expected_repository_digest
            and _SHA256.fullmatch(expected_repository_digest.rsplit("@", 1)[-1])
            and actual_reference == expected_repository_digest
        )
    if not matches:
        raise SourcePolicyError(
            "prepared runtime repository digest differs from registration"
        )


def _new_binding() -> ControllerBinding:
    import uuid

    return ControllerBinding(
        run_id=uuid.uuid4(), attempt_id=uuid.uuid4(), workspace_id=uuid.uuid4()
    )


def _observe(
    observer: RepairObserver | None,
    event: str,
    payload: dict[str, object],
) -> str | None:
    if observer is None:
        return None
    try:
        observer(event, payload)
    except Exception:
        return f"trusted persistence observer failed during {event}"
    return None


def _running_payload(
    phase: str,
    binding: ControllerBinding,
    *,
    attempt_number: int | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "phase": phase,
        "binding": binding.model_dump(mode="json"),
    }
    if attempt_number is not None:
        payload["attempt_number"] = attempt_number
    return payload


def _phase_payload(phase: str, result: DockerPhaseResult) -> dict[str, object]:
    return {
        "phase": phase,
        "outcome": result.outcome.value,
        "binding": result.binding.model_dump(mode="json"),
        "evidence": (
            result.evidence.model_dump(mode="json")
            if result.evidence is not None
            else None
        ),
        "attempt": (
            result.attempt.model_dump(mode="json")
            if result.attempt is not None
            else None
        ),
        "error": _bounded_error(result.error) if result.error else None,
    }


def _snapshot_revision(value: object) -> str | None:
    if isinstance(value, SourceSnapshot) and isinstance(value.base_commit, str):
        return value.base_commit
    return None


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


def _from_input_failure(value: LocalVerificationResult) -> RepairLocalResult:
    return RepairLocalResult(
        state=RepairCaseState.BLOCKED,
        outcome=value.outcome,
        source_revision=value.source_revision,
        baseline=value.baseline,
        baseline_attempt=value.baseline_attempt,
        message=_bounded_error(value.message or "local repair input policy rejected the case"),
    )


def _result(
    *,
    state: RepairCaseState,
    outcome: Outcome,
    snapshot_revision: str,
    baseline_phase: DockerPhaseResult,
    agent: AgentRunEvidence,
    investigations: list[InvestigationEvidence],
    proofs: list[ProofAttemptRecord],
    candidate: CandidatePatch | None,
    message: str,
) -> RepairLocalResult:
    return RepairLocalResult(
        state=state,
        outcome=outcome,
        source_revision=snapshot_revision,
        baseline=baseline_phase.evidence,
        baseline_attempt=baseline_phase.attempt,
        agent=agent,
        investigations=tuple(investigations),
        proofs=tuple(proofs),
        candidate=candidate,
        message=_bounded_error(message),
    )


def _attempt_config(
    config: RepairProviderConfig, remaining_seconds: float
) -> RepairProviderConfig:
    wall = max(1, min(config.wall_time_seconds, int(remaining_seconds)))
    payload = config.model_dump(mode="python")
    payload.update(
        {
            "wall_time_seconds": wall,
            "connect_timeout_seconds": min(config.connect_timeout_seconds, wall),
            "read_timeout_seconds": min(config.read_timeout_seconds, wall),
        }
    )
    return RepairProviderConfig.model_validate(payload)


def _build_candidate(snapshot: SourceSnapshot, recipe: Recipe) -> CandidatePatch:
    recipe_bytes = (
        json.dumps(
            recipe.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    readme_bytes = replace_block_bytes(
        snapshot.content(CONTROLLED_NODE_FIXTURE_POLICY.readme_path), recipe
    )
    return build_candidate_patch(
        snapshot,
        {
            CONTROLLED_NODE_FIXTURE_POLICY.recipe_path: recipe_bytes,
            CONTROLLED_NODE_FIXTURE_POLICY.readme_path: readme_bytes,
        },
    )


def _used_required_evidence_tools(records: tuple[CapabilityRecord, ...]) -> bool:
    succeeded = {
        record.tool_name for record in records if record.outcome == "succeeded"
    }
    return "read_baseline_evidence" in succeeded and bool(
        succeeded.intersection(_SOURCE_TOOL_NAMES)
    )


def _live_observations_coherent(
    observations: tuple[LiveRepairAgentObservation, ...],
) -> bool:
    return bool(
        observations
        and all(
            value.succeeded
            and value.provider_invoked
            and value.sdk_version is not None
            and value.provider_endpoint is not None
            for value in observations
        )
        and len({value.sdk_version for value in observations}) == 1
        and len({value.provider_endpoint for value in observations}) == 1
    )


def _decision_uses_available_evidence(
    decision: RepairOutcomeValue,
    *,
    baseline_recipe: Recipe,
    baseline_ref: str,
    all_records: tuple[CapabilityRecord, ...],
    current_records: tuple[CapabilityRecord, ...],
) -> bool:
    cited = set(decision.evidence_refs)
    if isinstance(decision, RepairProposal):
        for reason in decision.change_reasons:
            cited.update(reason.evidence_refs)
    available = {baseline_ref} | {
        record.evidence_ref
        for record in all_records
        if record.outcome == "succeeded"
    }
    if not cited.issubset(available):
        return False
    if not isinstance(decision, RepairProposal):
        return True
    baseline_tools = {
        record.evidence_ref
        for record in current_records
        if record.outcome == "succeeded"
        and record.tool_name == "read_baseline_evidence"
    }
    source_tools = {
        record.evidence_ref
        for record in current_records
        if record.outcome == "succeeded" and record.tool_name in _SOURCE_TOOL_NAMES
    }
    changed_ids = _changed_recipe_step_ids(
        baseline_recipe, decision.proposed_recipe
    )
    reason_ids = {reason.step_id for reason in decision.change_reasons}
    if changed_ids != reason_ids:
        return False
    return bool(cited.intersection(baseline_tools)) and bool(
        cited.intersection(source_tools)
    )


def _changed_recipe_step_ids(baseline: Recipe, proposed: Recipe) -> set[str]:
    baseline_steps = {step.id: step for step in (*baseline.steps, baseline.start)}
    proposed_steps = {step.id: step for step in (*proposed.steps, proposed.start)}
    return {
        step_id
        for step_id, step in proposed_steps.items()
        if baseline_steps.get(step_id) != step
    } | (set(baseline_steps) - set(proposed_steps))


def _agent_evidence(
    config: RepairProviderConfig,
    *,
    proposer_kind: Literal["live_strands", "fake_test"],
    observations: tuple[LiveRepairAgentObservation, ...],
    records: tuple[CapabilityRecord, ...],
    baseline_refs: tuple[str, ...],
    status: Literal[
        "completed",
        "timed_out",
        "infrastructure_error",
        "policy_blocked",
        "budget_exhausted",
    ],
    result: RepairOutcomeValue | None,
    error: str | None,
    elapsed_ms: int,
) -> AgentRunEvidence:
    sdk_versions = {value.sdk_version for value in observations if value.sdk_version}
    endpoints = {
        value.provider_endpoint for value in observations if value.provider_endpoint
    }
    if len(sdk_versions) > 1 or len(endpoints) > 1 or (
        proposer_kind == "live_strands"
        and status == "completed"
        and (len(sdk_versions) != 1 or len(endpoints) != 1)
    ):
        status = "infrastructure_error"
        result = None
        error = "provider runtime identity changed between repair attempts"
    tool_calls = tuple(_tool_call_evidence(value) for value in records)
    attempt_results = tuple(
        AgentAttemptResult(
            attempt_number=attempt_number,
            result=observation.decision,
            result_digest=ContentDigest.from_bytes(
                _canonical_json(observation.decision.model_dump(mode="json"))
            ),
        )
        for attempt_number, observation in enumerate(observations, 1)
        if observation.decision is not None
    )
    result_digest = (
        ContentDigest.from_bytes(_canonical_json(result.model_dump(mode="json")))
        if result is not None and status == "completed"
        else None
    )
    invoked = any(value.provider_invoked for value in observations)
    return AgentRunEvidence(
        proposer_kind=proposer_kind,
        provider_config=config,
        strands_sdk_version=next(iter(sdk_versions), None),
        provider_endpoint=next(iter(endpoints), None),
        provider_invoked=invoked,
        provider_request_count=None,
        attempts_used=len(observations),
        model_cycles=_sum_known(value.model_cycles for value in observations),
        input_tokens=_sum_known(value.input_tokens for value in observations),
        output_tokens=_sum_known(value.output_tokens for value in observations),
        total_tokens=_sum_known(value.total_tokens for value in observations),
        elapsed_milliseconds=min(elapsed_ms, 660_000),
        baseline_evidence_refs=baseline_refs,
        tool_calls=tool_calls,
        attempt_results=attempt_results,
        status=status,
        result=result if status == "completed" else None,
        result_digest=result_digest,
        sanitized_error=(
            _bounded_error(error) if error is not None and status != "completed" else None
        ),
    )


def _tool_call_evidence(record: CapabilityRecord) -> ToolCallEvidence:
    status = "rejected" if record.outcome == "denied" else record.outcome
    return ToolCallEvidence(
        evidence_ref=record.evidence_ref,
        agent_attempt=record.agent_attempt,
        sequence=record.sequence,
        tool_name=record.tool_name,
        diagnostic_index=record.diagnostic_index,
        arguments_digest=ContentDigest(record.request_digest),
        result_digest=ContentDigest(record.response_digest),
        status=status,
        duration_milliseconds=record.elapsed_ms,
        sanitized_result=record.sanitized_result,
        output_truncated=record.output_truncated,
    )


def _baseline_reference(evidence: RunEvidence | AttemptEvidence) -> str:
    digest = hashlib.sha256(
        _canonical_json(evidence.model_dump(mode="json"))
    ).hexdigest()
    return f"baseline_{digest[:24]}"


def _proof_record(
    attempt_number: int,
    candidate: CandidatePatch,
    phase: DockerPhaseResult,
) -> ProofAttemptRecord:
    return ProofAttemptRecord(
        attempt_number=attempt_number,
        outcome=phase.outcome,
        binding=phase.binding,
        candidate_digest=candidate.patch_digest,
        candidate_tree_digest=candidate.candidate_tree_digest,
        evidence=phase.evidence,
        partial_evidence=phase.attempt,
        error=_bounded_error(phase.error) if phase.error else None,
    )


def _proof_feedback(
    attempt_number: int,
    candidate: CandidatePatch,
    phase: DockerPhaseResult,
) -> dict[str, object]:
    evidence = phase.evidence
    return {
        "attempt": attempt_number,
        "candidate_digest": candidate.patch_digest,
        "candidate_outcome": phase.outcome.value,
        "controller_error": _bounded_error(phase.error) if phase.error else None,
        "commands": (
            [
                {
                    "step_id": command.step_id,
                    "outcome": command.outcome.value,
                    "exit_code": command.exit_code,
                    "stderr_tail": command.sanitized_stderr_tail,
                }
                for command in evidence.commands
            ]
            if evidence is not None
            else []
        ),
        "readiness": (
            {
                "outcome": evidence.readiness.outcome.value,
                "error": evidence.readiness.sanitized_error,
            }
            if evidence is not None
            else None
        ),
        "acceptance": (
            {
                "outcome": evidence.acceptance_probe.outcome.value,
                "passed_checks": list(evidence.acceptance_probe.passed_checks),
                "error": evidence.acceptance_probe.sanitized_error,
            }
            if evidence is not None
            else None
        ),
    }


def _case_and_candidate_match(
    baseline: RunEvidence | AttemptEvidence,
    proof: RunEvidence,
    candidate: CandidatePatch,
) -> bool:
    frozen_baseline = (
        baseline.base_commit,
        baseline.base_git_tree,
        baseline.source_git_tree,
        baseline.base_tree_digest,
        baseline.base_archive_digest,
        baseline.target_reference,
        baseline.target_digest,
        baseline.verifier_id,
        baseline.verifier_digest,
        baseline.policy_revision,
        baseline.policy_digest,
        baseline.app_runtime,
        baseline.verifier_runtime,
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
    return (
        frozen_baseline == frozen_proof
        and str(proof.candidate_digest) == candidate.patch_digest
        and str(proof.candidate_tree_digest) == candidate.candidate_tree_digest
    )


def _all_execution_identities_are_independent(
    baseline: RunEvidence | AttemptEvidence,
    investigations: tuple[InvestigationEvidence, ...],
    proofs: tuple[ProofAttemptRecord, ...],
) -> bool:
    groups: list[set[str]] = [_phase_identity_set(baseline)]
    for investigation in investigations:
        identities = {
            str(investigation.binding.run_id),
            str(investigation.binding.attempt_id),
            str(investigation.binding.workspace_id),
        }
        if investigation.app_container_id is not None:
            identities.add(investigation.app_container_id)
        groups.append(identities)
    for record in proofs:
        observed = record.evidence or record.partial_evidence
        if observed is not None:
            groups.append(_phase_identity_set(observed))
        else:
            groups.append(
                {
                    str(record.binding.run_id),
                    str(record.binding.attempt_id),
                    str(record.binding.workspace_id),
                }
            )
    seen: set[str] = set()
    for identities in groups:
        if not identities or not seen.isdisjoint(identities):
            return False
        seen.update(identities)
    return True


def _phase_identity_set(value: RunEvidence | AttemptEvidence) -> set[str]:
    identities = {
        str(value.run_id),
        str(value.attempt_id),
        str(value.workspace_id),
    }
    if value.app_container_id is not None:
        identities.add(value.app_container_id)
    if value.verifier_container_id is not None:
        identities.add(value.verifier_container_id)
    return identities


def _needs_input_message(decision: NeedsInput) -> str:
    message = {
        "missing_secret": (
            "A required setup secret is unavailable. Configure it outside model "
            "context, then rerun this case."
        ),
        "maintainer_choice": (
            "The repair requires a maintainer choice between setup paths."
        ),
        "external_authority": (
            "The repair requires authority that this local run does not have."
        ),
        "ambiguous_repository_fact": (
            "The repository does not contain enough unambiguous setup information."
        ),
    }[decision.reason_code]
    if decision.affected_step_id is not None:
        return f"{message} Affected recipe step: {decision.affected_step_id}."
    return message


def _blocked_message(decision: Blocked) -> str:
    return {
        "unsupported_stack": "The agent found a stack outside the approved repair lane.",
        "protected_source_defect": (
            "The observed defect is outside the repairable setup recipe."
        ),
        "conflicting_evidence": "The bounded evidence was contradictory.",
        "provider_issue": "The agent reported a provider-side obstacle.",
        "budget_exhausted": "The agent could not decide within its bounded budget.",
        "policy_rejected": "The agent could not propose an authorized recipe.",
    }[decision.reason_code]


def _classify_agent_error(
    error_code: str | None,
) -> tuple[
    Literal[
        "completed",
        "timed_out",
        "infrastructure_error",
        "policy_blocked",
        "budget_exhausted",
    ],
    Outcome,
]:
    if error_code in {"provider_timeout", "budget_exhausted"}:
        return "timed_out", Outcome.TIMED_OUT
    if error_code in {"provider_authorization_missing", "isolated_python_required"}:
        return "policy_blocked", Outcome.POLICY_BLOCKED
    return "infrastructure_error", Outcome.INFRASTRUCTURE_ERROR


def _agent_status_for_outcome(
    outcome: Outcome,
) -> Literal[
    "completed",
    "timed_out",
    "infrastructure_error",
    "policy_blocked",
    "budget_exhausted",
]:
    if outcome is Outcome.TIMED_OUT:
        return "timed_out"
    if outcome in {Outcome.POLICY_BLOCKED, Outcome.UNSUPPORTED}:
        return "policy_blocked"
    return "infrastructure_error"


def _candidate_to_dict(candidate: CandidatePatch | None) -> dict[str, Any] | None:
    if candidate is None:
        return None
    return {
        "patch_digest": candidate.patch_digest,
        "candidate_tree_digest": candidate.candidate_tree_digest,
        "replacements": {
            path: content.decode("utf-8")
            for path, content in sorted(candidate.replacements.items())
        },
    }


def _investigation_to_dict(value: InvestigationEvidence) -> dict[str, Any]:
    return {
        "binding": value.binding.model_dump(mode="json"),
        "app_container_id": value.app_container_id,
        "workspace_marker_digest": (
            str(value.workspace_marker_digest)
            if value.workspace_marker_digest is not None
            else None
        ),
        "setup_command": (
            value.setup_command.model_dump(mode="json")
            if value.setup_command is not None
            else None
        ),
        "diagnostics": [
            command.model_dump(mode="json") for command in value.diagnostics
        ],
        "cleanup": value.cleanup.model_dump(mode="json"),
        "outcome": value.outcome.value,
        "sanitized_errors": list(value.sanitized_errors),
    }


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _elapsed_ms(started_at: float) -> int:
    return max(0, min(int((time.monotonic() - started_at) * 1000), 660_000))


def _bounded_error(value: str) -> str:
    cleaned = "".join(
        " " if unicodedata.category(character) in {"Cc", "Cf"} else character
        for character in value
    ).strip()
    return (cleaned or "unspecified controller error")[:4096]


def _sum_known(values: Iterable[int | None]) -> int | None:
    collected = tuple(values)
    if any(value is None for value in collected):
        return None
    return sum(value for value in collected if value is not None)


__all__ = [
    "NEEDS_INPUT_EXIT_CODE",
    "ProofAttemptRecord",
    "RepairCaseState",
    "RepairLocalResult",
    "RepairObserver",
    "repair_local",
    "repair_snapshot",
    "verify_snapshot",
]
