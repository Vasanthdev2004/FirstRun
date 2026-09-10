"""Strict target and recipe contracts for the controlled FirstRun lane."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from pydantic import field_validator, model_validator


DEFAULT_MAX_CONTRACT_BYTES = 1_048_576

Identifier = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=40,
        pattern=r"^[a-z][a-z0-9_-]{0,39}$",
    ),
]
Argument = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=2048,
        pattern=r"^[^\x00\r\n]+$",
    ),
]
RelativeCwd = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200)]
StepTimeout = Annotated[int, Field(strict=True, ge=1, le=600)]
ProbeTimeout = Annotated[int, Field(strict=True, ge=1, le=120)]
AppPort = Annotated[int, Field(strict=True, ge=1024, le=65535)]


class ContractFileError(ValueError):
    """A contract file could not be read as bounded, unambiguous JSON."""


class _FrozenContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RecipeStep(_FrozenContractModel):
    id: Identifier
    argv: Annotated[tuple[Argument, ...], Field(min_length=1, max_length=32)]
    cwd: RelativeCwd
    timeout_seconds: StepTimeout

    @field_validator("argv", mode="before")
    @classmethod
    def _freeze_argv(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return tuple(value)
        return value

    @field_validator("cwd")
    @classmethod
    def _validate_cwd(cls, value: str) -> str:
        if "\\" in value or ":" in value or any(char in value for char in "\x00\r\n"):
            raise ValueError("cwd contains an unsupported character")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or str(path) != value:
            raise ValueError("cwd must be normalized, relative, and contained")
        return value


class Recipe(_FrozenContractModel):
    version: Literal[1]
    steps: Annotated[tuple[RecipeStep, ...], Field(min_length=1, max_length=12)]
    start: RecipeStep

    @field_validator("version", mode="before")
    @classmethod
    def _version_is_an_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("version must be the integer 1")
        return value

    @field_validator("steps", mode="before")
    @classmethod
    def _freeze_steps(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return tuple(value)
        return value

    @model_validator(mode="after")
    def _step_ids_are_unique(self) -> "Recipe":
        identifiers = [step.id for step in self.steps]
        identifiers.append(self.start.id)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("recipe step IDs must be unique")
        return self


class TargetRuntime(_FrozenContractModel):
    image_ref: Annotated[str, StringConstraints(strict=True, min_length=1)]
    platform: Literal["linux/amd64", "linux/arm64"]


class TargetReadiness(_FrozenContractModel):
    path: Literal["/health"]
    status: Literal[200]
    timeout_seconds: ProbeTimeout

    @field_validator("status", mode="before")
    @classmethod
    def _status_is_an_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("readiness status must be the integer 200")
        return value


class TargetAcceptance(_FrozenContractModel):
    verifier_id: Literal["notes-create-read-v1"]
    timeout_seconds: ProbeTimeout


class Target(_FrozenContractModel):
    version: Literal[1]
    runtime: TargetRuntime
    network: Literal["none"]
    app_port: AppPort
    readiness: TargetReadiness
    acceptance: TargetAcceptance

    @field_validator("version", mode="before")
    @classmethod
    def _version_is_an_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("version must be the integer 1")
        return value


ContractModel = TypeVar("ContractModel", bound=BaseModel)


def sha256_content_ref(content: bytes) -> str:
    """Return a stable reference to the exact bytes supplied by the controller."""

    if type(content) is not bytes:
        raise TypeError("content must be bytes")
    return "sha256:" + hashlib.sha256(content).hexdigest()


def load_contract(
    path: str | os.PathLike[str],
    model_type: type[ContractModel],
    *,
    max_bytes: int = DEFAULT_MAX_CONTRACT_BYTES,
) -> ContractModel:
    """Load one regular, non-symlink UTF-8 JSON file into a strict model."""

    if not isinstance(model_type, type) or not issubclass(model_type, BaseModel):
        raise TypeError("model_type must be a BaseModel subclass")
    raw = _read_bounded_regular_file(path, max_bytes=max_bytes)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractFileError("contract is not valid UTF-8") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise ContractFileError("contract is not valid JSON") from exc
    return model_type.model_validate(value)


def load_recipe(
    path: str | os.PathLike[str],
    *,
    max_bytes: int = DEFAULT_MAX_CONTRACT_BYTES,
) -> Recipe:
    return load_contract(path, Recipe, max_bytes=max_bytes)


def load_target(
    path: str | os.PathLike[str],
    *,
    max_bytes: int = DEFAULT_MAX_CONTRACT_BYTES,
) -> Target:
    return load_contract(path, Target, max_bytes=max_bytes)


def _read_bounded_regular_file(
    path: str | os.PathLike[str], *, max_bytes: int
) -> bytes:
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    candidate = Path(path)
    try:
        before = os.lstat(candidate)
    except OSError as exc:
        raise ContractFileError("contract file could not be inspected") from exc
    if stat.S_ISLNK(before.st_mode):
        raise ContractFileError("contract file must not be a symlink")
    if not stat.S_ISREG(before.st_mode):
        raise ContractFileError("contract path must be a regular file")
    if before.st_size > max_bytes:
        raise ContractFileError("contract file exceeds the byte limit")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise ContractFileError("contract file could not be opened safely") from exc
    try:
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise ContractFileError("opened contract is not a regular file")
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise ContractFileError("contract file changed while it was opened")
            raw = stream.read(max_bytes + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(raw) > max_bytes:
        raise ContractFileError("contract file exceeds the byte limit")
    return raw


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ContractFileError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ContractFileError(f"unsupported JSON constant: {value}")


__all__ = [
    "DEFAULT_MAX_CONTRACT_BYTES",
    "ContractFileError",
    "RecipeStep",
    "Recipe",
    "TargetRuntime",
    "TargetReadiness",
    "TargetAcceptance",
    "Target",
    "load_contract",
    "load_recipe",
    "load_target",
    "sha256_content_ref",
]
