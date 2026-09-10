"""Offline M3 webhook, durable-state, lease, and write-journal checks."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from firstrun.github_state import (
    GitHubConfig,
    GitHubStateError,
    JournalConflictError,
    LeaseConflictError,
    RepositoryRegistration,
    SQLiteStore,
)
from firstrun.webhooks import WebhookError, ingest_signed_push


SECRET = b"local-test-webhook-secret"
APPROVED_SHA = "a" * 40
PUSH_SHA = "b" * 40


def digest(character: str) -> str:
    return "sha256:" + character * 64


def registration(**changes: object) -> RepositoryRegistration:
    values: dict[str, object] = {
        "app_id": 11,
        "installation_id": 22,
        "repository_id": 33,
        "owner": "owner",
        "name": "repo",
        "branch": "main",
        "approved_sha": APPROVED_SHA,
        "source_prefix": "fixtures/notes-app",
        "target_digest": digest("1"),
        "approved_recipe_digest": digest("2"),
        "protected_digest": digest("3"),
        "verifier_digest": digest("4"),
        "policy_revision": "m1-policy-v1",
        "policy_digest": digest("5"),
        "runtime_digest": digest("6"),
        "runtime_image_id": digest("7"),
        "author_name": "FirstRun Test",
        "author_email": "firstrun@example.invalid",
        "automatic_prs": False,
    }
    values.update(changes)
    return RepositoryRegistration.model_validate(values)


def push_payload(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "ref": "refs/heads/main",
        "after": PUSH_SHA,
        "deleted": False,
        "installation": {"id": 22},
        "repository": {
            "id": 33,
            "name": "repo",
            "full_name": "owner/repo",
            "fork": False,
            "owner": {"login": "owner"},
        },
    }
    value.update(changes)
    return value


def body(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def headers(raw: bytes, *, delivery: str = "delivery-1") -> dict[str, str]:
    signature = hmac.new(SECRET, raw, hashlib.sha256).hexdigest()
    return {
        "X-Hub-Signature-256": "sha256=" + signature,
        "X-GitHub-Event": "push",
        "X-GitHub-Delivery": delivery,
    }


class Clock:
    def __init__(self, value: int = 1_700_000_000) -> None:
        self.value = value

    def __call__(self) -> float:
        return float(self.value)


class M3FoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="firstrun-m3-")
        self.directory = Path(self.temporary.name).resolve()
        self.secret_path = self.directory / "webhook.secret"
        self.key_path = self.directory / "github-app.pem"
        self.secret_path.write_bytes(SECRET)
        self.key_path.write_text("not-read-by-these-tests", encoding="ascii")
        self.config = GitHubConfig(
            registration=registration(),
            private_key_path=self.key_path,
            webhook_secret_path=self.secret_path,
        )
        self.clock = Clock()
        self.database = self.directory / "state.sqlite"
        self.store = SQLiteStore(self.database, clock=self.clock)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def ingest(
        self,
        value: dict[str, object] | None = None,
        *,
        delivery: str = "delivery-1",
    ) -> dict[str, object]:
        raw = body(value or push_payload())
        return ingest_signed_push(
            raw, headers(raw, delivery=delivery), self.config, self.store
        )

    def test_registration_is_strict_frozen_and_pins_every_execution_basis(self) -> None:
        configured = self.config.registration
        self.assertEqual("main", configured.branch)
        self.assertEqual("m1-policy-v1", configured.policy_revision)
        self.assertNotIn("secret", configured.model_dump(mode="json"))
        with self.assertRaises(ValidationError):
            configured.owner = "other"  # type: ignore[misc]

        invalid = (
            {"owner": "-owner"},
            {"name": "../repo"},
            {"branch": "refs/heads/main"},
            {"approved_sha": "A" * 40},
            {"source_prefix": "arbitrary"},
            {"runtime_digest": "sha256:fake"},
            {"policy_revision": "Bad Policy"},
            {"author_email": "bad email@example.invalid"},
            {"repository_id": True},
        )
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(ValidationError):
                registration(**change)

    def test_bad_signature_is_rejected_before_malformed_json_is_parsed(self) -> None:
        raw = b"not-json"
        bad = headers(b"different")
        with self.assertRaisesRegex(WebhookError, "signature"):
            ingest_signed_push(raw, bad, self.config, self.store)

        with self.assertRaisesRegex(WebhookError, "JSON"):
            ingest_signed_push(raw, headers(raw), self.config, self.store)

    def test_authorized_future_head_is_enqueued_without_requiring_approved_sha(self) -> None:
        value = push_payload(secret_marker="never-store-this-body")
        raw = body(value)
        result = ingest_signed_push(raw, headers(raw), self.config, self.store)
        self.assertEqual("accepted", result["status"])
        self.assertEqual(PUSH_SHA, result["sha"])
        self.assertNotEqual(APPROVED_SHA, result["sha"])
        case = self.store.get_case(str(result["case_id"]))
        assert case is not None
        self.assertEqual(PUSH_SHA, case["sha"])
        self.assertEqual("queued", case["phase"])
        self.assertEqual(1_700_000_000, case["created_at"])
        self.assertEqual(
            self.config.registration.model_dump(mode="json"), case["registration"]
        )
        persisted = b"".join(
            path.read_bytes()
            for path in self.directory.glob("state.sqlite*")
            if path.is_file()
        )
        self.assertNotIn(b"never-store-this-body", persisted)
        self.assertNotIn(SECRET, persisted)
        self.assertNotIn(raw, persisted)

    def test_delivery_and_logical_case_dedupe_are_durable_and_immutable(self) -> None:
        first = self.ingest(delivery="same-delivery")
        duplicate = self.ingest(delivery="same-delivery")
        logical_duplicate = self.ingest(delivery="another-delivery")
        self.assertEqual("duplicate", duplicate["status"])
        self.assertEqual(first["case_id"], duplicate["case_id"])
        self.assertEqual(first["case_id"], logical_duplicate["case_id"])

        changed = push_payload(after="c" * 40)
        raw = body(changed)
        with self.assertRaises(GitHubStateError):
            ingest_signed_push(
                raw,
                headers(raw, delivery="same-delivery"),
                self.config,
                self.store,
            )
        connection = sqlite3.connect(self.database)
        try:
            self.assertEqual(2, connection.execute("SELECT count(*) FROM deliveries").fetchone()[0])
            self.assertEqual(1, connection.execute("SELECT count(*) FROM cases").fetchone()[0])
        finally:
            connection.close()

    def test_authorization_rejects_wrong_installation_repo_branch_fork_and_delete(self) -> None:
        cases = (
            {"installation": {"id": 99}},
            {"repository": {**push_payload()["repository"], "id": 99}},  # type: ignore[arg-type]
            {"ref": "refs/heads/other"},
            {"repository": {**push_payload()["repository"], "fork": True}},  # type: ignore[arg-type]
            {"deleted": True},
        )
        for index, change in enumerate(cases):
            value = push_payload(**change)
            raw = body(value)
            with self.subTest(change=change), self.assertRaises(WebhookError):
                ingest_signed_push(
                    raw,
                    headers(raw, delivery=f"rejected-{index}"),
                    self.config,
                    self.store,
                )

    def test_fenced_lease_merges_payload_records_events_and_cannot_be_reused(self) -> None:
        case_id = self.store.enqueue(registration(), PUSH_SHA)
        owner = "lease-owner-00000001"
        claimed = self.store.claim(owner, 1_800)
        assert claimed is not None
        self.assertEqual(case_id, claimed["id"])
        self.store.update_case(case_id, owner, "baseline_running", {"baseline": {"ok": False}})
        updated = self.store.update_case(
            case_id, owner, "investigating", {"agent": {"attempts": 1}}
        )
        self.assertEqual(
            {"baseline": {"ok": False}, "agent": {"attempts": 1}},
            updated["payload"],
        )
        with self.assertRaises(LeaseConflictError):
            self.store.update_case(case_id, "lease-owner-00000002", "proof_running")
        self.store.finish(case_id, owner, "needs_input", {"message": "maintainer choice"})
        with self.assertRaises(LeaseConflictError):
            self.store.update_case(case_id, owner, "investigating")
        self.assertGreaterEqual(len(self.store.get_events(case_id)), 5)

        self.store.enqueue(registration(), "c" * 40)
        with self.assertRaises(LeaseConflictError):
            self.store.claim(owner, 1_800)

    def test_expired_execution_is_interrupted_and_blocks_repo_claims(self) -> None:
        first = self.store.enqueue(registration(), PUSH_SHA)
        owner = "lease-owner-00000011"
        self.store.claim(owner, 1_200)
        self.store.update_case(first, owner, "proof_running", {"evidence": "preserved"})
        self.clock.value += 1_201
        second = self.store.enqueue(registration(), "c" * 40)
        self.assertEqual("interrupted", self.store.get_case(first)["phase"])  # type: ignore[index]
        self.assertEqual("queued", self.store.get_case(second)["phase"])  # type: ignore[index]
        self.assertIsNone(self.store.claim("lease-owner-00000012", 1_200))
        self.assertEqual(
            "preserved", self.store.get_case(first)["payload"]["evidence"]  # type: ignore[index]
        )

        publishing_registration = registration(repository_id=55)
        publishing = self.store.enqueue(publishing_registration, "d" * 40)
        self.store.claim("lease-owner-00000023", 1_200)
        self.store.update_case(
            publishing,
            "lease-owner-00000023",
            "publishing",
            {"repair": {"artifact_ref": "artifact_123"}},
        )
        self.clock.value += 1_201
        resumed = self.store.claim("lease-owner-00000024", 1_200)
        assert resumed is not None
        self.assertEqual(publishing, resumed["id"])
        self.assertEqual("reconcile_pending", resumed["phase"])
        self.assertEqual(
            "artifact_123", resumed["payload"]["repair"]["artifact_ref"]  # type: ignore[index]
        )

    def test_write_journal_is_write_ahead_immutable_and_cas_confirmed(self) -> None:
        case_id = self.store.enqueue(registration(), PUSH_SHA)
        owner = "lease-owner-00000031"
        self.store.claim(owner, 1_800)
        self.store.update_case(case_id, owner, "publishing")
        request = {"head": "firstrun/case", "base": "main"}
        created = self.store.journal_intent(
            case_id, owner, "pull", "POST", "/repos/owner/repo/pulls", request
        )
        replay = self.store.journal_intent(
            case_id, owner, "pull", "POST", "/repos/owner/repo/pulls", request
        )
        self.assertTrue(created["created"])
        self.assertFalse(replay["created"])
        self.assertEqual("pending", replay["state"])
        with self.assertRaises(JournalConflictError):
            self.store.journal_intent(
                case_id,
                owner,
                "pull",
                "POST",
                "/repos/owner/repo/pulls",
                {"head": "different"},
            )

        result = {"number": 12, "head_sha": PUSH_SHA}
        confirmed = self.store.journal_result(case_id, owner, "pull", result)
        self.assertEqual("confirmed", confirmed["state"])
        self.assertEqual(result, confirmed["result"])
        self.assertEqual(
            confirmed["result"],
            self.store.journal_result(case_id, owner, "pull", result)["result"],
        )
        with self.assertRaises(JournalConflictError):
            self.store.journal_result(case_id, owner, "pull", {"number": 13})
        reopened = SQLiteStore(self.database, clock=self.clock).get_journal(case_id, "pull")
        assert reopened is not None
        self.assertFalse(reopened["created"])
        self.assertEqual(result, reopened["result"])

if __name__ == "__main__":
    unittest.main()
