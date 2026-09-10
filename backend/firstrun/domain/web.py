"""Strict browser-safe M4 request and response contracts.

These DTOs intentionally contain neither provider configuration, credential paths,
raw agent tool results, nor unrestricted artifact payloads.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator


Sha = Annotated[
    str,
    StringConstraints(
        strict=True, min_length=40, max_length=40, pattern=r"^[0-9a-f]{40}$"
    ),
]
Digest = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=71,
        max_length=71,
        pattern=r"^sha256:[0-9a-f]{64}$",
    ),
]
SafeText = Annotated[
    str,
    StringConstraints(
        strict=True, max_length=4_096, pattern=r"^[^\x00]*$"
    ),
]
CasePhase = Literal[
    "queued",
    "fetching",
    "baseline_running",
    "investigating",
    "needs_input",
    "proof_running",
    "verified",
    "repair_ready",
    "publishing",
    "reconcile_pending",
    "pr_open",
    "unresolved",
    "blocked",
    "stale",
    "cancelled",
    "interrupted",
    "quarantined",
]
RunOutcome = Literal[
    "passed",
    "failed",
    "timed_out",
    "infrastructure_error",
    "policy_blocked",
    "unsupported",
    "cleanup_failed",
]


class WebModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RecipeStepView(WebModel):
    id: SafeText
    kind: Literal["foreground", "start"]
    argv: Annotated[tuple[SafeText, ...], Field(min_length=1, max_length=32)]
    cwd: SafeText
    timeout_seconds: Annotated[int, Field(strict=True, ge=1, le=600)]

    @field_validator("argv", mode="before")
    @classmethod
    def _tuple(cls, value: object) -> object:
        return tuple(value) if isinstance(value, (list, tuple)) else value


class RecipeView(WebModel):
    version: Literal[1]
    steps: Annotated[tuple[RecipeStepView, ...], Field(min_length=2, max_length=13)]

    @field_validator("steps", mode="before")
    @classmethod
    def _tuple(cls, value: object) -> object:
        return tuple(value) if isinstance(value, (list, tuple)) else value


class TargetView(WebModel):
    runtime_image: SafeText
    platform: Literal["linux/amd64", "linux/arm64"]
    network: Literal["none"]
    app_port: Annotated[int, Field(strict=True, ge=1024, le=65535)]
    readiness_path: SafeText
    readiness_status: Literal[200]
    readiness_timeout_seconds: Annotated[int, Field(strict=True, ge=1, le=120)]
    verifier_id: SafeText
    acceptance_timeout_seconds: Annotated[int, Field(strict=True, ge=1, le=120)]


class RepositorySummary(WebModel):
    repository_id: Annotated[int, Field(strict=True, ge=1)]
    owner: SafeText
    name: SafeText
    branch: SafeText
    current_head: Sha | None = None
    latest_checked_sha: Sha | None = None
    latest_outcome: RunOutcome | None = None
    open_case_id: SafeText | None = None
    open_case_phase: CasePhase | None = None
    automatic_prs: bool
    observed_at: Annotated[int, Field(strict=True, ge=0)] | None = None
    data_source: Literal["stored_controller_state"] = "stored_controller_state"


class RepositoryDetail(WebModel):
    repository: RepositorySummary
    approved_sha: Sha
    source_prefix: Literal["", "fixtures/notes-app"]
    target: TargetView
    recipe: RecipeView
    setup_instructions: SafeText
    target_digest: Digest
    recipe_digest: Digest
    protected_digest: Digest
    verifier_digest: Digest
    policy_revision: SafeText
    policy_digest: Digest
    runtime_digest: Digest
    runtime_image_id: Digest


class CaseSummary(WebModel):
    id: SafeText
    repository_id: Annotated[int, Field(strict=True, ge=1)]
    sha: Sha
    phase: CasePhase
    version: Annotated[int, Field(strict=True, ge=1)]
    outcome: RunOutcome | None = None
    created_at: Annotated[int, Field(strict=True, ge=0)]
    updated_at: Annotated[int, Field(strict=True, ge=0)]
    cancellation_requested: bool = False
    data_source: Literal["stored_controller_state"] = "stored_controller_state"


class CasePage(WebModel):
    items: Annotated[tuple[CaseSummary, ...], Field(max_length=100)]
    next_cursor: SafeText | None = None

    @field_validator("items", mode="before")
    @classmethod
    def _tuple(cls, value: object) -> object:
        return tuple(value) if isinstance(value, (list, tuple)) else value


class EventView(WebModel):
    event_id: Annotated[int, Field(strict=True, ge=1)]
    kind: SafeText
    phase: CasePhase
    summary: SafeText
    created_at: Annotated[int, Field(strict=True, ge=0)]


class EventPage(WebModel):
    items: Annotated[tuple[EventView, ...], Field(max_length=100)]
    next_cursor: Annotated[int, Field(strict=True, ge=1)] | None = None

    @field_validator("items", mode="before")
    @classmethod
    def _tuple(cls, value: object) -> object:
        return tuple(value) if isinstance(value, (list, tuple)) else value


class CommandView(WebModel):
    step_id: SafeText
    kind: Literal["foreground", "start"]
    argv: Annotated[tuple[SafeText, ...], Field(min_length=1, max_length=32)]
    cwd: SafeText
    outcome: RunOutcome
    exit_code: int | None = None
    stdout_tail: SafeText = ""
    stderr_tail: SafeText = ""

    @field_validator("argv", mode="before")
    @classmethod
    def _tuple(cls, value: object) -> object:
        return tuple(value) if isinstance(value, (list, tuple)) else value


class BaselineView(WebModel):
    tested_sha: Sha
    outcome: RunOutcome
    commands: Annotated[tuple[CommandView, ...], Field(max_length=13)]
    readiness_outcome: RunOutcome | None = None
    functional_outcome: RunOutcome | None = None
    target_digest: Digest
    recipe_digest: Digest
    verifier_digest: Digest
    runtime_image_id: Digest
    cleanup_succeeded: bool

    @field_validator("commands", mode="before")
    @classmethod
    def _tuple(cls, value: object) -> object:
        return tuple(value) if isinstance(value, (list, tuple)) else value


class ProofView(WebModel):
    tested_sha: Sha
    git_tree: Sha
    outcome: RunOutcome
    verified: bool
    candidate_digest: Digest | None = None
    candidate_tree_digest: Digest | None = None
    content_tree_digest: Digest
    target_digest: Digest
    recipe_digest: Digest
    readme_digest: Digest
    verifier_digest: Digest
    policy_digest: Digest
    runtime_digest: Digest
    runtime_image_id: Digest
    fresh_state: bool
    cleanup_succeeded: bool


class CandidateDiffView(WebModel):
    patch_digest: Digest
    candidate_tree_digest: Digest
    paths: Annotated[tuple[Literal[".firstrun/recipe.json", "README.md"], ...], Field(max_length=2)]
    unified_diff: Annotated[
        str,
        StringConstraints(strict=True, max_length=65_536, pattern=r"^[^\x00]*$"),
    ]

    @field_validator("paths", mode="before")
    @classmethod
    def _tuple(cls, value: object) -> object:
        return tuple(value) if isinstance(value, (list, tuple)) else value


class NeedsInputView(WebModel):
    reason_code: Literal[
        "missing_secret",
        "maintainer_choice",
        "external_authority",
        "ambiguous_repository_fact",
    ]
    affected_step_id: SafeText | None = None
    prompt: SafeText
    supported_actions: tuple[Literal["cancel", "recheck"], ...] = (
        "cancel",
        "recheck",
    )


class CaseDetail(WebModel):
    case: CaseSummary
    message: SafeText = ""
    baseline: BaselineView | None = None
    proof: ProofView | None = None
    exact_commit_proof: ProofView | None = None
    candidate: CandidateDiffView | None = None
    needs_input: NeedsInputView | None = None
    events: EventPage
    repair_pr_number: Annotated[int, Field(strict=True, ge=1)] | None = None
    repair_pr_url: SafeText | None = None
    check_url: SafeText | None = None


class StartRunRequest(WebModel):
    request_id: SafeText

    @field_validator("request_id")
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        return _uuid(value)


class DecisionRequest(WebModel):
    request_id: SafeText
    expected_version: Annotated[int, Field(strict=True, ge=1)]
    case_sha: Sha
    action: Literal["cancel", "recheck"]

    @field_validator("request_id")
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        return _uuid(value)


class DecisionResult(WebModel):
    action: Literal["cancel", "recheck"]
    case: CaseSummary
    replacement_case_id: SafeText | None = None


def _uuid(value: str) -> str:
    from uuid import UUID

    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError("request_id must be a canonical UUID") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("request_id must be a canonical UUIDv4")
    return value


__all__ = [
    "BaselineView",
    "CandidateDiffView",
    "CaseDetail",
    "CasePage",
    "CaseSummary",
    "CommandView",
    "DecisionRequest",
    "DecisionResult",
    "EventPage",
    "EventView",
    "NeedsInputView",
    "ProofView",
    "RecipeStepView",
    "RecipeView",
    "RepositoryDetail",
    "RepositorySummary",
    "StartRunRequest",
    "TargetView",
]
