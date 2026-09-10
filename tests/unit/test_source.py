from __future__ import annotations

import unittest
from pathlib import Path

from firstrun.verification.source import (
    SourcePolicyError,
    build_candidate_patch,
    capture_approved_source,
    content_tree_digest,
    default_approved_repo_path,
    materialize_source,
)


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures" / "notes-app"


class ApprovedSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.snapshot = capture_approved_source(FIXTURE)

    def test_capture_is_bound_to_git_commit_tree_archive_and_content(self) -> None:
        snapshot = self.snapshot
        self.assertRegex(snapshot.base_commit, r"^[0-9a-f]{40}$")
        self.assertRegex(snapshot.base_tree, r"^[0-9a-f]{40}$")
        self.assertRegex(snapshot.source_tree, r"^[0-9a-f]{40}$")
        self.assertRegex(snapshot.archive_digest, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(snapshot.content_tree_digest, content_tree_digest(snapshot.files))
        self.assertIn(".firstrun/target.json", snapshot.files)
        self.assertNotIn(".git/config", snapshot.files)
        with self.assertRaises(TypeError):
            snapshot.files["new"] = b"value"  # type: ignore[index]

    def test_only_exact_controller_approved_repository_is_accepted(self) -> None:
        with self.assertRaises(SourcePolicyError):
            capture_approved_source(ROOT, approved_repo=default_approved_repo_path())

    def test_candidate_rejects_target_source_and_traversal(self) -> None:
        for path in (
            ".firstrun/target.json",
            "src/server.mjs",
            "../README.md",
            "README.md/../target.json",
            "C:\\target.json",
        ):
            with self.subTest(path=path), self.assertRaises(SourcePolicyError):
                build_candidate_patch(self.snapshot, {path: b"changed"})

    def test_materializations_do_not_share_mutable_state(self) -> None:
        first = materialize_source(self.snapshot)
        second = materialize_source(self.snapshot)
        first_root = first.root
        second_root = second.root
        try:
            self.assertNotEqual(first_root, second_root)
            marker = first_root / ".local" / "state-marker"
            marker.parent.mkdir()
            marker.write_text("phase one", encoding="utf-8")
            self.assertFalse((second_root / ".local" / "state-marker").exists())
            self.assertEqual(
                (second_root / ".firstrun" / "target.json").read_bytes(),
                self.snapshot.files[".firstrun/target.json"],
            )
        finally:
            first.cleanup()
            second.cleanup()
        self.assertFalse(first_root.exists())
        self.assertFalse(second_root.exists())


if __name__ == "__main__":
    unittest.main()
