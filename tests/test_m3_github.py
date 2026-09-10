from __future__ import annotations

import base64
import hashlib
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping
from unittest.mock import patch

from firstrun.integrations.github import (
    INSTALLATION_PERMISSIONS,
    GitHubAppConfig,
    GitHubClient,
    GitHubError,
    TransportResponse,
    fetch_snapshot,
    protected_digest,
)
from firstrun.verification.source import capture_approved_source


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "notes-app"
NOW = 2_000_000_000.0
COMMIT_SHA = "1" * 40


class RecordingTransport:
    def __init__(self, responses: list[TransportResponse | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

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
        self.calls.append(
            {
                "method": method,
                "path": path,
                "headers": dict(headers),
                "body": body,
                "timeout": timeout_seconds,
                "limit": max_response_bytes,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def response(value: object, status: int = 200) -> TransportResponse:
    return TransportResponse(
        status,
        {},
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    )


def token_response() -> TransportResponse:
    expiry = datetime.fromtimestamp(NOW + 3600, timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    return response(
        {
            "token": "ghs_test_installation_token",
            "expires_at": expiry,
            "permissions": {**INSTALLATION_PERMISSIONS, "metadata": "read"},
            "repository_selection": "selected",
            "repositories": [{"id": 33}],
        },
        status=201,
    )


def config() -> GitHubAppConfig:
    return GitHubAppConfig(
        app_id=11,
        installation_id=22,
        repository_id=33,
        owner="owner",
        name="repo",
        private_key_path=Path("unused-test-key.pem"),
    )


class GitHubClientTests(unittest.TestCase):
    def test_token_is_scoped_to_exact_repository_and_permissions(self) -> None:
        transport = RecordingTransport(
            [
                token_response(),
                response({"id": 33, "full_name": "owner/repo", "fork": False}),
            ]
        )
        client = GitHubClient(config(), transport=transport, clock=lambda: NOW)
        with patch.object(client, "_app_jwt", return_value="signed-app-jwt"):
            client.ensure_repository_authorized()

        token_call = transport.calls[0]
        self.assertEqual("POST", token_call["method"])
        self.assertEqual("/app/installations/22/access_tokens", token_call["path"])
        self.assertEqual("Bearer signed-app-jwt", token_call["headers"]["Authorization"])
        self.assertEqual(
            {
                "repository_ids": [33],
                "permissions": dict(INSTALLATION_PERMISSIONS),
            },
            json.loads(token_call["body"]),
        )
        self.assertEqual(
            "Bearer ghs_test_installation_token",
            transport.calls[1]["headers"]["Authorization"],
        )

    def test_head_returns_only_a_full_commit_sha(self) -> None:
        transport = RecordingTransport(
            [
                token_response(),
                response({"id": 33, "full_name": "owner/repo", "fork": False}),
                response({"object": {"type": "commit", "sha": COMMIT_SHA}}),
            ]
        )
        client = GitHubClient(config(), transport=transport, clock=lambda: NOW)
        with patch.object(client, "_app_jwt", return_value="jwt"):
            self.assertEqual(COMMIT_SHA, client.head("feature/setup"))
        self.assertTrue(transport.calls[-1]["path"].endswith("feature%2Fsetup"))

    def test_generic_request_cannot_leave_configured_repository(self) -> None:
        client = GitHubClient(config(), transport=RecordingTransport([]), clock=lambda: NOW)
        with self.assertRaises(GitHubError) as raised:
            client.request("GET", "/repos/other/repo")
        self.assertEqual("repository_scope_violation", raised.exception.code)

    def test_network_failure_on_write_is_uncertain_and_sanitized(self) -> None:
        transport = RecordingTransport([token_response(), OSError("secret response body")])
        client = GitHubClient(config(), transport=transport, clock=lambda: NOW)
        with patch.object(client, "_app_jwt", return_value="jwt"):
            with self.assertRaises(GitHubError) as raised:
                client.request("POST", f"{client.prefix}/git/refs", {"ref": "refs/heads/x"})
        self.assertTrue(raised.exception.uncertain)
        self.assertIsNone(raised.exception.status)
        self.assertNotIn("secret", str(raised.exception))
        self.assertNotIn("ghs_test", str(raised.exception))

    def test_token_response_must_confirm_exact_scope(self) -> None:
        bad = json.loads(token_response().body)
        bad["permissions"]["issues"] = "write"
        client = GitHubClient(
            config(), transport=RecordingTransport([response(bad, 201)]), clock=lambda: NOW
        )
        with patch.object(client, "_app_jwt", return_value="jwt"):
            with self.assertRaises(GitHubError) as raised:
                client.request("GET", client.prefix)
        self.assertEqual("invalid_token_response", raised.exception.code)


class SnapshotClient:
    prefix = "/repos/owner/repo"

    def __init__(self, values: dict[str, object]) -> None:
        self.values = values
        self.authorized = False

    def ensure_repository_authorized(self) -> None:
        self.authorized = True

    def request(self, method: str, path: str, body: object = None) -> object:
        if method != "GET" or body is not None:
            raise AssertionError("source fetch must be read-only")
        return self.values[path]


def git_blob_sha(content: bytes) -> str:
    value = f"blob {len(content)}\0".encode("ascii") + content
    return hashlib.sha1(value, usedforsecurity=False).hexdigest()


def git_tree_sha(entries: list[dict[str, object]]) -> str:
    ordered = sorted(
        entries,
        key=lambda item: (
            str(item["path"]) + ("/" if item["type"] == "tree" else "")
        ).encode("utf-8"),
    )
    content = b"".join(
        str(item["mode"]).lstrip("0").encode("ascii")
        + b" "
        + str(item["path"]).encode("utf-8")
        + b"\0"
        + bytes.fromhex(str(item["sha"]))
        for item in ordered
    )
    value = f"tree {len(content)}\0".encode("ascii") + content
    return hashlib.sha1(value, usedforsecurity=False).hexdigest()


def source_api_values(
    files: Mapping[str, bytes],
    *,
    source_prefix: str = "fixture",
    mode_override: tuple[str, str] | None = None,
) -> dict[str, object]:
    prefix = SnapshotClient.prefix
    values: dict[str, object] = {}
    root: dict[str, object] = {}
    for path, content in files.items():
        cursor = root
        parts = path.split("/")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})  # type: ignore[assignment]
        cursor[parts[-1]] = content

    def add_tree(node: dict[str, object]) -> str:
        entries: list[dict[str, object]] = []
        for name in sorted(node):
            value = node[name]
            if isinstance(value, dict):
                child_sha = add_tree(value)
                entries.append(
                    {"path": name, "mode": "040000", "type": "tree", "sha": child_sha}
                )
            else:
                assert isinstance(value, bytes)
                object_id = git_blob_sha(value)
                mode = "100644"
                if mode_override is not None and path_for(node, name, root) == mode_override[0]:
                    mode = mode_override[1]
                entries.append(
                    {
                        "path": name,
                        "mode": mode,
                        "type": "blob",
                        "sha": object_id,
                        "size": len(value),
                    }
                )
                values[f"{prefix}/git/blobs/{object_id}"] = {
                    "sha": object_id,
                    "encoding": "base64",
                    "size": len(value),
                    "content": base64.b64encode(value).decode("ascii"),
                }
        tree_sha = git_tree_sha(entries)
        values[f"{prefix}/git/trees/{tree_sha}"] = {
            "sha": tree_sha,
            "truncated": False,
            "tree": entries,
        }
        return tree_sha

    source_tree = add_tree(root)
    if source_prefix:
        base_entries = [
            {
                "path": source_prefix,
                "mode": "040000",
                "type": "tree",
                "sha": source_tree,
            }
        ]
        base_tree = git_tree_sha(base_entries)
        values[f"{prefix}/git/trees/{base_tree}"] = {
            "sha": base_tree,
            "truncated": False,
            "tree": base_entries,
        }
    else:
        base_tree = source_tree
    values[f"{prefix}/git/commits/{COMMIT_SHA}"] = {
        "sha": COMMIT_SHA,
        "tree": {"sha": base_tree},
    }
    return values


def path_for(node: object, name: str, root: object) -> str:
    """Only used to identify the top-level mode override in compact test fixtures."""

    if node is root:
        return name
    return ""


class ExactSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.local = capture_approved_source(FIXTURE)

    def test_fetches_exact_nested_source_and_preserves_git_identities(self) -> None:
        values = source_api_values(self.local.files)
        client = SnapshotClient(values)
        snapshot = fetch_snapshot(client, COMMIT_SHA, "fixture", ROOT)

        self.assertTrue(client.authorized)
        self.assertEqual(COMMIT_SHA, snapshot.base_commit)
        self.assertEqual(dict(self.local.files), dict(snapshot.files))
        self.assertEqual(protected_digest(self.local.files), protected_digest(snapshot.files))
        self.assertNotEqual(snapshot.base_tree, snapshot.source_tree)

    def test_protected_remote_change_is_rejected(self) -> None:
        changed = dict(self.local.files)
        changed["package.json"] += b" "
        client = SnapshotClient(source_api_values(changed))
        with self.assertRaises(GitHubError) as raised:
            fetch_snapshot(client, COMMIT_SHA, "fixture", ROOT)
        self.assertEqual("source_not_approved", raised.exception.code)

    def test_executable_entry_is_rejected(self) -> None:
        values = source_api_values(
            self.local.files, source_prefix="", mode_override=("README.md", "100755")
        )
        client = SnapshotClient(values)
        with self.assertRaises(GitHubError) as raised:
            fetch_snapshot(client, COMMIT_SHA, "", ROOT)
        self.assertEqual("source_not_supported", raised.exception.code)

    def test_blob_content_must_match_git_sha(self) -> None:
        values = source_api_values(self.local.files)
        blob_path = next(path for path in values if "/git/blobs/" in path)
        values[blob_path] = dict(values[blob_path])
        values[blob_path]["content"] = base64.b64encode(b"tampered").decode("ascii")
        client = SnapshotClient(values)
        with self.assertRaises(GitHubError) as raised:
            fetch_snapshot(client, COMMIT_SHA, "fixture", ROOT)
        self.assertEqual("source_integrity_error", raised.exception.code)

    def test_tree_entries_must_match_git_sha(self) -> None:
        values = source_api_values(self.local.files)
        tree_path = next(path for path in values if "/git/trees/" in path)
        values[tree_path] = dict(values[tree_path])
        values[tree_path]["tree"] = [dict(entry) for entry in values[tree_path]["tree"]]
        values[tree_path]["tree"][0]["sha"] = "f" * 40
        client = SnapshotClient(values)
        with self.assertRaises(GitHubError) as raised:
            fetch_snapshot(client, COMMIT_SHA, "fixture", ROOT)
        self.assertEqual("source_integrity_error", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
