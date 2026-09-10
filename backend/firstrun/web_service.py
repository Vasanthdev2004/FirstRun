"""Authenticated-route service for M4's browser-safe controller projections.

Authentication and repository membership are API preconditions.  This module
still scopes every lookup to the single configured repository and never returns
raw state payloads, artifact objects, provider details, or credential paths.
"""

from __future__ import annotations

import difflib
import json
import re
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import ValidationError

from firstrun.artifacts import ArtifactStore
from firstrun.domain.contracts import Recipe, Target, sha256_content_ref
from firstrun.domain.evidence import AttemptEvidence, RunEvidence
from firstrun.domain.repair import NeedsInput
from firstrun.domain.web import (
    BaselineView,
    CandidateDiffView,
    CaseDetail,
    CasePage,
    CaseSummary,
    CommandView,
    DecisionRequest,
    DecisionResult,
    EventPage,
    EventView,
    NeedsInputView,
    ProofView,
    RecipeStepView,
    RecipeView,
    RepositoryDetail,
    RepositorySummary,
    StartRunRequest,
    TargetView,
)
from firstrun.github_state import (
    GitHubConfig,
    GitHubStateError,
    RepositoryRegistration,
    SQLiteStore,
    protected_source_digest,
)
from firstrun.verification.readme import check_block_bytes, render_block
from firstrun.verification.source import SourceSnapshot, build_candidate_patch


