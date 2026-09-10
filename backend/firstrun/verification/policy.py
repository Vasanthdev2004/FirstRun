"""Versioned policy for FirstRun's single controlled M1 Node fixture lane."""

from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from firstrun.domain.evidence import ContentDigest
from firstrun.domain.outcomes import Outcome


RelativePath = Annotated[str, StringConstraints(min_length=1, max_length=256, strict=True)]
CommandArgument = Annotated[str, StringConstraints(min_length=1, max_length=2048, strict=True)]


class _FrozenPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _relative_path(value: str) -> str:
    if "\\" in value or ":" in value or any(character in value for character in "\x00\r\n"):
        raise ValueError("policy paths must be unambiguous POSIX-relative paths")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or ".." in parsed.parts or str(parsed) != value:
        raise ValueError("policy paths must be normalized and relative")
    return value


class ApprovedCommand(_FrozenPolicy):
    """One exact command approved for the checked-in baseline recipe."""

    step_id: Annotated[
        str,
        StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,39}$", strict=True),
    ]
    argv: Annotated[tuple[CommandArgument, ...], Field(min_length=1, max_length=32)]
    cwd: RelativePath
    timeout_seconds: Annotated[int, Field(ge=1, le=600)]

    _cwd_is_relative = field_validator("cwd")(_relative_path)


class DockerLimits(_FrozenPolicy):
    """Explicit resource and isolation limits inherited from the proved M0 lane."""

    user: Literal["65534:65534"] = "65534:65534"
    memory_bytes: Literal[134_217_728] = 134_217_728
    memory_swap_bytes: Literal[134_217_728] = 134_217_728
    nano_cpus: Literal[250_000_000] = 250_000_000
    pids: Literal[64] = 64
    workspace_tmpfs_bytes: Literal[16_777_216] = 16_777_216
    tmp_tmpfs_bytes: Literal[16_777_216] = 16_777_216
    shm_bytes: Literal[16_777_216] = 16_777_216
    max_output_bytes: Literal[262_144] = 262_144
    max_log_bytes: Literal[1_048_576] = 1_048_576
    wall_time_seconds: Literal[120] = 120
    stop_timeout_seconds: Literal[2] = 2
    network_mode: Literal["none"] = "none"
    cap_drop: Literal["ALL"] = "ALL"
    no_new_privileges: Literal[True] = True
    seccomp_profile: Literal["builtin"] = "builtin"
    read_only_root: Literal[True] = True


class ControlledNodeFixturePolicy(_FrozenPolicy):
    """The sole M1 source/runtime/verifier registration.

    This records the intentionally broken baseline recipe.  It deliberately does
    not encode the known migration oracle; candidate authorization is a separate
    semantic check performed by the trusted controller.
    """

    schema_version: Literal[1] = 1
    policy_id: Literal["controlled-node-fixture-v1"] = "controlled-node-fixture-v1"
    revision: Literal["m1-policy-v1"] = "m1-policy-v1"
    fixture_path: Literal["fixtures/notes-app"] = "fixtures/notes-app"
    target_path: Literal[".firstrun/target.json"] = ".firstrun/target.json"
    recipe_path: Literal[".firstrun/recipe.json"] = ".firstrun/recipe.json"
    readme_path: Literal["README.md"] = "README.md"
    runtime_image_reference: Literal["node:22-bookworm-slim"] = "node:22-bookworm-slim"
    platform: Literal["linux/amd64"] = "linux/amd64"
    verifier_id: Literal["notes-create-read-v1"] = "notes-create-read-v1"
    baseline_commands: Annotated[tuple[ApprovedCommand, ...], Field(min_length=1, max_length=12)]
    start_command: ApprovedCommand
    docker: DockerLimits = DockerLimits()

    _fixture_path_is_relative = field_validator("fixture_path")(_relative_path)
    _target_path_is_relative = field_validator("target_path")(_relative_path)
    _recipe_path_is_relative = field_validator("recipe_path")(_relative_path)
    _readme_path_is_relative = field_validator("readme_path")(_relative_path)

    @model_validator(mode="after")
    def approved_commands_are_exact(self) -> ControlledNodeFixturePolicy:
        baseline = tuple(
            (command.step_id, command.argv, command.cwd, command.timeout_seconds)
            for command in self.baseline_commands
        )
        if baseline != (
            (
                "install",
                ("npm", "ci", "--offline", "--no-audit", "--no-fund"),
                ".",
                60,
            ),
        ):
            raise ValueError("baseline commands do not match the registered fixture")
        start = (
            self.start_command.step_id,
            self.start_command.argv,
            self.start_command.cwd,
            self.start_command.timeout_seconds,
        )
        if start != ("start", ("npm", "run", "dev"), ".", 60):
            raise ValueError("start command does not match the registered fixture")
        return self


CONTROLLED_NODE_FIXTURE_POLICY = ControlledNodeFixturePolicy(
    baseline_commands=(
        ApprovedCommand(
            step_id="install",
            argv=("npm", "ci", "--offline", "--no-audit", "--no-fund"),
            cwd=".",
            timeout_seconds=60,
        ),
    ),
    start_command=ApprovedCommand(
        step_id="start",
        argv=("npm", "run", "dev"),
        cwd=".",
        timeout_seconds=60,
    ),
)


def canonical_policy_json(policy: ControlledNodeFixturePolicy) -> bytes:
    """Return stable UTF-8 JSON bytes used as the policy's provenance input."""

    value = json.dumps(
        policy.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return value.encode("utf-8")


def digest_policy(policy: ControlledNodeFixturePolicy) -> ContentDigest:
    """Digest the exact canonical policy document used by the controller."""

    return ContentDigest.from_bytes(canonical_policy_json(policy))


CONTROLLED_NODE_FIXTURE_POLICY_DIGEST = digest_policy(CONTROLLED_NODE_FIXTURE_POLICY)


def classify_target_tuple(
    *,
    image_reference: str,
    platform: str,
    verifier_id: str,
    policy: ControlledNodeFixturePolicy = CONTROLLED_NODE_FIXTURE_POLICY,
) -> Outcome:
    """Classify a schema-valid target tuple against the registered M1 lane."""

    if (
        image_reference == policy.runtime_image_reference
        and platform == policy.platform
        and verifier_id == policy.verifier_id
    ):
        return Outcome.PASSED
    return Outcome.UNSUPPORTED
