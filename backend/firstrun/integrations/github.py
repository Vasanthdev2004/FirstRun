"""Least-privilege GitHub App REST client and exact-source transport.

This module is trusted controller code.  Tokens and private-key bytes never enter a
repository archive, response object, exception message, or log.  The concrete
transport connects only to ``api.github.com`` and does not follow redirects.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import http.client
import json
import os
import re
import ssl
import stat
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import quote

from firstrun.verification.policy import CONTROLLED_NODE_FIXTURE_POLICY
from firstrun.verification.source import (
    ALLOWED_CANDIDATE_PATHS,
    MAX_ARCHIVE_BYTES,
    MAX_FILE_BYTES,
    MAX_SOURCE_BYTES,
    MAX_SOURCE_FILES,
    SourceInfrastructureError,
    SourcePolicyError,
    SourceSnapshot,
    _build_canonical_archive,
    _content_ref,
    _read_safe_archive,
    _validate_text,
    capture_approved_source,
    content_tree_digest,
    default_repository_root,
)


API_HOST = "api.github.com"
API_PORT = 443
API_VERSION = "2026-03-10"
USER_AGENT = "FirstRun/0.1"
INSTALLATION_PERMISSIONS = MappingProxyType(
    {"checks": "write", "contents": "write", "pull_requests": "write"}
)

MAX_REQUEST_BYTES = 1_048_576
MAX_PRIVATE_KEY_BYTES = 131_072
MAX_TREE_DEPTH = 12
MAX_TREE_OBJECTS = 64
MAX_TREE_ENTRIES = 2_048

_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_SAFE_METHODS = frozenset({"GET"})
_METHODS = frozenset({"GET", "POST", "PATCH", "PUT", "DELETE"})


class GitHubError(RuntimeError):
    """Sanitized GitHub integration failure.

    ``uncertain`` means a mutating request may have reached GitHub and must be
    reconciled with a read before another write is attempted.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        uncertain: bool = False,
    ) -> None:
        super().__init__(message[:512])
        self.code = code
        self.status = status
        self.uncertain = uncertain


@dataclass(frozen=True, slots=True)
class TransportResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class GitHubTransport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> TransportResponse:
        ...


