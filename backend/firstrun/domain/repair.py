"""Strict M2 contracts for model repair decisions and controller evidence.

These models deliberately keep proof, target, verifier, and authoritative run
outcomes outside the model-controlled schema.  A repair proposal is only an
input to controller-side policy checks and an independent proof.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StringConstraints,
    field_validator,
    model_validator,
)

from firstrun.domain.contracts import Identifier, Recipe
from firstrun.domain.evidence import ContentDigest


MAX_REPAIR_ATTEMPTS = 2
MAX_DIAGNOSTIC_COMMANDS_PER_ATTEMPT = 8
MAX_AGENT_TURNS_PER_ATTEMPT = 16
MAX_AGENT_OUTPUT_TOKENS_PER_ATTEMPT = 4_096
MAX_AGENT_TOTAL_TOKENS_PER_ATTEMPT = 32_768
MAX_REPAIR_WALL_TIME_SECONDS = 600
MAX_TOOL_CALLS_PER_RUN = 64
MAX_PROVIDER_REQUESTS_PER_RUN = (
    MAX_REPAIR_ATTEMPTS * MAX_AGENT_TURNS_PER_ATTEMPT * 2
)


ShortText = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=1_024,
        pattern=r"^[^\x00-\x1f\x7f]*$",
    ),
]
Explanation = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=4_096,
        pattern=r"^[^\x00-\x1f\x7f]*$",
    ),
]
ProviderText = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=2_048,
        pattern=r"^[^\x00\r\n]+$",
    ),
]
EvidenceReference = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=3,
        max_length=128,
        pattern=r"^(?:baseline|tool)_[a-z0-9][a-z0-9_-]{0,118}$",
    ),
]
SanitizedToolResult = Annotated[
    str,
    StringConstraints(strict=True, max_length=16_384, pattern=r"^[^\x00]*$"),
]
SanitizedAgentError = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=4_096,
        pattern=r"^[^\x00-\x1f\x7f]*$",
    ),
]


def _as_tuple(value: object) -> object:
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return value


def _unique(values: tuple[str, ...], name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{name} must be unique")


class _FrozenRepairModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RepairProviderConfig(_FrozenRepairModel):
    """Explicit provider selection and controller-owned M2 budgets."""

    provider_id: Literal["amazon-bedrock"] = "amazon-bedrock"
    aws_profile: Annotated[
        str,
        StringConstraints(
            strict=True,
            min_length=1,
            max_length=256,
            pattern=r"^[^\x00\r\n]+$",
        ),
    ]
    region: Annotated[
        str,
        StringConstraints(
            strict=True,
            max_length=128,
            pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)+-[0-9]+$",
        ),
    ]
    model_id: ProviderText
    provider_cost_acknowledged: bool
    credential_identity_verified: bool
    max_attempts: Annotated[
        int, Field(strict=True, ge=1, le=MAX_REPAIR_ATTEMPTS)
    ] = MAX_REPAIR_ATTEMPTS
    max_diagnostic_commands_per_attempt: Annotated[
        int,
        Field(
            strict=True,
            ge=1,
            le=MAX_DIAGNOSTIC_COMMANDS_PER_ATTEMPT,
        ),
    ] = MAX_DIAGNOSTIC_COMMANDS_PER_ATTEMPT
    max_turns_per_attempt: Annotated[
        int, Field(strict=True, ge=1, le=MAX_AGENT_TURNS_PER_ATTEMPT)
    ] = 12
    max_output_tokens_per_attempt: Annotated[
        int,
        Field(
            strict=True,
            ge=1,
            le=MAX_AGENT_OUTPUT_TOKENS_PER_ATTEMPT,
        ),
    ] = 2_048
    max_total_tokens_per_attempt: Annotated[
        int,
        Field(
            strict=True,
            ge=1,
            le=MAX_AGENT_TOTAL_TOKENS_PER_ATTEMPT,
        ),
    ] = 16_384
    wall_time_seconds: Annotated[
        int, Field(strict=True, ge=1, le=MAX_REPAIR_WALL_TIME_SECONDS)
    ] = MAX_REPAIR_WALL_TIME_SECONDS
    connect_timeout_seconds: Annotated[
        int, Field(strict=True, ge=1, le=30)
    ] = 5
    read_timeout_seconds: Annotated[
        int, Field(strict=True, ge=1, le=120)
    ] = 60
    provider_total_attempts: Annotated[int, Field(strict=True, ge=1, le=2)] = 2

    @model_validator(mode="after")
    def budgets_are_coherent(self) -> Self:
        if self.max_total_tokens_per_attempt < self.max_output_tokens_per_attempt:
            raise ValueError(
                "max_total_tokens_per_attempt must be at least "
                "max_output_tokens_per_attempt"
            )
        if self.connect_timeout_seconds > self.wall_time_seconds:
            raise ValueError("connect_timeout_seconds exceeds the repair wall time")
        if self.read_timeout_seconds > self.wall_time_seconds:
            raise ValueError("read_timeout_seconds exceeds the repair wall time")
        return self


class RecipeChangeReason(_FrozenRepairModel):
    """Why one recipe step is part of the proposed change."""

    step_id: Identifier
    reason: Explanation
    evidence_refs: Annotated[
        tuple[EvidenceReference, ...], Field(min_length=1, max_length=16)
    ]

    _freeze_evidence_refs = field_validator("evidence_refs", mode="before")(_as_tuple)

    @model_validator(mode="after")
    def references_are_unique(self) -> Self:
        _unique(self.evidence_refs, "change evidence references")
        return self


class RepairProposal(_FrozenRepairModel):
    """Model proposal; never proof and never an authoritative success result."""

    kind: Literal["repair_proposal"] = "repair_proposal"
    diagnosis: Explanation
    proposed_recipe: Recipe
    change_reasons: Annotated[
        tuple[RecipeChangeReason, ...], Field(min_length=1, max_length=12)
    ]
    evidence_refs: Annotated[
        tuple[EvidenceReference, ...], Field(min_length=1, max_length=32)
    ]
    unresolved_risks: Annotated[tuple[ShortText, ...], Field(max_length=8)] = ()

    _freeze_change_reasons = field_validator("change_reasons", mode="before")(_as_tuple)
    _freeze_evidence_refs = field_validator("evidence_refs", mode="before")(_as_tuple)
    _freeze_unresolved_risks = field_validator("unresolved_risks", mode="before")(
        _as_tuple
    )

    @model_validator(mode="after")
    def proposal_is_coherent(self) -> Self:
        _unique(self.evidence_refs, "proposal evidence references")
        _unique(self.unresolved_risks, "unresolved risks")
        reason_ids = tuple(reason.step_id for reason in self.change_reasons)
        _unique(reason_ids, "change reason step IDs")
        recipe_ids = {step.id for step in self.proposed_recipe.steps}
        recipe_ids.add(self.proposed_recipe.start.id)
        if not set(reason_ids).issubset(recipe_ids):
            raise ValueError("change reason names a step absent from proposed_recipe")
        cited = set(self.evidence_refs)
        if any(not set(reason.evidence_refs).issubset(cited) for reason in self.change_reasons):
            raise ValueError(
                "change reason evidence must be listed in proposal evidence_refs"
            )
        return self


class NeedsInput(_FrozenRepairModel):
    """A coded request rendered into safe controller-owned copy."""

    kind: Literal["needs_input"] = "needs_input"
    reason_code: Literal[
        "missing_secret",
        "maintainer_choice",
        "external_authority",
        "ambiguous_repository_fact",
    ]
    affected_step_id: Identifier | None = None
    evidence_refs: Annotated[tuple[EvidenceReference, ...], Field(max_length=32)] = ()

    _freeze_evidence_refs = field_validator("evidence_refs", mode="before")(_as_tuple)

    @model_validator(mode="after")
    def references_are_unique(self) -> Self:
        _unique(self.evidence_refs, "needs-input evidence references")
        return self


BlockedReason = Literal[
    "unsupported_stack",
    "protected_source_defect",
    "conflicting_evidence",
    "provider_issue",
    "budget_exhausted",
    "policy_rejected",
]


class Blocked(_FrozenRepairModel):
    """A typed terminal diagnosis when no authorized proposal can be made."""

    kind: Literal["blocked"] = "blocked"
    reason_code: BlockedReason
    reason: Explanation
    evidence_refs: Annotated[tuple[EvidenceReference, ...], Field(max_length=32)] = ()

    _freeze_evidence_refs = field_validator("evidence_refs", mode="before")(_as_tuple)

    @model_validator(mode="after")
    def references_are_unique(self) -> Self:
        _unique(self.evidence_refs, "blocked evidence references")
        return self


RepairOutcomeValue: TypeAlias = Annotated[
    RepairProposal | NeedsInput | Blocked,
    Field(discriminator="kind"),
]


class RepairOutcome(RootModel[RepairOutcomeValue]):
    """Strict discriminated wrapper for untrusted model output."""

    model_config = ConfigDict(frozen=True, strict=True)


def validate_repair_outcome(value: Any) -> RepairOutcomeValue:
    """Validate an untrusted model result and return its typed branch."""

    return RepairOutcome.model_validate(value).root


ToolName = Literal[
    "read_source_file",
    "search_source",
    "inspect_pinned_diff",
    "read_baseline_evidence",
    "run_diagnostic",
]
ToolCallStatus = Literal["succeeded", "rejected", "failed", "timed_out"]


class ToolCallEvidence(_FrozenRepairModel):
    """Bounded controller observation of one model-requested capability call."""

    schema_version: Literal[1] = 1
    evidence_source: Literal["trusted_local_controller"] = "trusted_local_controller"
    evidence_ref: EvidenceReference
    agent_attempt: Annotated[
        int, Field(strict=True, ge=1, le=MAX_REPAIR_ATTEMPTS)
    ]
    sequence: Annotated[int, Field(strict=True, ge=1, le=MAX_TOOL_CALLS_PER_RUN)]
    tool_name: ToolName
    diagnostic_index: Annotated[
        int,
        Field(strict=True, ge=1, le=MAX_DIAGNOSTIC_COMMANDS_PER_ATTEMPT),
    ] | None = None
    arguments_digest: ContentDigest
    result_digest: ContentDigest | None = None
    status: ToolCallStatus
    duration_milliseconds: Annotated[int, Field(strict=True, ge=0, le=600_000)]
    sanitized_result: SanitizedToolResult = ""
    output_truncated: bool = False

    @model_validator(mode="after")
    def call_is_coherent(self) -> Self:
        if (
            self.tool_name == "run_diagnostic"
            and self.status in {"succeeded", "failed", "timed_out"}
            and self.diagnostic_index is None
        ):
            raise ValueError("an executed diagnostic requires diagnostic_index")
        if self.tool_name != "run_diagnostic" and self.diagnostic_index is not None:
            raise ValueError("only run_diagnostic may have diagnostic_index")
        if self.status == "succeeded" and self.result_digest is None:
            raise ValueError("a successful tool call requires result_digest")
        return self


AgentRunStatus = Literal[
    "completed",
    "timed_out",
    "infrastructure_error",
    "policy_blocked",
    "budget_exhausted",
]
ProposerKind = Literal["live_strands", "fake_test"]


class AgentAttemptResult(_FrozenRepairModel):
    """Exact typed decision returned by one provider attempt."""

    attempt_number: Annotated[
        int, Field(strict=True, ge=1, le=MAX_REPAIR_ATTEMPTS)
    ]
    result: RepairOutcomeValue
    result_digest: ContentDigest


class AgentRunEvidence(_FrozenRepairModel):
    """Controller-owned M2 record; a proposal here does not certify a repair."""

    schema_version: Literal[1] = 1
    evidence_source: Literal["trusted_local_controller"] = "trusted_local_controller"
    proposer_kind: ProposerKind
    provider_config: RepairProviderConfig
    strands_sdk_version: Annotated[
        str, StringConstraints(strict=True, min_length=1, max_length=64)
    ] | None = None
    provider_endpoint: Annotated[
        str, StringConstraints(strict=True, min_length=1, max_length=512)
    ] | None = None
    provider_invoked: bool
    provider_request_count: Annotated[
        int, Field(strict=True, ge=0, le=MAX_PROVIDER_REQUESTS_PER_RUN)
    ] | None = None
    attempts_used: Annotated[
        int, Field(strict=True, ge=0, le=MAX_REPAIR_ATTEMPTS)
    ]
    model_cycles: Annotated[
        int,
        Field(
            strict=True,
            ge=0,
            le=MAX_REPAIR_ATTEMPTS * MAX_AGENT_TURNS_PER_ATTEMPT,
        ),
    ] | None = None
    input_tokens: Annotated[
        int,
        Field(
            strict=True,
            ge=0,
            le=MAX_REPAIR_ATTEMPTS * MAX_AGENT_TOTAL_TOKENS_PER_ATTEMPT,
        ),
    ] | None = None
    output_tokens: Annotated[
        int,
        Field(
            strict=True,
            ge=0,
            le=MAX_REPAIR_ATTEMPTS * MAX_AGENT_OUTPUT_TOKENS_PER_ATTEMPT,
        ),
    ] | None = None
    total_tokens: Annotated[
        int,
        Field(
            strict=True,
            ge=0,
            le=MAX_REPAIR_ATTEMPTS * MAX_AGENT_TOTAL_TOKENS_PER_ATTEMPT,
        ),
    ] | None = None
    elapsed_milliseconds: Annotated[int, Field(strict=True, ge=0, le=660_000)]
    baseline_evidence_refs: Annotated[
        tuple[EvidenceReference, ...], Field(max_length=32)
    ] = ()
    tool_calls: Annotated[
        tuple[ToolCallEvidence, ...], Field(max_length=MAX_TOOL_CALLS_PER_RUN)
    ] = ()
    attempt_results: Annotated[
        tuple[AgentAttemptResult, ...], Field(max_length=MAX_REPAIR_ATTEMPTS)
    ] = ()
    status: AgentRunStatus
    result: RepairOutcomeValue | None = None
    result_digest: ContentDigest | None = None
    sanitized_error: SanitizedAgentError | None = None

    _freeze_baseline_refs = field_validator(
        "baseline_evidence_refs", mode="before"
    )(_as_tuple)
    _freeze_tool_calls = field_validator("tool_calls", mode="before")(_as_tuple)
    _freeze_attempt_results = field_validator("attempt_results", mode="before")(
        _as_tuple
    )

    @model_validator(mode="after")
    def run_is_coherent(self) -> Self:
        _unique(self.baseline_evidence_refs, "baseline evidence references")
        tool_refs = tuple(call.evidence_ref for call in self.tool_calls)
        _unique(tool_refs, "tool evidence references")
        if set(tool_refs).intersection(self.baseline_evidence_refs):
            raise ValueError("baseline and tool evidence references must be distinct")

        if self.proposer_kind == "fake_test":
            if self.provider_invoked or self.provider_request_count not in {None, 0}:
                raise ValueError("fake_test evidence cannot claim provider invocation")
            if self.strands_sdk_version is not None or self.provider_endpoint is not None:
                raise ValueError("fake_test evidence cannot claim a live provider runtime")
        elif (
            self.provider_request_count is not None
            and self.provider_request_count > 0
            and not self.provider_invoked
        ):
            raise ValueError("provider request count requires provider_invoked")

        if self.attempts_used > self.provider_config.max_attempts:
            raise ValueError("attempts_used exceeds the configured attempt budget")
        attempt_numbers = tuple(
            str(value.attempt_number) for value in self.attempt_results
        )
        _unique(attempt_numbers, "agent attempt result numbers")
        if any(
            value.attempt_number > self.attempts_used
            for value in self.attempt_results
        ):
            raise ValueError("agent result belongs to an unused attempt")
        if self.model_cycles is not None and self.model_cycles > (
            self.attempts_used * self.provider_config.max_turns_per_attempt
        ):
            raise ValueError("model_cycles exceeds the configured turn budget")
        if self.output_tokens is not None and self.output_tokens > (
            self.attempts_used
            * self.provider_config.max_output_tokens_per_attempt
        ):
            raise ValueError("output_tokens exceeds the configured output-token budget")
        if self.total_tokens is not None and self.total_tokens > (
            self.attempts_used * self.provider_config.max_total_tokens_per_attempt
        ):
            raise ValueError("total_tokens exceeds the configured total-token budget")
        if self.total_tokens is not None:
            if self.input_tokens is not None and self.total_tokens < self.input_tokens:
                raise ValueError("total_tokens cannot be smaller than input_tokens")
            if self.output_tokens is not None and self.total_tokens < self.output_tokens:
                raise ValueError("total_tokens cannot be smaller than output_tokens")

        per_attempt: dict[int, list[ToolCallEvidence]] = {}
        for call in self.tool_calls:
            if call.agent_attempt > self.attempts_used:
                raise ValueError("tool call belongs to an unused agent attempt")
            per_attempt.setdefault(call.agent_attempt, []).append(call)
        for calls in per_attempt.values():
            _unique(tuple(str(call.sequence) for call in calls), "tool call sequences")
            diagnostics = [
                call for call in calls if call.diagnostic_index is not None
            ]
            if len(diagnostics) > self.provider_config.max_diagnostic_commands_per_attempt:
                raise ValueError("diagnostic calls exceed the configured attempt budget")
            indexes = tuple(
                str(call.diagnostic_index) for call in diagnostics
            )
            _unique(indexes, "diagnostic indexes")

        if self.status == "completed":
            if self.result is None or self.result_digest is None:
                raise ValueError("completed agent evidence requires a typed result and digest")
            if self.attempts_used < 1:
                raise ValueError("completed agent evidence requires an attempted repair")
            if self.sanitized_error is not None:
                raise ValueError("completed agent evidence cannot include an agent error")
            if self.proposer_kind == "live_strands" and not self.provider_invoked:
                raise ValueError("completed live Strands evidence requires provider invocation")
        elif self.result is not None or self.result_digest is not None:
            raise ValueError("non-completed agent evidence cannot contain a model result")

        if self.result is not None:
            if (
                not self.attempt_results
                or self.attempt_results[-1].result != self.result
                or self.attempt_results[-1].result_digest != self.result_digest
            ):
                raise ValueError("final result must match the last recorded attempt result")
            available = set(self.baseline_evidence_refs) | {
                call.evidence_ref
                for call in self.tool_calls
                if call.status == "succeeded"
            }
            cited = _result_evidence_refs(self.result)
            if not cited.issubset(available):
                raise ValueError("agent result cites unavailable evidence")
        return self

    @property
    def milestone_eligible(self) -> bool:
        """Whether this is real completed Strands evidence, not proof of repair."""

        return (
            self.proposer_kind == "live_strands"
            and self.provider_invoked
            and self.status == "completed"
            and self.result is not None
            and self.result_digest is not None
        )


def _result_evidence_refs(result: RepairOutcomeValue) -> set[str]:
    references = set(result.evidence_refs)
    if isinstance(result, RepairProposal):
        for reason in result.change_reasons:
            references.update(reason.evidence_refs)
    return references


__all__ = [
    "MAX_REPAIR_ATTEMPTS",
    "MAX_DIAGNOSTIC_COMMANDS_PER_ATTEMPT",
    "MAX_AGENT_TURNS_PER_ATTEMPT",
    "MAX_AGENT_OUTPUT_TOKENS_PER_ATTEMPT",
    "MAX_AGENT_TOTAL_TOKENS_PER_ATTEMPT",
    "MAX_REPAIR_WALL_TIME_SECONDS",
    "MAX_TOOL_CALLS_PER_RUN",
    "MAX_PROVIDER_REQUESTS_PER_RUN",
    "EvidenceReference",
    "RepairProviderConfig",
    "RecipeChangeReason",
    "RepairProposal",
    "NeedsInput",
    "BlockedReason",
    "Blocked",
    "RepairOutcomeValue",
    "RepairOutcome",
    "validate_repair_outcome",
    "ToolName",
    "ToolCallStatus",
    "ToolCallEvidence",
    "AgentRunStatus",
    "ProposerKind",
    "AgentAttemptResult",
    "AgentRunEvidence",
]
