"""Authenticated GitHub push ingress for one explicitly registered repository."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
from collections.abc import Mapping
from typing import Any

from firstrun.github_state import GitHubConfig, SQLiteStore


MAX_WEBHOOK_BYTES = 512 * 1024
MAX_WEBHOOK_SECRET_BYTES = 4_096
_SIGNATURE = re.compile(r"^sha256=([0-9a-f]{64})$")
_DELIVERY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,127}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")


class WebhookError(ValueError):
    """A webhook failed authentication, authorization, or strict decoding."""


def ingest_signed_push(
    raw_body: bytes,
    headers: Mapping[str, str],
    config: GitHubConfig,
    store: SQLiteStore,
) -> dict[str, object]:
    """Authenticate raw bytes, authorize the push, and durably enqueue it.

    Signature verification deliberately precedes JSON decoding.  The raw body and
    webhook secret are never passed to the store or included in the result.
    """

    if type(raw_body) is not bytes or not 1 <= len(raw_body) <= MAX_WEBHOOK_BYTES:
        raise WebhookError("webhook body is missing or exceeds its limit")
    normalized = _headers(headers)
    supplied = normalized.get("x-hub-signature-256", "")
    match = _SIGNATURE.fullmatch(supplied)
    if match is None:
        raise WebhookError("webhook signature is invalid")
    secret = _read_secret(config.webhook_secret_path)
    expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(match.group(1), expected):
        raise WebhookError("webhook signature is invalid")

    # Everything below this line may inspect the authenticated body.
    event = normalized.get("x-github-event")
    delivery_id = normalized.get("x-github-delivery", "")
    if event != "push" or _DELIVERY.fullmatch(delivery_id) is None:
        raise WebhookError("webhook event or delivery identity is unsupported")
    payload = _decode_object(raw_body)
    registration = config.registration

    installation = _object(payload, "installation")
    repository = _object(payload, "repository")
    repository_owner = _object(repository, "owner")
    installation_id = _integer(installation, "id")
    repository_id = _integer(repository, "id")
    sha = _text(payload, "after")
    if _SHA.fullmatch(sha) is None or sha == "0" * 40:
        raise WebhookError("push SHA is invalid")
    expected_full_name = f"{registration.owner}/{registration.name}"
    if (
        installation_id != registration.installation_id
        or repository_id != registration.repository_id
        or _text(repository, "name") != registration.name
        or _text(repository, "full_name") != expected_full_name
        or _text(repository_owner, "login") != registration.owner
        or repository.get("fork") is not False
        or payload.get("deleted") is not False
        or _text(payload, "ref") != f"refs/heads/{registration.branch}"
    ):
        raise WebhookError("push is not authorized for the registered repository")

    result = store.ingest_delivery(
        "github", delivery_id, "push", registration, sha
    ).to_dict()
    result["event"] = "push"
    return result


def _headers(headers: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(headers, Mapping):
        raise WebhookError("webhook headers are invalid")
    normalized: dict[str, str] = {}
    for raw_name, raw_value in headers.items():
        if not isinstance(raw_name, str) or not isinstance(raw_value, str):
            raise WebhookError("webhook headers are invalid")
        name = raw_name.lower()
        if name in normalized:
            raise WebhookError("webhook headers are ambiguous")
        normalized[name] = raw_value
    return normalized


def _decode_object(raw_body: bytes) -> dict[str, Any]:
    try:
        text = raw_body.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise WebhookError("authenticated webhook body is invalid JSON") from exc
    if not isinstance(value, dict):
        raise WebhookError("authenticated webhook body must be an object")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise WebhookError("authenticated webhook body has a duplicate key")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise WebhookError(f"authenticated webhook body has unsupported constant {value}")


def _object(parent: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise WebhookError("authenticated webhook body has an invalid object")
    return value


def _text(parent: Mapping[str, Any], key: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value or any(
        character in value for character in "\x00\r\n"
    ):
        raise WebhookError("authenticated webhook body has invalid text")
    return value


def _integer(parent: Mapping[str, Any], key: str) -> int:
    value = parent.get(key)
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        raise WebhookError("authenticated webhook body has an invalid identifier")
    return value


def _read_secret(path: os.PathLike[str]) -> bytes:
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise WebhookError("webhook secret is unavailable") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise WebhookError("webhook secret is unavailable")
    if not 1 <= before.st_size <= MAX_WEBHOOK_SECRET_BYTES:
        raise WebhookError("webhook secret is unavailable")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise WebhookError("webhook secret changed while opening")
        secret = os.read(descriptor, MAX_WEBHOOK_SECRET_BYTES + 1)
    except OSError as exc:
        raise WebhookError("webhook secret is unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if (
        not 1 <= len(secret) <= MAX_WEBHOOK_SECRET_BYTES
        or any(character in secret for character in (0, 10, 13))
    ):
        raise WebhookError("webhook secret is unavailable")
    return secret


__all__ = ["MAX_WEBHOOK_BYTES", "WebhookError", "ingest_signed_push"]
