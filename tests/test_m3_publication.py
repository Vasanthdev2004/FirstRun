"""Offline publication safety checks; no App credentials or repository execution."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from unittest.mock import patch
from pathlib import Path
from uuid import uuid4

from firstrun.artifacts import ArtifactStore
from firstrun.domain.outcomes import Outcome
from firstrun.domain.evidence import build_run_evidence
from firstrun.domain.repair import RepairProviderConfig
from firstrun.github_state import GitHubConfig, RepositoryRegistration, SQLiteStore
from firstrun.orchestration.github import (
    PublicationBlocked, Publisher, ReconciliationPending, git_files_tree,
    git_object_id, git_tree_id, validate_tree,
    process_one,
)
from firstrun.orchestration.repair import RepairCaseState, RepairLocalResult
from firstrun.verification.source import capture_approved_source, default_approved_repo_path


def registration() -> RepositoryRegistration:
    return RepositoryRegistration(
        app_id=1, installation_id=2, repository_id=3, owner="owner", name="demo",
        branch="main", approved_sha="a" * 40, source_prefix="",
        target_digest="sha256:" + "1" * 64, approved_recipe_digest="sha256:" + "2" * 64,
        protected_digest="sha256:" + "3" * 64, verifier_digest="sha256:" + "4" * 64,
        policy_digest="sha256:" + "7" * 64, policy_revision="m1-policy-v1",
        runtime_digest="sha256:" + "5" * 64, runtime_image_id="sha256:" + "6" * 64,
        author_name="Owner", author_email="owner@example.com", automatic_prs=True,
    )


class FakeClient:
    def __init__(self) -> None:
        self.posts = 0
        self.visible = None
        self.reveal = False
        self.current_head = "a" * 40

    def request(self, method, path, body=None):
        if method == "POST":
            self.posts += 1
            if self.reveal:
                self.visible = {"number": 123}
        return {}

    def head(self, branch):
        return self.current_head


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "state.sqlite"
        self.store = SQLiteStore(self.database)
        self.registration = registration()
        self.case_id = self.store.enqueue(self.registration, "a" * 40)
        self.owner = str(uuid4())
        self.store.claim(self.owner, 1800)
        self.store.update_case(self.case_id, self.owner, "publishing")
        self.client = FakeClient()
        self.publisher = Publisher(self.client, self.store, self.registration, self.case_id, self.owner)

    def tearDown(self):
        self.temp.cleanup()

    def write(self):
        return self.publisher._write("pull", "POST", "/repos/owner/demo/pulls",
                                     {"head": "repair"}, lambda: self.client.visible)

    def test_uncertain_post_is_not_repeated_after_store_reopen(self):
        with self.assertRaises(ReconciliationPending):
            self.write()
        self.store.finish(self.case_id, self.owner, "reconcile_pending")
        self.store = SQLiteStore(self.database)
        self.owner = str(uuid4())
        self.store.claim(self.owner, 1800)
        self.publisher = Publisher(self.client, self.store, self.registration, self.case_id, self.owner)
        with self.assertRaises(ReconciliationPending):
            self.write()
        self.assertEqual(self.client.posts, 1)
        self.client.visible = {"number": 123}
        self.assertEqual(self.write(), {"number": 123})
        self.assertEqual(self.client.posts, 1)

    def test_success_requires_readback_and_repeat_does_not_post(self):
        self.client.reveal = True
        self.assertEqual(self.write(), {"number": 123})
        self.assertEqual(self.write(), {"number": 123})
        self.assertEqual(self.client.posts, 1)

    def test_disabled_permission_prevents_even_dispatch(self):
        self.publisher.registration = self.registration.model_copy(update={"automatic_prs": False})
        with self.assertRaises(PublicationBlocked):
            self.write()
        self.assertEqual(self.client.posts, 0)

    def test_check_preserves_timeout_and_infrastructure_outcomes(self):
        from unit.test_evidence import binding, passing_observation
        authority = binding()
        evidence = build_run_evidence(binding=authority, observation=passing_observation(authority))
        for outcome, conclusion in ((Outcome.TIMED_OUT, "timed_out"),
                                    (Outcome.INFRASTRUCTURE_ERROR, "action_required"),
                                    (Outcome.FAILED, "failure")):
            with self.subTest(outcome=outcome), patch.object(self.publisher, "_write") as write:
                current = evidence.model_copy(update={"base_commit": "a" * 40, "outcome": outcome})
                self.publisher.publish_check(current, "default", None)
                self.assertEqual(write.call_args.args[3]["conclusion"], conclusion)
                self.assertEqual(write.call_args.args[3]["head_sha"], "a" * 40)

    def test_changed_head_marks_stale_and_queues_distinct_case(self):
        self.client.current_head = "b" * 40
        self.assertTrue(self.publisher.stale())
        old = self.store.get_case(self.case_id)
        self.assertEqual(old["phase"], "stale")
        new = self.store.get_case(old["payload"]["replacement_case_id"])
        self.assertEqual(new["sha"], "b" * 40)
        self.assertNotEqual(new["id"], self.case_id)
        self.assertEqual(self.client.posts, 0)

    def test_head_change_during_nonrepair_run_starts_new_case(self):
        self.store.finish(self.case_id, self.owner, "reconcile_pending")
        source = replace(capture_approved_source(default_approved_repo_path()), base_commit="a" * 40)
        config = GitHubConfig(registration=self.registration,
                              private_key_path=self.database.parent / "unused.pem",
                              webhook_secret_path=self.database.parent / "unused.secret")
        provider = RepairProviderConfig(aws_profile="offline", region="us-east-1", model_id="offline-model",
                                        provider_cost_acknowledged=True, credential_identity_verified=True)

        def completed(*args, **kwargs):
            self.client.current_head = "b" * 40
            return RepairLocalResult(state=RepairCaseState.BLOCKED, outcome=Outcome.POLICY_BLOCKED,
                                     source_revision="a" * 40, message="offline result")

        module = "firstrun.orchestration.github."
        with patch(module + "make_client", return_value=self.client), \
             patch(module + "fetch_snapshot", return_value=source), \
             patch(module + "validate_registration_source"), \
             patch(module + "repair_snapshot", side_effect=completed):
            result = process_one(config, self.store, provider,
                                 artifact_root=self.database.parent / "artifacts")
        self.assertEqual(result["phase"], "stale")
        self.assertEqual(self.store.get_case(result["payload"]["replacement_case_id"])["sha"], "b" * 40)
        self.assertEqual(self.client.posts, 0)


class GitIdentityTests(unittest.TestCase):
    def test_local_artifacts_are_content_addressed_and_reject_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory))
            reference = store.put({"phase": "proof", "outcome": "failed"})
            self.assertEqual(store.get(reference), {"phase": "proof", "outcome": "failed"})
            self.assertEqual(store.put({"phase": "proof", "outcome": "failed"}), reference)
            with self.assertRaises(ValueError):
                store.get("../../credentials")

    def test_computed_tree_matches_existing_committed_fixture(self):
        source = capture_approved_source(default_approved_repo_path())
        self.assertEqual(git_files_tree(source.files), source.source_tree)

    def test_tree_sort_uses_git_directory_order_and_rejects_tampering(self):
        entries = [
            {"path": "a", "type": "tree", "mode": "040000", "sha": "1" * 40},
            {"path": "a.txt", "type": "blob", "mode": "100644", "sha": "2" * 40},
        ]
        expected = git_object_id("tree", b"100644 a.txt\0" + bytes.fromhex("2" * 40)
                                 + b"40000 a\0" + bytes.fromhex("1" * 40))
        self.assertEqual(git_tree_id(entries), expected)
        self.assertEqual(validate_tree({"sha": expected, "truncated": False, "tree": entries}, expected), entries)
        with self.assertRaises(PublicationBlocked):
            validate_tree({"sha": expected, "truncated": True, "tree": entries}, expected)
        entries[0]["sha"] = "3" * 40
        with self.assertRaises(PublicationBlocked):
            validate_tree({"sha": expected, "truncated": False, "tree": entries}, expected)


class IngressSurfaceTests(unittest.TestCase):
    def test_only_signed_ingress_and_non_sensitive_health_are_exposed(self):
        from fastapi.testclient import TestClient
        from firstrun.api import create_app
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = GitHubConfig(registration=registration(),
                                  private_key_path=root / "unused-key.pem",
                                  webhook_secret_path=root / "unused-secret")
            with TestClient(create_app(config, root / "state.sqlite")) as client:
                self.assertEqual(client.get("/health").json()["status"], "ready")
                self.assertEqual(client.get("/cases").status_code, 404)
                self.assertEqual(client.get("/docs").status_code, 404)
                self.assertEqual(client.post("/github/webhook", content=b"{}").status_code, 400)
                response = client.post("/github/webhook", content=b"{}", headers={
                    "X-Hub-Signature-256": "sha256=" + "0" * 64,
                    "X-GitHub-Event": "push", "X-GitHub-Delivery": "offline-test",
                })
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json(), {"detail": "Webhook rejected"})


if __name__ == "__main__":
    unittest.main()
