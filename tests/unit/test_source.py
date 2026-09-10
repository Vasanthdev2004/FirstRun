from __future__ import annotations

import io
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import firstrun.verification.source as source_module
from firstrun.verification.source import (
    SourceInfrastructureError,
    SourcePolicyError,
    build_candidate_patch,
    capture_approved_source,
    content_tree_digest,
    default_approved_repo_path,
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

    def test_repo_local_git_executable_is_never_run_on_the_host(self) -> None:
        with patch(
            "firstrun.verification.source.shutil.which",
            return_value=str(ROOT / "git.exe"),
        ):
            with self.assertRaises(SourceInfrastructureError):
                capture_approved_source(FIXTURE)

    def test_all_tree_and_archive_reads_use_the_single_resolved_commit(self) -> None:
        with (
            patch.object(source_module, "_git_text", wraps=source_module._git_text) as text_git,
            patch.object(source_module, "_git_bytes", wraps=source_module._git_bytes) as bytes_git,
        ):
            snapshot = capture_approved_source(FIXTURE)

        text_args = [call.args[1:] for call in text_git.call_args_list]
        bytes_args = [call.args[1:] for call in bytes_git.call_args_list]
        self.assertIn(("rev-parse", f"{snapshot.base_commit}^{{tree}}"), text_args)
        self.assertIn(
            ("rev-parse", f"{snapshot.base_commit}:fixtures/notes-app"), text_args
        )
        self.assertIn(("ls-tree", "-r", "-z", "--full-tree", snapshot.source_tree), bytes_args)
        self.assertTrue(any(args[:2] == ("cat-file", "blob") for args in bytes_args))
        self.assertFalse(any(args and args[0] in {"archive", "show"} for args in bytes_args))
        self.assertNotIn(("rev-parse", "HEAD^{tree}"), text_args)

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

    def test_archive_rejects_unsafe_paths_links_binary_lfs_and_oversize(self) -> None:
        cases = (
            ("traversal", "../escape", b"text", tarfile.REGTYPE, ""),
            ("control path", "bad\nname", b"text", tarfile.REGTYPE, ""),
            ("symlink", "link", b"", tarfile.SYMTYPE, "target"),
            ("hardlink", "hard", b"", tarfile.LNKTYPE, "target"),
            ("binary", "binary", b"a\x00b", tarfile.REGTYPE, ""),
            (
                "lfs pointer",
                "pointer",
                b"version https://git-lfs.github.com/spec/v1\n",
                tarfile.REGTYPE,
                "",
            ),
            (
                "oversize",
                "large",
                b"x" * (source_module.MAX_FILE_BYTES + 1),
                tarfile.REGTYPE,
                "",
            ),
        )
        for label, name, content, entry_type, linkname in cases:
            with self.subTest(label=label), self.assertRaises(SourcePolicyError):
                source_module._read_safe_archive(
                    self._tar_bytes(name, content, entry_type, linkname)
                )

    def test_archive_rejects_duplicate_paths(self) -> None:
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:") as archive:
            for content in (b"first", b"second"):
                info = tarfile.TarInfo("same")
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        with self.assertRaises(SourcePolicyError):
            source_module._read_safe_archive(output.getvalue())

    def test_git_tree_rejects_gitlinks_links_and_executable_files(self) -> None:
        object_id = b"a" * 40
        for mode, object_type in (
            (b"160000", b"commit"),
            (b"120000", b"blob"),
            (b"100755", b"blob"),
        ):
            entry = mode + b" " + object_type + b" " + object_id + b"\tunsafe\x00"
            with (
                self.subTest(mode=mode),
                patch.object(source_module, "_git_bytes", return_value=entry),
                self.assertRaises(SourcePolicyError),
            ):
                source_module._read_committed_entries(ROOT, "b" * 40)

    def test_capture_ignores_git_replace_refs(self) -> None:
        selected_git = source_module.shutil.which("git")
        self.assertIsNotNone(selected_git)
        assert selected_git is not None
        with tempfile.TemporaryDirectory(prefix="firstrun-source-test-") as temporary:
            repository = Path(temporary).resolve()
            fixture = repository / "fixtures" / "notes-app"
            for relative, content in self.snapshot.files.items():
                destination = fixture.joinpath(*relative.split("/"))
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)

            def git(*args: str) -> str:
                completed = subprocess.run(
                    (selected_git, *args),
                    cwd=repository,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    shell=False,
                    timeout=20,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                return completed.stdout.strip()

            git("init", "--quiet")
            git("config", "user.name", "FirstRun Test")
            git("config", "user.email", "firstrun-test@example.invalid")
            git("add", "--", "fixtures/notes-app")
            git("commit", "--quiet", "-m", "original")
            original = git("rev-parse", "HEAD")
            original_readme = (fixture / "README.md").read_bytes()

            (fixture / "README.md").write_bytes(b"replacement\n" + original_readme)
            git("add", "--", "fixtures/notes-app/README.md")
            git("commit", "--quiet", "-m", "replacement")
            replacement = git("rev-parse", "HEAD")
            head_ref = git("symbolic-ref", "HEAD")
            git("replace", original, replacement)
            git("update-ref", head_ref, original)

            captured = capture_approved_source(fixture, approved_repo=fixture)

            self.assertEqual(captured.base_commit, original)
            self.assertEqual(captured.files["README.md"], original_readme)

    @staticmethod
    def _tar_bytes(
        name: str,
        content: bytes,
        entry_type: bytes,
        linkname: str,
    ) -> bytes:
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:") as archive:
            info = tarfile.TarInfo(name)
            info.type = entry_type
            info.linkname = linkname
            info.size = len(content) if entry_type == tarfile.REGTYPE else 0
            archive.addfile(info, io.BytesIO(content) if info.size else None)
        return output.getvalue()

    def test_git_output_has_a_hard_controller_byte_limit(self) -> None:
        class FakeProcess:
            def __init__(self) -> None:
                self.stdout = io.BytesIO(b"x" * 17)
                self.stderr = io.BytesIO()
                self.killed = False

            def wait(self, *, timeout: float) -> int:
                return 0

            def kill(self) -> None:
                self.killed = True

        process = FakeProcess()
        selected_git = source_module.shutil.which("git")
        self.assertIsNotNone(selected_git)
        with (
            patch.object(source_module, "MAX_ARCHIVE_BYTES", 16),
            patch.object(source_module.subprocess, "Popen", return_value=process),
        ):
            with self.assertRaises(SourceInfrastructureError):
                source_module._git(ROOT, ("status",))

        self.assertTrue(process.killed)


if __name__ == "__main__":
    unittest.main()