SourceLoader = Callable[[str], SourceSnapshot]
HeadLoader = Callable[[], str]
_ARTIFACT_KEYS = frozenset(
    {
        "repair_artifact",
        "worker_artifact",
        "exact_commit_proof_artifact",
        "exact_commit_attempt_artifact",
    }
)
_OPEN_PHASES = frozenset(
    {
        "queued",
        "fetching",
        "baseline_running",
        "investigating",
        "needs_input",
        "proof_running",
        "repair_ready",
        "publishing",
        "reconcile_pending",
        "interrupted",
        "quarantined",
    }
)
_OUTCOMES = frozenset(
    {
        "passed",
        "failed",
        "timed_out",
        "infrastructure_error",
        "policy_blocked",
        "unsupported",
        "cleanup_failed",
    }
)
_SAFE_EVENT_KIND = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TOKEN_PATTERNS = (
    re.compile(r"(?i)(authorization|password|secret|token)(\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"\bgh[opsu]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
)
_EVENT_SUMMARIES = {
    "enqueued": "Run queued from an authenticated GitHub event.",
    "manual_enqueued": "Maintainer requested a fresh run.",
    "recheck_enqueued": "Maintainer requested a fresh recheck.",
    "webhook_accepted": "Authenticated push accepted.",
    "lease_claimed": "Controller worker claimed the case.",
    "phase_updated": "Controller persisted execution progress.",
    "finished": "Controller persisted the case outcome.",
    "lease_released": "Controller released the case lease.",
    "lease_expired": "Worker lease expired; recovery policy was applied.",
    "superseded": "A newer branch revision superseded this pending case.",
    "cancellation_requested": "Cancellation will apply at a safe controller boundary.",
    "cancelled": "Queued work was cancelled before execution.",
    "recheck_requested": "A versioned recheck decision was recorded.",
    "write_intent": "External write intent was durably recorded.",
    "write_confirmed": "External write was confirmed by read-back.",
}
_NEEDS_INPUT_COPY = {
    "missing_secret": "Configure the required protected secret outside FirstRun, then recheck.",
    "maintainer_choice": "Resolve the repository-specific maintainer choice, then recheck.",
    "external_authority": "Complete the required external authorization, then recheck.",
    "ambiguous_repository_fact": "Clarify the repository setup fact, then recheck.",
}


class WebServiceError(RuntimeError):
    """Base class for safe M4 service failures."""


class NotFoundError(WebServiceError):
    """The authenticated repository scope does not contain the requested object."""


class ConflictError(WebServiceError):
    """A version, SHA, request identity, or current phase conflicts."""


class DataUnavailableError(WebServiceError):
    """Persisted controller data cannot safely produce the requested projection."""


class FirstRunWebService:
    def __init__(
        self,
        config: GitHubConfig,
        store: SQLiteStore,
        source_loader: SourceLoader,
        head_loader: HeadLoader,
        artifact_store: ArtifactStore | None = None,
    ) -> None:
        if not isinstance(config, GitHubConfig):
            raise TypeError("config must be GitHubConfig")
        if not isinstance(store, SQLiteStore):
            raise TypeError("store must be SQLiteStore")
        if not callable(source_loader) or not callable(head_loader):
            raise TypeError("source_loader and head_loader must be callable")
        if artifact_store is not None and not isinstance(artifact_store, ArtifactStore):
            raise TypeError("artifact_store must be ArtifactStore or None")
        self.config = config
        self.store = store
        self.source_loader = source_loader
        self.head_loader = head_loader
        self.artifacts = artifact_store

    def list_repositories(self, repository_id: int) -> tuple[RepositorySummary, ...]:
        self._scope(repository_id)
        return (self._repository_summary(repository_id),)

    def get_repository(self, repository_id: int) -> RepositoryDetail:
        registration = self._scope(repository_id)
        try:
            snapshot = self.source_loader(registration.approved_sha)
            if not isinstance(snapshot, SourceSnapshot):
                raise TypeError("source loader returned the wrong type")
            if snapshot.base_commit != registration.approved_sha:
                raise ValueError("approved source loader returned another revision")
            recipe_bytes = snapshot.content(".firstrun/recipe.json")
            target_bytes = snapshot.content(".firstrun/target.json")
            readme = snapshot.content("README.md")
            if sha256_content_ref(recipe_bytes) != registration.approved_recipe_digest:
                raise ValueError("approved recipe digest differs from registration")
            if sha256_content_ref(target_bytes) != registration.target_digest:
                raise ValueError("approved target digest differs from registration")
            if protected_source_digest(snapshot.files) != registration.protected_digest:
                raise ValueError("approved protected source differs from registration")
            recipe = Recipe.model_validate_json(recipe_bytes)
            target = Target.model_validate_json(target_bytes)
            if not check_block_bytes(readme, recipe):
                raise ValueError("approved README and recipe diverge")
        except Exception as exc:
            raise DataUnavailableError("approved repository contracts are unavailable") from exc
        return RepositoryDetail(
            repository=self._repository_summary(repository_id),
            approved_sha=registration.approved_sha,
            source_prefix=registration.source_prefix,
            target=TargetView(
                runtime_image=target.runtime.image_ref,
                platform=target.runtime.platform,
                network=target.network,
                app_port=target.app_port,
                readiness_path=target.readiness.path,
                readiness_status=target.readiness.status,
                readiness_timeout_seconds=target.readiness.timeout_seconds,
                verifier_id=target.acceptance.verifier_id,
                acceptance_timeout_seconds=target.acceptance.timeout_seconds,
            ),
            recipe=self._recipe_view(recipe),
            setup_instructions=render_block(recipe),
            target_digest=registration.target_digest,
            recipe_digest=registration.approved_recipe_digest,
            protected_digest=registration.protected_digest,
            verifier_digest=registration.verifier_digest,
            policy_revision=registration.policy_revision,
            policy_digest=registration.policy_digest,
            runtime_digest=registration.runtime_digest,
            runtime_image_id=registration.runtime_image_id,
        )

    def list_cases(
        self, repository_id: int, *, cursor: str | None = None, limit: int = 20
    ) -> CasePage:
        self._scope(repository_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("case page limit must be between 1 and 100")
        try:
            rows = self.store.list_cases(
                repository_id, before_case_id=cursor, limit=limit + 1
            )
        except GitHubStateError as exc:
            raise NotFoundError("case cursor is outside the repository") from exc
        extra = len(rows) > limit
        shown = rows[:limit]
        return CasePage(
            items=tuple(self._case_summary(row) for row in shown),
            next_cursor=str(shown[-1]["id"]) if extra and shown else None,
        )

    def list_events(
        self,
        repository_id: int,
        case_id: str,
        *,
        cursor: int | None = None,
        limit: int = 50,
    ) -> EventPage:
        self._scope(repository_id)
        self._case(repository_id, case_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("event page limit must be between 1 and 100")
        after = 0 if cursor is None else cursor
        try:
            rows = self.store.get_events(
                case_id,
                repository_id=repository_id,
                after_event_id=after,
                limit=limit + 1,
            )
        except GitHubStateError as exc:
            raise NotFoundError("case is outside the repository") from exc
        extra = len(rows) > limit
        shown = rows[:limit]
        return EventPage(
            items=tuple(self._event_view(row) for row in shown),
            next_cursor=int(shown[-1]["event_id"]) if extra and shown else None,
        )

    def get_case(
        self,
        repository_id: int,
        case_id: str,
        *,
        event_cursor: int | None = None,
        event_limit: int = 50,
    ) -> CaseDetail:
        case = self._case(repository_id, case_id)
        payload = self._mapping(case.get("payload"))
        return CaseDetail(
            case=self._case_summary(case),
            message=self._safe_text(payload.get("message"), 4_096),
            baseline=self._baseline(case),
            proof=self._proof(case, exact_commit=False),
            exact_commit_proof=self._proof(case, exact_commit=True),
            candidate=self._candidate(case),
            needs_input=self._needs_input(case),
            events=self.list_events(
                repository_id,
                case_id,
                cursor=event_cursor,
                limit=event_limit,
            ),
            repair_pr_number=self._positive_int(payload.get("pr_number")),
            repair_pr_url=self._safe_optional_url(payload.get("pr_url")),
            check_url=self._safe_optional_url(payload.get("check_url")),
        )

    def get_baseline(self, repository_id: int, case_id: str) -> BaselineView | None:
        return self._baseline(self._case(repository_id, case_id))

    def get_proof(
        self, repository_id: int, case_id: str, *, exact_commit: bool = False
    ) -> ProofView | None:
        return self._proof(self._case(repository_id, case_id), exact_commit=exact_commit)

    def get_candidate_diff(
        self, repository_id: int, case_id: str
    ) -> CandidateDiffView | None:
        return self._candidate(self._case(repository_id, case_id))

    def start_run(
        self, repository_id: int, request: StartRunRequest
    ) -> CaseSummary:
        registration = self._scope(repository_id)
        if not isinstance(request, StartRunRequest):
            raise TypeError("request must be StartRunRequest")
        try:
            head = self.head_loader()
            case_id = self.store.enqueue_manual(registration, head, request.request_id)
            self.store.set_health(repository_id, head=head)
        except (GitHubStateError, ValueError) as exc:
            raise ConflictError(str(exc)) from exc
        return self._case_summary(self._case(repository_id, case_id))

    def decide(
        self, repository_id: int, case_id: str, request: DecisionRequest
    ) -> DecisionResult:
        registration = self._scope(repository_id)
        if not isinstance(request, DecisionRequest):
            raise TypeError("request must be DecisionRequest")
        try:
            if request.action == "cancel":
                result = self.store.cancel_case(
                    repository_id,
                    case_id,
                    sha=request.case_sha,
                    expected_version=request.expected_version,
                    request_id=request.request_id,
                )
                replacement = None
            else:
                head = self.head_loader()
                result = self.store.recheck_case(
                    registration,
                    case_id,
                    sha=request.case_sha,
                    expected_version=request.expected_version,
                    request_id=request.request_id,
                    current_sha=head,
                )
                self.store.set_health(repository_id, head=head)
                replacement = str(result["id"])
        except (GitHubStateError, ValueError) as exc:
            raise ConflictError(str(exc)) from exc
        return DecisionResult(
            action=request.action,
            case=self._case_summary(result),
            replacement_case_id=replacement,
        )

    def _scope(self, repository_id: int):
        registration = self.config.registration
        if type(repository_id) is not int or repository_id != registration.repository_id:
            raise NotFoundError("repository is not in the authenticated configuration")
        return registration

    def _case(self, repository_id: int, case_id: str) -> dict[str, object]:
        self._scope(repository_id)
        case = self.store.get_case(case_id)
        if case is None or case.get("repository_id") != repository_id:
            raise NotFoundError("case is not in the authenticated repository")
        return case

    def _repository_summary(self, repository_id: int) -> RepositorySummary:
        registration = self._scope(repository_id)
        health = self.store.get_health(repository_id) or {}
        open_case: dict[str, object] | None = None
        cursor: str | None = None
        while open_case is None:
            rows = self.store.list_cases(
                repository_id, before_case_id=cursor, limit=101
            )
            for row in rows:
                if row.get("phase") in _OPEN_PHASES:
                    open_case = row
                    break
            if open_case is not None or len(rows) < 101:
                break
            cursor = str(rows[-1]["id"])
        return RepositorySummary(
            repository_id=repository_id,
            owner=registration.owner,
            name=registration.name,
            branch=registration.branch,
            current_head=health.get("head"),
            latest_checked_sha=health.get("checked_sha"),
            latest_outcome=health.get("outcome"),
            open_case_id=str(open_case["id"]) if open_case else None,
            open_case_phase=open_case.get("phase") if open_case else None,
            automatic_prs=registration.automatic_prs,
            observed_at=health.get("updated_at"),
        )

    def _case_summary(self, case: Mapping[str, object]) -> CaseSummary:
        payload = self._mapping(case.get("payload"))
        return CaseSummary(
            id=case["id"],
            repository_id=case["repository_id"],
            sha=case["sha"],
            phase=case["phase"],
            version=case["version"],
            outcome=self._case_outcome(case),
            created_at=case["created_at"],
            updated_at=case["updated_at"],
            cancellation_requested=payload.get("cancellation_requested") is True,
        )

    def _case_outcome(self, case: Mapping[str, object]) -> str | None:
        repair = self._repair(case)
        if repair is not None and repair.get("outcome") in _OUTCOMES:
            return str(repair["outcome"])
        phase = case.get("phase")
        return "passed" if phase in {"verified", "repair_ready", "pr_open"} else None

    def _event_view(self, event: Mapping[str, object]) -> EventView:
        raw_kind = event.get("kind")
        kind = raw_kind if isinstance(raw_kind, str) and _SAFE_EVENT_KIND.fullmatch(raw_kind) else "state_updated"
        return EventView(
            event_id=event["event_id"],
            kind=kind,
            phase=event["phase"],
            summary=_EVENT_SUMMARIES.get(kind, "Controller recorded a state update."),
            created_at=event["created_at"],
        )

    def _repair(self, case: Mapping[str, object]) -> dict[str, Any] | None:
        payload = self._mapping(case.get("payload"))
        reference = payload.get("repair_artifact")
        if reference is None:
            return None
        return self._artifact(case, reference)

    def _artifact(self, case: Mapping[str, object], reference: object) -> dict[str, Any]:
        if self.artifacts is None:
            raise DataUnavailableError("controller artifact storage is unavailable")
        if not isinstance(reference, str) or reference not in self._artifact_refs(case):
            raise NotFoundError("artifact is not bound to this case")
        try:
            return self.artifacts.get(reference)
        except Exception as exc:
            raise DataUnavailableError("controller artifact is unavailable") from exc

    def _artifact_refs(self, case: Mapping[str, object]) -> frozenset[str]:
        references: set[str] = set()
        payloads: list[Mapping[str, object]] = [self._mapping(case.get("payload"))]
        try:
            events = self.store.get_events(
                str(case["id"]), repository_id=int(case["repository_id"])
            )
        except (GitHubStateError, ValueError) as exc:
            raise DataUnavailableError("case artifact bindings are unavailable") from exc
        payloads.extend(self._mapping(event.get("payload")) for event in events)
        for payload in payloads:
            for key in _ARTIFACT_KEYS:
                value = payload.get(key)
                if isinstance(value, str):
                    references.add(value)
        return frozenset(references)

    def _baseline(self, case: Mapping[str, object]) -> BaselineView | None:
        repair = self._repair(case)
        if repair is None:
            return None
        raw = repair.get("baseline") or repair.get("baseline_attempt")
        if raw is None:
            return None
        try:
            evidence = (
                RunEvidence.model_validate_json(json.dumps(raw))
                if repair.get("baseline") is not None
                else AttemptEvidence.model_validate_json(json.dumps(raw))
            )
            self._bind_evidence(case, evidence, candidate=False, exact_commit=False)
            return self._baseline_view(evidence)
        except (ValidationError, ValueError, TypeError) as exc:
            raise DataUnavailableError("baseline evidence is invalid") from exc

    def _baseline_view(self, evidence: RunEvidence | AttemptEvidence) -> BaselineView:
        return BaselineView(
            tested_sha=evidence.base_commit,
            outcome=evidence.outcome.value,
            commands=tuple(
                CommandView(
                    step_id=value.step_id,
                    kind=value.kind,
                    argv=value.argv,
                    cwd=value.cwd,
                    outcome=value.outcome.value,
                    exit_code=value.exit_code,
                    stdout_tail=self._safe_text(value.sanitized_stdout_tail, 4_096),
                    stderr_tail=self._safe_text(value.sanitized_stderr_tail, 4_096),
                )
                for value in evidence.commands
            ),
            readiness_outcome=(
                evidence.readiness.outcome.value if evidence.readiness is not None else None
            ),
            functional_outcome=(
                evidence.acceptance_probe.outcome.value
                if evidence.acceptance_probe is not None
                else None
            ),
            target_digest=str(evidence.target_digest),
            recipe_digest=str(evidence.recipe_digest),
            verifier_digest=str(evidence.verifier_digest),
            runtime_image_id=str(evidence.app_runtime.image_id),
            cleanup_succeeded=evidence.cleanup.succeeded,
        )

    def _bind_evidence(
        self,
        case: Mapping[str, object],
        evidence: RunEvidence | AttemptEvidence,
        *,
        candidate: bool,
        exact_commit: bool,
    ) -> None:
        """Rebind a typed artifact to its exact case, source, and approval pins."""

        try:
            registration = RepositoryRegistration.model_validate_json(
                json.dumps(case.get("registration"))
            )
            payload = self._mapping(case.get("payload"))
            expected_commit = (
                payload.get("repair_commit") if exact_commit else case.get("sha")
            )
            if not isinstance(expected_commit, str):
                raise ValueError("tested commit is absent")
            snapshot = self.source_loader(expected_commit)
            if not isinstance(snapshot, SourceSnapshot):
                raise TypeError("source loader returned the wrong type")
            if (
                evidence.base_commit != expected_commit
                or snapshot.base_commit != expected_commit
                or evidence.base_git_tree != snapshot.base_tree
                or evidence.source_git_tree != snapshot.source_tree
                or str(evidence.base_tree_digest) != snapshot.content_tree_digest
                or str(evidence.base_archive_digest) != snapshot.archive_digest
            ):
                raise ValueError("evidence source binding differs from the case")
            if exact_commit and payload.get("repair_git_tree") != snapshot.base_tree:
                raise ValueError("repair Git tree differs from exact proof")
            if protected_source_digest(snapshot.files) != registration.protected_digest:
                raise ValueError("protected source differs from case approval")
            target_digest = sha256_content_ref(snapshot.content(".firstrun/target.json"))
            if (
                target_digest != registration.target_digest
                or str(evidence.target_digest) != registration.target_digest
                or str(evidence.verifier_digest) != registration.verifier_digest
                or str(evidence.policy_digest) != registration.policy_digest
                or evidence.policy_revision != registration.policy_revision
                or str(evidence.app_runtime.repository_digest)
                != registration.runtime_digest
                or str(evidence.app_runtime.image_id) != registration.runtime_image_id
            ):
                raise ValueError("evidence changed an immutable case input")
            expected_recipe = snapshot.content(".firstrun/recipe.json")
            expected_readme = snapshot.content("README.md")
            if candidate:
                if evidence.phase != "proof":
                    raise ValueError("candidate proof uses the wrong phase")
                base_snapshot = self.source_loader(str(case["sha"]))
                if (
                    not isinstance(base_snapshot, SourceSnapshot)
                    or base_snapshot.base_commit != case["sha"]
                ):
                    raise ValueError("candidate base source differs from case")
                repair = self._repair(case)
                raw_candidate = self._mapping(
                    repair.get("candidate") if repair is not None else None
                )
                raw_replacements = self._mapping(raw_candidate.get("replacements"))
                replacements = {
                    path: value.encode("utf-8")
                    for path, value in raw_replacements.items()
                    if isinstance(path, str) and isinstance(value, str)
                }
                patch = build_candidate_patch(base_snapshot, replacements)
                expected_recipe = patch.replacements.get(
                    ".firstrun/recipe.json",
                    base_snapshot.content(".firstrun/recipe.json"),
                )
                expected_readme = patch.replacements.get(
                    "README.md", base_snapshot.content("README.md")
                )
                if (
                    str(evidence.candidate_digest) != patch.patch_digest
                    or str(evidence.candidate_tree_digest)
                    != patch.candidate_tree_digest
                ):
                    raise ValueError("proof differs from the candidate bytes")
            elif evidence.phase != "baseline":
                raise ValueError("baseline evidence uses the wrong phase")
            if (
                str(evidence.recipe_digest) != sha256_content_ref(expected_recipe)
                or str(evidence.readme_digest) != sha256_content_ref(expected_readme)
            ):
                raise ValueError("executed setup bytes differ from the source")
            if isinstance(evidence, RunEvidence) and (
                str(evidence.observed_target_digest) != registration.target_digest
                or str(evidence.executed_recipe_digest)
                != sha256_content_ref(expected_recipe)
                or str(evidence.rendered_readme_digest)
                != sha256_content_ref(expected_readme)
                or str(evidence.observed_verifier_digest)
                != registration.verifier_digest
                or str(evidence.observed_policy_digest) != registration.policy_digest
            ):
                raise ValueError("observed proof inputs differ from expected inputs")
            if exact_commit:
                repair = self._repair(case)
                raw_candidate = self._mapping(
                    repair.get("candidate") if repair is not None else None
                )
                raw_replacements = self._mapping(raw_candidate.get("replacements"))
                base = self.source_loader(str(case["sha"]))
                patch = build_candidate_patch(
                    base,
                    {
                        path: value.encode("utf-8")
                        for path, value in raw_replacements.items()
                        if isinstance(path, str) and isinstance(value, str)
                    },
                )
                if (
                    snapshot.content_tree_digest != patch.candidate_tree_digest
                    or payload.get("candidate_digest") != patch.patch_digest
                ):
                    raise ValueError("exact commit proof differs from proved candidate")
        except (KeyError, TypeError, ValueError, ValidationError, UnicodeError) as exc:
            raise DataUnavailableError("evidence is not bound to this exact case") from exc

    def _proof(
        self, case: Mapping[str, object], *, exact_commit: bool
    ) -> ProofView | None:
        try:
            if exact_commit:
                payload = self._mapping(case.get("payload"))
                reference = payload.get("exact_commit_proof_artifact")
                if reference is None:
                    return None
                raw = self._artifact(case, reference)
            else:
                repair = self._repair(case)
                if repair is None:
                    return None
                proofs = repair.get("proofs")
                if not isinstance(proofs, list):
                    return None
                raw = next(
                    (
                        item.get("evidence")
                        for item in reversed(proofs)
                        if isinstance(item, Mapping) and item.get("evidence") is not None
                    ),
                    None,
                )
                if raw is None:
                    return None
            evidence = RunEvidence.model_validate_json(json.dumps(raw))
            self._bind_evidence(
                case,
                evidence,
                candidate=not exact_commit,
                exact_commit=exact_commit,
            )
            return ProofView(
                tested_sha=evidence.base_commit,
                git_tree=evidence.base_git_tree,
                outcome=evidence.outcome.value,
                verified=evidence.verified,
                candidate_digest=(
                    str(evidence.candidate_digest)
                    if evidence.candidate_digest is not None
                    else None
                ),
                candidate_tree_digest=(
                    str(evidence.candidate_tree_digest)
                    if evidence.candidate_tree_digest is not None
                    else None
                ),
                content_tree_digest=str(evidence.base_tree_digest),
                target_digest=str(evidence.target_digest),
                recipe_digest=str(evidence.recipe_digest),
                readme_digest=str(evidence.readme_digest),
                verifier_digest=str(evidence.verifier_digest),
                policy_digest=str(evidence.policy_digest),
                runtime_digest=str(evidence.app_runtime.repository_digest),
                runtime_image_id=str(evidence.app_runtime.image_id),
                fresh_state=evidence.fresh_state.satisfied,
                cleanup_succeeded=evidence.cleanup.succeeded,
            )
        except (ValidationError, ValueError, TypeError) as exc:
            raise DataUnavailableError("proof evidence is invalid") from exc

    def _candidate(self, case: Mapping[str, object]) -> CandidateDiffView | None:
        repair = self._repair(case)
        if repair is None or repair.get("candidate") is None:
            return None
        try:
            if repair.get("source_revision") != case.get("sha"):
                raise ValueError("candidate source revision differs from case")
            raw = self._mapping(repair.get("candidate"))
            replacements = self._mapping(raw.get("replacements"))
            if not replacements or any(
                path not in {".firstrun/recipe.json", "README.md"}
                or not isinstance(content, str)
                for path, content in replacements.items()
            ):
                raise ValueError("candidate replacements are invalid")
            snapshot = self.source_loader(str(case["sha"]))
            if not isinstance(snapshot, SourceSnapshot) or snapshot.base_commit != case["sha"]:
                raise ValueError("candidate source loader returned another revision")
            encoded = {path: content.encode("utf-8") for path, content in replacements.items()}
            candidate = build_candidate_patch(snapshot, encoded)
            if (
                candidate.patch_digest != raw.get("patch_digest")
                or candidate.candidate_tree_digest != raw.get("candidate_tree_digest")
            ):
                raise ValueError("candidate digest differs from persisted bytes")
            chunks: list[str] = []
            for path in sorted(candidate.replacements):
                before = snapshot.content(path).decode("utf-8")
                after = candidate.replacements[path].decode("utf-8")
                chunks.extend(
                    difflib.unified_diff(
                        before.splitlines(),
                        after.splitlines(),
                        fromfile="a/" + path,
                        tofile="b/" + path,
                        lineterm="",
                    )
                )
            unified = "\n".join(chunks)
            if self._contains_secret(unified):
                raise ValueError("candidate resembles a credential")
            return CandidateDiffView(
                patch_digest=candidate.patch_digest,
                candidate_tree_digest=candidate.candidate_tree_digest,
                paths=tuple(sorted(candidate.replacements)),
                unified_diff=unified,
            )
        except (UnicodeError, ValidationError, ValueError, TypeError, KeyError) as exc:
            raise DataUnavailableError("candidate diff is invalid or unavailable") from exc

    def _needs_input(self, case: Mapping[str, object]) -> NeedsInputView | None:
        repair = self._repair(case)
        if repair is None:
            return None
        agent = repair.get("agent")
        if not isinstance(agent, Mapping):
            return None
        attempts = agent.get("attempt_results")
        if not isinstance(attempts, list):
            return None
        raw = next(
            (
                item.get("result")
                for item in reversed(attempts)
                if isinstance(item, Mapping)
                and isinstance(item.get("result"), Mapping)
                and item["result"].get("kind") == "needs_input"
            ),
            None,
        )
        if raw is None:
            return None
        try:
            decision = NeedsInput.model_validate_json(json.dumps(raw))
        except ValidationError as exc:
            raise DataUnavailableError("needs-input decision is invalid") from exc
        return NeedsInputView(
            reason_code=decision.reason_code,
            affected_step_id=decision.affected_step_id,
            prompt=_NEEDS_INPUT_COPY[decision.reason_code],
        )

    @staticmethod
    def _recipe_view(recipe: Recipe) -> RecipeView:
        return RecipeView(
            version=recipe.version,
            steps=tuple(
                RecipeStepView(
                    id=step.id,
                    kind="start" if step is recipe.start else "foreground",
                    argv=step.argv,
                    cwd=step.cwd,
                    timeout_seconds=step.timeout_seconds,
                )
                for step in (*recipe.steps, recipe.start)
            ),
        )

    @staticmethod
    def _mapping(value: object) -> Mapping[str, Any]:
        return value if isinstance(value, Mapping) else {}

    @classmethod
    def _safe_text(cls, value: object, limit: int) -> str:
        if not isinstance(value, str):
            return ""
        text = "".join(
            char for char in value if char in "\n\t" or ord(char) >= 32
        )[-limit:]
        for pattern in _TOKEN_PATTERNS:
            text = pattern.sub(lambda match: match.group(1) + match.group(2) + "[REDACTED]" if match.lastindex else "[REDACTED]", text)
        return text

    @classmethod
    def _contains_secret(cls, value: str) -> bool:
        return any(pattern.search(value) is not None for pattern in _TOKEN_PATTERNS)

    @staticmethod
    def _positive_int(value: object) -> int | None:
        return value if type(value) is int and value > 0 else None

    @staticmethod
    def _safe_optional_url(value: object) -> str | None:
        if not isinstance(value, str) or len(value) > 4_096 or "\x00" in value:
            return None
        return value if value.startswith("https://github.com/") else None


__all__ = [
    "ConflictError",
    "DataUnavailableError",
    "FirstRunWebService",
    "NotFoundError",
    "WebServiceError",
]
