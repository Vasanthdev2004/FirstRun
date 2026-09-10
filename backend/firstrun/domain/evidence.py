"""Strict controller-owned evidence contracts for local verification runs.

These models describe observations made by the trusted controller.  They are not
an authentication format: callers must not deserialize repository, worker, or
model JSON directly into :class:`RunEvidence`.  ``build_run_evidence`` is the
single construction seam and binds an observation to the controller's expected
run, attempt, and workspace identifiers.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StringConstraints,
    field_validator,
    model_validator,
)

from firstrun.domain.outcomes import Outcome


DigestValue = Annotated[
    str,
    StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$", strict=True),
]
GitObjectId = Annotated[
    str,
    StringConstraints(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$", strict=True),
]
ReferenceText = Annotated[str, StringConstraints(min_length=1, max_length=512, strict=True)]
IdentifierText = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
        strict=True,
    ),
]
StepId = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,39}$", strict=True),
]
CommandArgument = Annotated[
    str,
    StringConstraints(min_length=1, max_length=2048, pattern=r"^[^\x00\r\n]+$", strict=True),
]
CheckId = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$", strict=True),
]
SanitizedError = Annotated[str, StringConstraints(min_length=1, max_length=4096, strict=True)]
SanitizedLogTail = Annotated[str, StringConstraints(max_length=16384, strict=True)]
RelativeReference = Annotated[str, StringConstraints(min_length=1, max_length=256, strict=True)]
ContainerId = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{12,64}$", strict=True),
]


class ContentDigest(RootModel[DigestValue]):
    """A lowercase SHA-256 content digest with an explicit algorithm prefix."""

    model_config = ConfigDict(frozen=True, strict=True)

    @classmethod
    def from_bytes(cls, value: bytes) -> ContentDigest:
        """Hash trusted bytes into the canonical evidence representation."""

        from hashlib import sha256

        return cls(f"sha256:{sha256(value).hexdigest()}")

    def __str__(self) -> str:
        return self.root


class _FrozenEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _validate_relative_reference(value: str) -> str:
    if "\\" in value or ":" in value or any(character in value for character in "\x00\r\n"):
        raise ValueError("reference must be an unambiguous POSIX-relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value:
        raise ValueError("reference must be normalized and relative")
    return value


class RuntimeImageEvidence(_FrozenEvidence):
    """The exact image selected once by the controller for a case."""

    requested_reference: ReferenceText
    resolved_reference: ReferenceText
    repository_digest: ContentDigest
    image_id: ContentDigest
    platform: Literal["linux/amd64", "linux/arm64"]

    @model_validator(mode="after")
    def resolved_reference_contains_digest(self) -> Self:
        if not self.resolved_reference.endswith(f"@{self.repository_digest}"):
            raise ValueError("resolved_reference must end with the repository digest")
        return self


class CommandEvidence(_FrozenEvidence):
    """A bounded observation for one frozen recipe command."""

    step_id: StepId
    kind: Literal["foreground", "start"]
    argv: Annotated[tuple[CommandArgument, ...], Field(min_length=1, max_length=32)]
    cwd: RelativeReference
    timeout_seconds: Annotated[int, Field(ge=1, le=600)]
    outcome: Outcome
    exit_code: int | None
    sanitized_stdout_tail: SanitizedLogTail = ""
    sanitized_stderr_tail: SanitizedLogTail = ""

    _cwd_is_relative = field_validator("cwd")(_validate_relative_reference)

    @model_validator(mode="after")
    def result_is_coherent(self) -> Self:
        if self.outcome in {Outcome.UNSUPPORTED, Outcome.POLICY_BLOCKED, Outcome.CLEANUP_FAILED}:
            raise ValueError("command evidence cannot use a controller-level outcome")
        if self.outcome is Outcome.PASSED:
            if self.kind == "foreground" and self.exit_code != 0:
                raise ValueError("a passed foreground command must exit zero")
            if self.kind == "start" and self.exit_code is not None:
                raise ValueError("a passed managed start command must still be running")
        return self

    @property
    def succeeded(self) -> bool:
        if self.outcome is not Outcome.PASSED:
            return False
        return self.exit_code == 0 if self.kind == "foreground" else self.exit_code is None


class ReadinessEvidence(_FrozenEvidence):
    """Controller observation of the protected readiness condition."""

    path: Annotated[str, StringConstraints(min_length=1, max_length=256, pattern=r"^/[^\x00\r\n]*$", strict=True)]
    expected_status: Annotated[int, Field(ge=100, le=599)]
    observed_status: Annotated[int, Field(ge=100, le=599)] | None
    timeout_seconds: Annotated[int, Field(ge=1, le=120)]
    attempts: Annotated[int, Field(ge=0, le=10_000)]
    outcome: Outcome
    sanitized_error: SanitizedError | None = None

    @property
    def succeeded(self) -> bool:
        return (
            self.outcome is Outcome.PASSED
            and self.attempts > 0
            and self.observed_status == self.expected_status
            and self.sanitized_error is None
        )


class AcceptanceProbeEvidence(_FrozenEvidence):
    """Result emitted by the registered trusted functional verifier."""

    verifier_id: IdentifierText
    verifier_digest: ContentDigest
    timeout_seconds: Annotated[int, Field(ge=1, le=120)]
    outcome: Outcome
    nonce_digest: ContentDigest | None
    required_checks: Annotated[tuple[CheckId, ...], Field(min_length=1, max_length=32)]
    passed_checks: Annotated[tuple[CheckId, ...], Field(max_length=32)]
    sanitized_error: SanitizedError | None = None

    @model_validator(mode="after")
    def checks_are_unique(self) -> Self:
        if len(set(self.required_checks)) != len(self.required_checks):
            raise ValueError("required probe checks must be unique")
        if len(set(self.passed_checks)) != len(self.passed_checks):
            raise ValueError("passed probe checks must be unique")
        if not set(self.passed_checks).issubset(self.required_checks):
            raise ValueError("passed probe checks must be required checks")
        return self

    @property
    def succeeded(self) -> bool:
        return (
            self.outcome is Outcome.PASSED
            and self.nonce_digest is not None
            and self.passed_checks == self.required_checks
            and self.sanitized_error is None
        )


class CleanupEvidence(_FrozenEvidence):
    """Exact-resource cleanup and worker-reuse decision."""

    attempted: bool
    app_container_created: bool
    app_container_removed: bool
    verifier_container_created: bool
    verifier_container_removed: bool
    workspace_created: bool
    workspace_removed: bool
    run_owned_resources_only: bool
    worker_quarantined: bool
    sanitized_errors: Annotated[tuple[SanitizedError, ...], Field(max_length=32)] = ()

    @model_validator(mode="after")
    def resource_lifecycle_is_coherent(self) -> Self:
        pairs = (
            (self.app_container_created, self.app_container_removed, "app container"),
            (
                self.verifier_container_created,
                self.verifier_container_removed,
                "verifier container",
            ),
            (self.workspace_created, self.workspace_removed, "workspace"),
        )
        for created, removed, resource in pairs:
            if removed and not created:
                raise ValueError(f"{resource} cannot be removed when it was not created")
        if self.verifier_container_created and not self.app_container_created:
            raise ValueError("verifier container requires an app container")
        if self.workspace_created and not self.app_container_created:
            raise ValueError("workspace requires an app container")
        return self

    @property
    def succeeded(self) -> bool:
        return (
            self.attempted
            and (not self.app_container_created or self.app_container_removed)
            and (not self.verifier_container_created or self.verifier_container_removed)
            and (not self.workspace_created or self.workspace_removed)
            and self.run_owned_resources_only
            and not self.worker_quarantined
            and not self.sanitized_errors
        )


class FreshStateEvidence(_FrozenEvidence):
    """Controller assertion about the provenance of mutable run state."""

    workspace_created_for_attempt: bool
    preexisting_workspace_marker_absent: bool
    workspace_marker_digest: ContentDigest
    mutable_state_reused: bool
    shared_mutable_resource_ids: Annotated[tuple[IdentifierText, ...], Field(max_length=32)] = ()
    only_immutable_image_layers_reused: bool

    @property
    def satisfied(self) -> bool:
        return (
            self.workspace_created_for_attempt
            and self.preexisting_workspace_marker_absent
            and not self.mutable_state_reused
            and not self.shared_mutable_resource_ids
            and self.only_immutable_image_layers_reused
        )


RunPhase = Literal["baseline", "proof"]


class AttemptEvidence(_FrozenEvidence):
    """Typed partial evidence retained even when a phase cannot reach its probe."""

    schema_version: Literal[1] = 1
    evidence_source: Literal["trusted_local_controller"] = "trusted_local_controller"
    phase: RunPhase
    run_id: UUID
    attempt_id: UUID
    workspace_id: UUID
    base_commit: GitObjectId
    base_git_tree: GitObjectId
    source_git_tree: GitObjectId
    base_tree_digest: ContentDigest
    base_archive_digest: ContentDigest
    candidate_digest: ContentDigest | None
    candidate_tree_digest: ContentDigest | None
    target_reference: RelativeReference
    target_digest: ContentDigest
    recipe_reference: RelativeReference
    recipe_digest: ContentDigest
    readme_reference: RelativeReference
    readme_digest: ContentDigest
    verifier_id: IdentifierText
    verifier_digest: ContentDigest
    policy_revision: IdentifierText
    policy_digest: ContentDigest
    app_runtime: RuntimeImageEvidence
    verifier_runtime: RuntimeImageEvidence
    app_container_id: ContainerId | None
    verifier_container_id: ContainerId | None
    workspace_marker_digest: ContentDigest | None = None
    commands: Annotated[tuple[CommandEvidence, ...], Field(max_length=13)] = ()
    readiness: ReadinessEvidence | None = None
    acceptance_probe: AcceptanceProbeEvidence | None = None
    cleanup: CleanupEvidence
    outcome: Outcome
    sanitized_errors: Annotated[tuple[SanitizedError, ...], Field(max_length=32)] = ()

    _target_reference_is_relative = field_validator("target_reference")(
        _validate_relative_reference
    )
    _recipe_reference_is_relative = field_validator("recipe_reference")(
        _validate_relative_reference
    )
    _readme_reference_is_relative = field_validator("readme_reference")(
        _validate_relative_reference
    )

    @model_validator(mode="after")
    def binding_and_phase_are_coherent(self) -> Self:
        if len({self.run_id, self.attempt_id, self.workspace_id}) != 3:
            raise ValueError("run, attempt, and workspace IDs must be distinct")
        if self.app_container_id is not None and self.app_container_id == self.verifier_container_id:
            raise ValueError("app and verifier containers must be distinct")
        if self.verifier_container_id is not None and self.app_container_id is None:
            raise ValueError("verifier container requires an app container")
        if self.cleanup.app_container_created != (self.app_container_id is not None):
            raise ValueError("app container identity and cleanup evidence disagree")
        if self.cleanup.verifier_container_created != (self.verifier_container_id is not None):
            raise ValueError("verifier container identity and cleanup evidence disagree")
        if self.workspace_marker_digest is not None and not self.cleanup.workspace_created:
            raise ValueError("workspace marker requires a created workspace")
        if self.readiness is not None and self.verifier_container_id is None:
            raise ValueError("readiness evidence requires a verifier container")
        if self.acceptance_probe is not None and self.verifier_container_id is None:
            raise ValueError("acceptance evidence requires a verifier container")
        if self.phase == "baseline" and (
            self.candidate_digest is not None or self.candidate_tree_digest is not None
        ):
            raise ValueError("baseline attempt cannot have candidate digests")
        if self.phase == "proof" and (
            self.candidate_digest is None or self.candidate_tree_digest is None
        ):
            raise ValueError("proof attempt requires candidate digests")
        return self


class _RunObservationFields(_FrozenEvidence):
    phase: RunPhase
    base_commit: GitObjectId
    base_git_tree: GitObjectId
    source_git_tree: GitObjectId
    base_tree_digest: ContentDigest
    base_archive_digest: ContentDigest
    candidate_digest: ContentDigest | None
    candidate_tree_digest: ContentDigest | None

    target_reference: RelativeReference
    target_digest: ContentDigest
    observed_target_digest: ContentDigest
    recipe_reference: RelativeReference
    recipe_digest: ContentDigest
    executed_recipe_digest: ContentDigest
    readme_reference: RelativeReference
    readme_digest: ContentDigest
    rendered_readme_digest: ContentDigest
    verifier_id: IdentifierText
    verifier_digest: ContentDigest
    observed_verifier_digest: ContentDigest
    policy_revision: IdentifierText
    policy_digest: ContentDigest
    observed_policy_digest: ContentDigest

    app_runtime: RuntimeImageEvidence
    verifier_runtime: RuntimeImageEvidence
    app_container_id: ContainerId
    verifier_container_id: ContainerId
    fresh_state: FreshStateEvidence
    policy_authorized: bool

    commands: Annotated[tuple[CommandEvidence, ...], Field(min_length=2, max_length=13)]
    readiness: ReadinessEvidence
    acceptance_probe: AcceptanceProbeEvidence
    cleanup: CleanupEvidence
    outcome: Outcome
    sanitized_errors: Annotated[tuple[SanitizedError, ...], Field(max_length=32)] = ()
    sanitized_controller_log_tail: SanitizedLogTail = ""

    _target_reference_is_relative = field_validator("target_reference")(
        _validate_relative_reference
    )
    _recipe_reference_is_relative = field_validator("recipe_reference")(
        _validate_relative_reference
    )
    _readme_reference_is_relative = field_validator("readme_reference")(
        _validate_relative_reference
    )

    @model_validator(mode="after")
    def phase_and_commands_are_coherent(self) -> Self:
        if self.phase == "baseline" and self.candidate_digest is not None:
            raise ValueError("baseline evidence cannot have a candidate digest")
        if self.phase == "baseline" and self.candidate_tree_digest is not None:
            raise ValueError("baseline evidence cannot have a candidate tree digest")
        if self.phase == "proof" and (
            self.candidate_digest is None or self.candidate_tree_digest is None
        ):
            raise ValueError("proof evidence requires exact candidate patch and tree digests")

        command_ids = [command.step_id for command in self.commands]
        if len(set(command_ids)) != len(command_ids):
            raise ValueError("command step IDs must be unique")
        if sum(command.kind == "start" for command in self.commands) != 1:
            raise ValueError("run evidence requires exactly one managed start command")
        if not any(command.kind == "foreground" for command in self.commands):
            raise ValueError("run evidence requires at least one foreground command")
        if self.app_container_id == self.verifier_container_id:
            raise ValueError("app and verifier containers must be distinct")
        if not (
            self.cleanup.app_container_created
            and self.cleanup.verifier_container_created
            and self.cleanup.workspace_created
        ):
            raise ValueError("complete run evidence requires all run resources")
        return self


class ControllerBinding(_FrozenEvidence):
    """Opaque identifiers selected by the trusted controller before execution."""

    run_id: UUID
    attempt_id: UUID
    workspace_id: UUID

    @model_validator(mode="after")
    def identifiers_are_distinct(self) -> Self:
        if len({self.run_id, self.attempt_id, self.workspace_id}) != 3:
            raise ValueError("run, attempt, and workspace IDs must be distinct")
        return self


class ControllerObservation(_RunObservationFields):
    """In-process observation captured by the trusted local controller."""

    binding: ControllerBinding


class RunEvidence(_RunObservationFields):
    """Immutable, case-bound evidence exported by the trusted controller."""

    schema_version: Literal[1] = 1
    evidence_source: Literal["trusted_local_controller"]
    run_id: UUID
    attempt_id: UUID
    workspace_id: UUID

    @model_validator(mode="after")
    def identifiers_are_distinct(self) -> Self:
        if len({self.run_id, self.attempt_id, self.workspace_id}) != 3:
            raise ValueError("run, attempt, and workspace IDs must be distinct")
        return self

    def satisfies_verified_predicate(self) -> bool:
        """Derive verification from complete observations, never an input flag."""

        return (
            self.outcome is Outcome.PASSED
            and not self.sanitized_errors
            and self.policy_authorized
            and self.fresh_state.satisfied
            and self.target_digest == self.observed_target_digest
            and self.recipe_digest == self.executed_recipe_digest
            and self.readme_digest == self.rendered_readme_digest
            and self.verifier_digest == self.observed_verifier_digest
            and self.policy_digest == self.observed_policy_digest
            and all(command.succeeded for command in self.commands)
            and self.readiness.succeeded
            and self.acceptance_probe.succeeded
            and self.acceptance_probe.verifier_id == self.verifier_id
            and self.acceptance_probe.verifier_digest == self.verifier_digest
            and self.cleanup.app_container_created
            and self.cleanup.verifier_container_created
            and self.cleanup.workspace_created
            and self.cleanup.succeeded
        )

    @property
    def verified(self) -> bool:
        return self.satisfies_verified_predicate()


class EvidenceBindingError(ValueError):
    """Raised when worker-like observations do not match controller authority."""


def build_run_evidence(
    *,
    binding: ControllerBinding,
    observation: ControllerObservation,
) -> RunEvidence:
    """Bind one trusted observation to the controller's expected attempt.

    A payload with a stale or fabricated run, attempt, or workspace identifier is
    rejected before it can become exported evidence.
    """

    if type(binding) is not ControllerBinding:
        raise TypeError("binding must be a ControllerBinding")
    if type(observation) is not ControllerObservation:
        raise TypeError("observation must be a ControllerObservation")
    if observation.binding != binding:
        raise EvidenceBindingError("observation does not match the controller binding")

    observation_values = {
        name: getattr(observation, name) for name in _RunObservationFields.model_fields
    }
    return RunEvidence(
        **observation_values,
        schema_version=1,
        evidence_source="trusted_local_controller",
        run_id=binding.run_id,
        attempt_id=binding.attempt_id,
        workspace_id=binding.workspace_id,
    )