class StdlibGitHubTransport:
    """Small fixed-origin HTTPS transport; redirects remain ordinary errors."""

    def request(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> TransportResponse:
        _validate_api_path(path)
        connection = http.client.HTTPSConnection(
            API_HOST,
            API_PORT,
            timeout=timeout_seconds,
            context=ssl.create_default_context(),
        )
        try:
            connection.request(method, path, body=body, headers=dict(headers))
            response = connection.getresponse()
            content = response.read(max_response_bytes + 1)
            if len(content) > max_response_bytes:
                raise GitHubError(
                    "response_too_large",
                    "GitHub returned a response larger than the configured limit.",
                    status=response.status,
                    uncertain=method not in _SAFE_METHODS,
                )
            response_headers = MappingProxyType(
                {name.lower(): value[:2048] for name, value in response.getheaders()}
            )
            return TransportResponse(response.status, response_headers, content)
        except GitHubError:
            raise
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            raise GitHubError(
                "network_error",
                "GitHub could not be reached through the fixed HTTPS transport.",
                uncertain=method not in _SAFE_METHODS,
            ) from exc
        finally:
            connection.close()


@dataclass(frozen=True, slots=True)
class GitHubAppConfig:
    app_id: int
    installation_id: int
    repository_id: int
    owner: str
    name: str
    private_key_path: Path
    timeout_seconds: float = 10.0
    max_response_bytes: int = 2 * 1024 * 1024

    def __post_init__(self) -> None:
        for field_name in ("app_id", "installation_id", "repository_id"):
            value = getattr(self, field_name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")
        if not isinstance(self.owner, str) or _OWNER.fullmatch(self.owner) is None:
            raise ValueError("owner is not a supported GitHub login")
        if not isinstance(self.name, str) or _REPOSITORY.fullmatch(self.name) is None:
            raise ValueError("name is not a supported GitHub repository name")
        if not isinstance(self.private_key_path, Path):
            raise ValueError("private_key_path must be a pathlib.Path")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not 1 <= float(self.timeout_seconds) <= 30
        ):
            raise ValueError("timeout_seconds must be between one and 30")
        if (
            type(self.max_response_bytes) is not int
            or not 1024 <= self.max_response_bytes <= 4 * 1024 * 1024
        ):
            raise ValueError("max_response_bytes is outside the supported range")


class GitHubClient:
    """One-installation, one-repository GitHub App client."""

    def __init__(
        self,
        config: GitHubAppConfig | None = None,
        *,
        app_id: int | None = None,
        installation_id: int | None = None,
        repository_id: int | None = None,
        owner: str | None = None,
        name: str | None = None,
        private_key_path: Path | None = None,
        timeout_seconds: float = 10.0,
        max_response_bytes: int = 2 * 1024 * 1024,
        transport: GitHubTransport | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        primitive_values = (
            app_id,
            installation_id,
            repository_id,
            owner,
            name,
            private_key_path,
        )
        if config is not None and any(value is not None for value in primitive_values):
            raise ValueError("use either GitHubAppConfig or primitive configuration fields")
        if config is None:
            if any(value is None for value in primitive_values):
                raise ValueError("all primitive GitHub App configuration fields are required")
            config = GitHubAppConfig(
                app_id=app_id,  # type: ignore[arg-type]
                installation_id=installation_id,  # type: ignore[arg-type]
                repository_id=repository_id,  # type: ignore[arg-type]
                owner=owner,  # type: ignore[arg-type]
                name=name,  # type: ignore[arg-type]
                private_key_path=private_key_path,  # type: ignore[arg-type]
                timeout_seconds=timeout_seconds,
                max_response_bytes=max_response_bytes,
            )
        self.config = config
        self.transport = transport or StdlibGitHubTransport()
        self._clock = clock or time.time
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._repository_authorized = False

    @property
    def prefix(self) -> str:
        return f"/repos/{quote(self.config.owner, safe='')}/{quote(self.config.name, safe='')}"

    def request(
        self, method: str, path: str, body: Mapping[str, object] | None = None
    ) -> Any:
        """Call one configured-repository endpoint with an installation token."""

        method = _validate_method(method)
        _validate_repository_path(path, self.prefix)
        token = self._installation_token()
        return self._request_with_token(method, path, body, token)

    def ensure_repository_authorized(self) -> None:
        """Read back repository identity through the exactly-scoped token."""

        if self._repository_authorized:
            return
        value = self.request("GET", self.prefix)
        if not isinstance(value, dict):
            raise GitHubError("invalid_response", "GitHub repository metadata was invalid.")
        expected_full_name = f"{self.config.owner}/{self.config.name}"
        if (
            value.get("id") != self.config.repository_id
            or value.get("fork") is not False
            or not isinstance(value.get("full_name"), str)
            or value["full_name"].casefold() != expected_full_name.casefold()
        ):
            raise GitHubError(
                "repository_not_authorized",
                "GitHub repository identity did not match the approved registration.",
            )
        self._repository_authorized = True

    def head(self, branch: str) -> str:
        if (
            not isinstance(branch, str)
            or not 1 <= len(branch) <= 255
            or any(ord(character) < 32 or ord(character) == 127 for character in branch)
        ):
            raise GitHubError("invalid_branch", "Branch name is not a bounded Git reference.")
        self.ensure_repository_authorized()
        value = self.request(
            "GET", f"{self.prefix}/git/ref/heads/{quote(branch, safe='')}"
        )
        try:
            object_value = value["object"]
            sha = object_value["sha"]
            object_type = object_value["type"]
        except (KeyError, TypeError) as exc:
            raise GitHubError("invalid_response", "GitHub branch response was invalid.") from exc
        if object_type != "commit" or not _is_git_sha(sha):
            raise GitHubError("invalid_response", "GitHub branch did not resolve to a commit.")
        return sha

    def _installation_token(self) -> str:
        now = float(self._clock())
        if self._token is not None and now + 60 < self._token_expires_at:
            return self._token
        request_body = {
            "repository_ids": [self.config.repository_id],
            "permissions": dict(INSTALLATION_PERMISSIONS),
        }
        path = f"/app/installations/{self.config.installation_id}/access_tokens"
        value = self._request_with_token("POST", path, request_body, self._app_jwt())
        token, expiry = self._validate_token_response(value, now)
        self._token = token
        self._token_expires_at = expiry
        return token

    def _validate_token_response(self, value: object, now: float) -> tuple[str, float]:
        if not isinstance(value, dict):
            raise GitHubError("invalid_token_response", "GitHub token response was invalid.")
        token = value.get("token")
        expires_at = value.get("expires_at")
        permissions = value.get("permissions")
        repositories = value.get("repositories")
        allowed_response_permissions = [
            dict(INSTALLATION_PERMISSIONS),
            {**INSTALLATION_PERMISSIONS, "metadata": "read"},
        ]
        if (
            not isinstance(token, str)
            or not 8 <= len(token) <= 8192
            or any(character.isspace() or ord(character) < 32 for character in token)
            or not isinstance(expires_at, str)
            or permissions not in allowed_response_permissions
            or value.get("repository_selection") != "selected"
            or not isinstance(repositories, list)
            or len(repositories) != 1
            or not isinstance(repositories[0], dict)
            or repositories[0].get("id") != self.config.repository_id
        ):
            raise GitHubError(
                "invalid_token_response",
                "GitHub did not confirm the exact repository and permission scope.",
            )
        try:
            parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError
            expiry = parsed.astimezone(timezone.utc).timestamp()
        except (ValueError, OverflowError) as exc:
            raise GitHubError("invalid_token_response", "GitHub token expiry was invalid.") from exc
        if not now + 60 < expiry <= now + 3700:
            raise GitHubError(
                "invalid_token_response", "GitHub token lifetime was outside the safe bound."
            )
        return token, expiry

    def _app_jwt(self) -> str:
        try:
            import jwt
        except ImportError as exc:  # optional integration dependency
            raise GitHubError(
                "dependency_missing", "The locked GitHub App signing dependency is unavailable."
            ) from exc
        private_key = _read_private_key(self.config.private_key_path)
        now = int(self._clock())
        try:
            encoded = jwt.encode(
                {"iat": now - 60, "exp": now + 540, "iss": str(self.config.app_id)},
                private_key,
                algorithm="RS256",
            )
        except Exception as exc:
            raise GitHubError("jwt_signing_failed", "GitHub App JWT signing failed.") from exc
        if isinstance(encoded, bytes):
            encoded = encoded.decode("ascii", errors="strict")
        if not isinstance(encoded, str) or not 16 <= len(encoded) <= 8192:
            raise GitHubError("jwt_signing_failed", "GitHub App JWT signing failed.")
        return encoded

    def _request_with_token(
        self,
        method: str,
        path: str,
        body: Mapping[str, object] | None,
        token: str,
    ) -> Any:
        method = _validate_method(method)
        _validate_api_path(path)
        if method == "GET" and body is not None:
            raise GitHubError("invalid_request", "GET requests cannot include a JSON body.")
        encoded_body = None if body is None else _canonical_json(body)
        if encoded_body is not None and len(encoded_body) > MAX_REQUEST_BYTES:
            raise GitHubError("request_too_large", "GitHub request exceeded its byte limit.")
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": API_VERSION,
        }
        if encoded_body is not None:
            headers["Content-Type"] = "application/json"
        try:
            response = self.transport.request(
                method,
                path,
                headers,
                encoded_body,
                timeout_seconds=float(self.config.timeout_seconds),
                max_response_bytes=self.config.max_response_bytes,
            )
        except GitHubError:
            raise
        except Exception as exc:
            raise GitHubError(
                "network_error",
                "GitHub request failed before a bounded response was available.",
                uncertain=method not in _SAFE_METHODS,
            ) from exc
        if not isinstance(response, TransportResponse):
            raise GitHubError("invalid_transport", "GitHub transport returned an invalid response.")
        if not 200 <= response.status < 300:
            raise GitHubError(
                "github_http_error",
                "GitHub rejected the request.",
                status=response.status,
                uncertain=method not in _SAFE_METHODS and response.status >= 500,
            )
        if not response.body:
            return None
        try:
            return json.loads(
                response.body.decode("utf-8", errors="strict"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise GitHubError(
                "invalid_response", "GitHub returned malformed JSON.", status=response.status
            ) from exc


def fetch_snapshot(
    client: GitHubClient,
    sha: str,
    source_prefix: str = "",
    controller_root: Path | None = None,
) -> SourceSnapshot:
    """Fetch one exact approved commit subtree as a credential-free snapshot."""

    commit_sha = _require_git_sha(sha, "commit")
    prefix = _validate_source_prefix(source_prefix)
    client.ensure_repository_authorized()

    commit = client.request("GET", f"{client.prefix}/git/commits/{commit_sha}")
    if not isinstance(commit, dict) or commit.get("sha") != commit_sha:
        raise GitHubError("source_integrity_error", "GitHub commit identity did not match.")
    tree_value = commit.get("tree")
    base_tree = tree_value.get("sha") if isinstance(tree_value, dict) else None
    if not _is_git_sha(base_tree):
        raise GitHubError("invalid_response", "GitHub commit tree was invalid.")

    source_tree = _resolve_source_tree(client, base_tree, prefix)
    remote_entries = _walk_source_tree(client, source_tree)
    local_snapshot = _controlled_local_snapshot(controller_root)
    if set(remote_entries) != set(local_snapshot.files):
        raise GitHubError(
            "source_not_approved",
            "Remote source paths did not match the controller-approved fixture allowlist.",
        )

    files: dict[str, bytes] = {}
    total_bytes = 0
    for path in sorted(remote_entries):
        object_id, declared_size = remote_entries[path]
        content = _fetch_blob(client, object_id, declared_size)
        try:
            _validate_text(path, content)
        except SourcePolicyError as exc:
            raise GitHubError(
                "source_not_supported", "Remote source contained unsupported file content."
            ) from exc
        files[path] = content
        total_bytes += len(content)
        if total_bytes > MAX_SOURCE_BYTES:
            raise GitHubError("source_too_large", "Remote source exceeded its byte limit.")

    if protected_digest(files) != protected_digest(local_snapshot.files):
        raise GitHubError(
            "source_not_approved",
            "Remote protected source bytes did not match the controller-approved fixture.",
        )
    archive_entries = {path: ("100644", remote_entries[path][0]) for path in files}
    archive = _build_canonical_archive(files, archive_entries)
    if not archive or len(archive) > MAX_ARCHIVE_BYTES:
        raise GitHubError("source_too_large", "Remote source archive exceeded its byte limit.")
    try:
        reread = _read_safe_archive(archive)
    except (SourcePolicyError, OSError) as exc:
        raise GitHubError("source_integrity_error", "Remote source archive validation failed.") from exc
    if reread != files:
        raise GitHubError("source_integrity_error", "Remote source archive changed exact bytes.")
    return SourceSnapshot(
        repository_root=local_snapshot.repository_root,
        approved_repo_path=local_snapshot.approved_repo_path,
        base_commit=commit_sha,
        base_tree=base_tree,
        source_tree=source_tree,
        archive_digest=_content_ref(archive),
        content_tree_digest=content_tree_digest(files),
        files=MappingProxyType(files),
    )


def protected_digest(files: Mapping[str, bytes]) -> str:
    """Digest every immutable source path, excluding the two repairable files."""

    protected: dict[str, bytes] = {}
    for path, content in files.items():
        if path in ALLOWED_CANDIDATE_PATHS:
            continue
        if not isinstance(path, str) or not isinstance(content, bytes):
            raise TypeError("protected source must map paths to bytes")
        protected[path] = content
    return content_tree_digest(protected)


def _resolve_source_tree(client: GitHubClient, base_tree: str, prefix: str) -> str:
    current = base_tree
    if not prefix:
        return current
    for component in PurePosixPath(prefix).parts:
        entries = _tree_entries(client, current)
        matches = [entry for entry in entries if entry.get("path") == component]
        if len(matches) != 1:
            raise GitHubError("source_not_found", "Approved source prefix was not found.")
        entry = matches[0]
        child_sha = entry.get("sha")
        if entry.get("mode") != "040000" or entry.get("type") != "tree" or not _is_git_sha(
            child_sha
        ):
            raise GitHubError("source_not_supported", "Approved source prefix was not a tree.")
        current = child_sha
    return current


def _walk_source_tree(client: GitHubClient, source_tree: str) -> dict[str, tuple[str, int]]:
    files: dict[str, tuple[str, int]] = {}
    stack: list[tuple[str, str, int]] = [(source_tree, "", 0)]
    tree_objects = 0
    total_entries = 0
    while stack:
        tree_sha, parent, depth = stack.pop()
        if depth > MAX_TREE_DEPTH:
            raise GitHubError("source_too_deep", "Remote source tree exceeded its depth limit.")
        tree_objects += 1
        if tree_objects > MAX_TREE_OBJECTS:
            raise GitHubError("source_too_large", "Remote source contained too many trees.")
        entries = _tree_entries(client, tree_sha)
        total_entries += len(entries)
        if total_entries > MAX_TREE_ENTRIES:
            raise GitHubError("source_too_large", "Remote source contained too many entries.")
        for entry in entries:
            name = _tree_component(entry.get("path"))
            path = f"{parent}/{name}" if parent else name
            mode = entry.get("mode")
            object_type = entry.get("type")
            object_id = entry.get("sha")
            if not _is_git_sha(object_id):
                raise GitHubError("invalid_response", "GitHub tree entry identity was invalid.")
            if mode == "040000" and object_type == "tree":
                stack.append((object_id, path, depth + 1))
                continue
            if mode != "100644" or object_type != "blob":
                raise GitHubError(
                    "source_not_supported",
                    "Remote source contained a link, submodule, or executable entry.",
                )
            size = entry.get("size")
            if type(size) is not int or not 0 <= size <= MAX_FILE_BYTES:
                raise GitHubError("source_too_large", "Remote source file size was invalid.")
            if path in files:
                raise GitHubError("source_integrity_error", "Remote source path was duplicated.")
            files[path] = (object_id, size)
            if len(files) > MAX_SOURCE_FILES:
                raise GitHubError("source_too_large", "Remote source contained too many files.")
    return files


def _tree_entries(client: GitHubClient, tree_sha: str) -> list[dict[str, object]]:
    value = client.request("GET", f"{client.prefix}/git/trees/{tree_sha}")
    if (
        not isinstance(value, dict)
        or value.get("sha") != tree_sha
        or value.get("truncated") is not False
        or not isinstance(value.get("tree"), list)
        or any(not isinstance(entry, dict) for entry in value["tree"])
    ):
        raise GitHubError("invalid_response", "GitHub returned an invalid or truncated tree.")
    entries = value["tree"]
    if _git_tree_sha(entries) != tree_sha:
        raise GitHubError(
            "source_integrity_error", "GitHub tree entries did not match their Git ID."
        )
    return entries


def _fetch_blob(client: GitHubClient, object_id: str, declared_size: int) -> bytes:
    value = client.request("GET", f"{client.prefix}/git/blobs/{object_id}")
    if not isinstance(value, dict) or value.get("sha") != object_id:
        raise GitHubError("source_integrity_error", "GitHub blob identity did not match.")
    encoded = value.get("content")
    if value.get("encoding") != "base64" or not isinstance(encoded, str):
        raise GitHubError("invalid_response", "GitHub blob encoding was unsupported.")
    compact = "".join(encoded.split())
    try:
        content = base64.b64decode(compact, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise GitHubError("invalid_response", "GitHub blob base64 was invalid.") from exc
    if (
        len(content) != declared_size
        or value.get("size") != declared_size
        or _git_blob_sha(content) != object_id
    ):
        raise GitHubError("source_integrity_error", "GitHub blob bytes did not match their Git ID.")
    return content


def _controlled_local_snapshot(controller_root: Path | None) -> SourceSnapshot:
    try:
        root = (controller_root or default_repository_root()).resolve(strict=True)
        approved = (root / CONTROLLED_NODE_FIXTURE_POLICY.fixture_path).absolute()
        return capture_approved_source(approved, approved_repo=approved)
    except (OSError, SourceInfrastructureError, SourcePolicyError) as exc:
        raise GitHubError(
            "trusted_source_unavailable", "Controller-approved source could not be captured."
        ) from exc


def _read_private_key(path: Path) -> bytes:
    try:
        before = os.lstat(path)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise OSError
        if before.st_size <= 0 or before.st_size > MAX_PRIVATE_KEY_BYTES:
            raise OSError
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or (
                before.st_dev,
                before.st_ino,
            ) != (opened.st_dev, opened.st_ino):
                raise OSError
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                content = stream.read(MAX_PRIVATE_KEY_BYTES + 1)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    except OSError as exc:
        raise GitHubError("private_key_unavailable", "GitHub App private key is unavailable.") from exc
    if not content or len(content) > MAX_PRIVATE_KEY_BYTES:
        raise GitHubError("private_key_unavailable", "GitHub App private key is unavailable.")
    return content


def _validate_method(method: str) -> str:
    if not isinstance(method, str) or method.upper() not in _METHODS or method != method.upper():
        raise GitHubError("invalid_request", "GitHub method is not allowed.")
    return method


def _validate_api_path(path: str) -> None:
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or path.startswith("//")
        or len(path) > 4096
        or "\\" in path
        or "#" in path
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        raise GitHubError("invalid_request", "GitHub API path is invalid.")


def _validate_repository_path(path: str, prefix: str) -> None:
    _validate_api_path(path)
    resource = path.split("?", 1)[0]
    if resource != prefix and not resource.startswith(prefix + "/"):
        raise GitHubError("repository_scope_violation", "GitHub path left the configured repository.")


def _validate_source_prefix(value: str) -> str:
    if value == "":
        return ""
    if (
        not isinstance(value, str)
        or len(value) > 240
        or "\\" in value
        or ":" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise GitHubError("invalid_source_prefix", "Source prefix is invalid.")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or ".." in parsed.parts or str(parsed) != value:
        raise GitHubError("invalid_source_prefix", "Source prefix is not normalized.")
    return value


def _tree_component(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 240
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or ":" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise GitHubError("source_not_supported", "Remote source path was invalid.")
    return value


def _require_git_sha(value: object, label: str) -> str:
    if not _is_git_sha(value):
        raise GitHubError("invalid_git_sha", f"Approved {label} must be a full Git SHA-1.")
    return value


def _is_git_sha(value: object) -> bool:
    return isinstance(value, str) and _GIT_SHA.fullmatch(value) is not None


def _git_blob_sha(content: bytes) -> str:
    header = f"blob {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content, usedforsecurity=False).hexdigest()


def _git_tree_sha(entries: list[dict[str, object]]) -> str:
    supported_kinds = {
        ("040000", "tree"),
        ("100644", "blob"),
        ("100755", "blob"),
        ("120000", "blob"),
        ("160000", "commit"),
    }
    normalized: list[tuple[bytes, str, str, str]] = []
    paths: set[str] = set()
    for entry in entries:
        path = entry.get("path")
        mode = entry.get("mode")
        kind = entry.get("type")
        object_id = entry.get("sha")
        if (
            not isinstance(path, str)
            or not path
            or path in {".", ".."}
            or "/" in path
            or "\0" in path
            or (mode, kind) not in supported_kinds
            or not _is_git_sha(object_id)
            or path in paths
        ):
            raise GitHubError("invalid_response", "GitHub tree entry metadata was invalid.")
        try:
            encoded_path = path.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise GitHubError(
                "invalid_response", "GitHub tree entry path encoding was invalid."
            ) from exc
        paths.add(path)
        normalized.append((encoded_path, mode, kind, object_id))
    normalized.sort(key=lambda item: item[0] + (b"/" if item[2] == "tree" else b""))
    raw = b"".join(
        mode.lstrip("0").encode("ascii")
        + b" "
        + path
        + b"\0"
        + bytes.fromhex(object_id)
        for path, mode, _kind, object_id in normalized
    )
    header = f"tree {len(raw)}\0".encode("ascii")
    return hashlib.sha1(header + raw, usedforsecurity=False).hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"unsupported JSON constant: {value}")


__all__ = [
    "API_VERSION",
    "GitHubAppConfig",
    "GitHubClient",
    "GitHubError",
    "GitHubTransport",
    "INSTALLATION_PERMISSIONS",
    "StdlibGitHubTransport",
    "TransportResponse",
    "fetch_snapshot",
    "protected_digest",
]
