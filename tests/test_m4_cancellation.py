"""Cancellation must not publish a stopped case or hide incomplete cleanup."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from firstrun.domain.outcomes import Outcome
from firstrun.github_state import GitHubConfig, SQLiteStore
from firstrun.orchestration.github import process_one
from firstrun.orchestration.repair import _observe
from test_m3_publication import FakeClient, registration


def cleanup(success=True):
    return {"attempted": True, "app_container_created": True,
            "app_container_removed": success, "verifier_container_created": False,
            "verifier_container_removed": False, "workspace_created": True,
            "workspace_removed": success, "run_owned_resources_only": True,
            "worker_quarantined": not success, "sanitized_errors": []}


class CancellationTests(unittest.TestCase):
    def run_case(self, cleanup_payload):
        with tempfile.TemporaryDirectory(prefix="firstrun-m4-cancel-") as folder:
            root = Path(folder)
            store = SQLiteStore(root / "state.sqlite")
            reg = registration()
            config = GitHubConfig(registration=reg, private_key_path=root / "unread.pem",
                                  webhook_secret_path=root / "unread.secret")
            case_id = store.enqueue(reg, reg.approved_sha)
            snapshot = SimpleNamespace(base_commit=reg.approved_sha, base_tree="b" * 40,
                                       source_tree="b" * 40, archive_digest="archive",
                                       content_tree_digest="tree")

            def repair(_snapshot, _provider, *, observer, **kwargs):
                observer("baseline_running", {"binding": {}})
                case = store.get_case(case_id)
                store.cancel_case(reg.repository_id, case_id, sha=case["sha"],
                                  expected_version=case["version"], request_id=str(uuid4()))
                _observe(observer, "baseline_completed", cleanup_payload)
                return SimpleNamespace(outcome=Outcome.FAILED, baseline=None,
                                       to_dict=lambda: {"state": "blocked", "baseline": None})

            with patch("firstrun.orchestration.github.make_client", return_value=FakeClient()), \
                 patch("firstrun.orchestration.github.fetch_snapshot", return_value=snapshot), \
                 patch("firstrun.orchestration.github.validate_registration_source"), \
                 patch("firstrun.orchestration.github.repair_snapshot", side_effect=repair), \
                 patch("firstrun.orchestration.github.Publisher.publish") as publish:
                result = process_one(config, store, object(), artifact_root=root / "artifacts")
                publish.assert_not_called()
                return result

    def test_confirmed_cleanup_finishes_cancelled(self):
        result = self.run_case({"evidence": {"cleanup": cleanup()}})
        self.assertEqual(result["phase"], "cancelled")
        self.assertTrue(result["payload"]["cancellation_requested"])

    def test_missing_cleanup_keeps_quarantine(self):
        self.assertEqual(self.run_case({})["phase"], "interrupted")

    def test_failed_cleanup_keeps_quarantine(self):
        self.assertEqual(self.run_case({"attempt": {"cleanup": cleanup(False)}})["phase"], "interrupted")


if __name__ == "__main__":
    unittest.main()
