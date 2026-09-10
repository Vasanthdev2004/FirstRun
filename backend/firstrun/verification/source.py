"""Trusted, immutable source capture for the controlled M1 fixture lane.

The local CLI is deliberately not a general repository runner.  It accepts one
controller-owned fixture path and captures a bounded archive of one exact committed
subtree.  Working-tree bytes and Git conversion filters are never consulted, and
Docker never receives a bind mount of the live checkout.
"""

from __future__ import annotations

import hashlib
import io
import os
import signal
import shutil
import stat
import subprocess
import tarfile
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Mapping


MAX_ARCHIVE_BYTES = 2 * 1024 * 1024
MAX_SOURCE_BYTES = 1024 * 1024
MAX_FILE_BYTES = 256 * 1024
MAX_SOURCE_FILES = 128
ALLOWED_CANDIDATE_PATHS = frozenset({".firstrun/recipe.json", "README.md"})


class SourcePolicyError(RuntimeError):
    """The requested source is outside the single approved M1 lane."""


class SourceInfrastructureError(RuntimeError):
    """Trusted Git/archive processing failed."""


@dataclass(frozen=True)
class SourceSnapshot:
    """Credential-free bytes and identities captured from one Git revision."""

    repository_root: Path
    approved_repo_path: Path
    base_commit: str
    base_tree: str
    source_tree: str
    archive_digest: str
    content_tree_digest: str
    files: Mapping[str, bytes]

    def content(self, path: str) -> bytes:
        try:
            return self.files[path]
        except KeyError as exc:
            raise SourcePolicyError(f"required committed file is missing: {path}") from exc


@dataclass(frozen=True)
class CandidatePatch:
    """An exact, allowlisted set of replacement bytes for a proof run."""

    replacements: Mapping[str, bytes]
    patch_digest: str
    candidate_tree_digest: str


def default_repository_root() -> Path:
    """Return the controller source root containing this installed M1 build."""

    return Path(__file__).resolve().parents[3]


def default_approved_repo_path() -> Path:
    return default_repository_root() / "fixtures" / "notes-app"


def capture_approved_source(
    requested_repo: Path,
    *,
    approved_repo: Path | None = None,
) -> SourceSnapshot:
    """Capture the exact committed controlled fixture after policy checks."""

    approved = (approved_repo or default_approved_repo_path()).absolute()
    requested = requested_repo.absolute()
    _assert_plain_path(approved)
    _assert_plain_path(requested)
    if requested.resolve(strict=True) != approved.resolve(strict=True):
        raise SourcePolicyError(
            f"repository is not the controller-approved M1 fixture: {requested}"
        )

    repository_root = approved.parent.parent
    relative_repo = approved.relative_to(repository_root).as_posix()
    if not (repository_root / ".git").exists():
        raise SourceInfrastructureError("controller source root is not a Git checkout")

    base_commit = _git_text(
        repository_root, "rev-parse", "--verify", "HEAD^{commit}"
    ).strip()
    base_tree = _git_text(
        repository_root, "rev-parse", f"{base_commit}^{{tree}}"
    ).strip()
    source_tree = _git_text(
        repository_root, "rev-parse", f"{base_commit}:{relative_repo}"
    ).strip()
    for name, value in (
        ("base commit", base_commit),
        ("base tree", base_tree),
        ("source tree", source_tree),
    ):
        if not _is_git_object_id(value):
            raise SourceInfrastructureError(f"Git returned an invalid {name}")

    committed_entries = _read_committed_entries(repository_root, source_tree)
    committed_files: dict[str, bytes] = {}
    total = 0
    for path, (_, object_id) in committed_entries.items():
        committed = _git_bytes(repository_root, "cat-file", "blob", object_id)
        if len(committed) > MAX_FILE_BYTES:
            raise SourcePolicyError(f"source file exceeds its byte limit: {path}")
        _validate_text(path, committed)
        committed_files[path] = committed
        total += len(committed)
        if total > MAX_SOURCE_BYTES:
            raise SourcePolicyError("source snapshot exceeds its bounded limits")
    archive = _build_canonical_archive(committed_files, committed_entries)
    if not archive or len(archive) > MAX_ARCHIVE_BYTES:
        raise SourcePolicyError("approved source archive is empty or exceeds its byte limit")
    files = _read_safe_archive(archive)
    if files != committed_files:
        raise SourceInfrastructureError("canonical source archive did not preserve exact blobs")
    _require_fixture_files(files)
    return SourceSnapshot(
        repository_root=repository_root.resolve(),
        approved_repo_path=approved.resolve(),
        base_commit=base_commit,
        base_tree=base_tree,
        source_tree=source_tree,
        archive_digest=_content_ref(archive),
        content_tree_digest=content_tree_digest(files),
        files=MappingProxyType(files),
    )


def build_candidate_patch(
    snapshot: SourceSnapshot,
    replacements: Mapping[str, bytes],
) -> CandidatePatch:
    """Validate and bind an exact M1 recipe/README candidate."""

    if not replacements:
        raise SourcePolicyError("candidate patch must change at least one file")
    normalized: dict[str, bytes] = {}
    for path, content in replacements.items():
        _validate_relative_path(path)
        if path not in ALLOWED_CANDIDATE_PATHS:
            raise SourcePolicyError(f"candidate path is not allowed in M1: {path}")
        if path not in snapshot.files:
            raise SourcePolicyError(f"candidate cannot create an untracked path: {path}")
        if not isinstance(content, bytes) or len(content) > MAX_FILE_BYTES:
            raise SourcePolicyError(f"candidate content is invalid or oversized: {path}")
        _validate_text(path, content)
        if content == snapshot.files[path]:
            continue
        normalized[path] = content
    if not normalized:
        raise SourcePolicyError("candidate patch makes no byte changes")

    candidate_files = dict(snapshot.files)
    candidate_files.update(normalized)
    patch_hasher = hashlib.sha256()
    for path in sorted(normalized):
        _update_tree_hash(patch_hasher, path, normalized[path])
    return CandidatePatch(
        replacements=MappingProxyType(normalized),
        patch_digest="sha256:" + patch_hasher.hexdigest(),
        candidate_tree_digest=content_tree_digest(candidate_files),
    )


def read_committed_controller_file(
    snapshot: SourceSnapshot,
    relative_path: str,
    *,
    max_bytes: int = MAX_FILE_BYTES,
) -> bytes:
    """Read one controller asset from the exact revision bound to a snapshot."""

    _validate_relative_path(relative_path)
    if type(max_bytes) is not int or max_bytes <= 0 or max_bytes > MAX_FILE_BYTES:
        raise ValueError("max_bytes must be within the controller file limit")
    object_id = _git_text(
        snapshot.repository_root,
        "rev-parse",
        "--verify",
        f"{snapshot.base_commit}:{relative_path}",
    ).strip()
    if not _is_git_object_id(object_id):
        raise SourceInfrastructureError("Git returned an invalid controller asset identity")
    content = _git_bytes(snapshot.repository_root, "cat-file", "blob", object_id)
    if len(content) > max_bytes:
        raise SourcePolicyError(f"controller asset exceeds its byte limit: {relative_path}")
    _validate_text(relative_path, content)
    return content


def content_tree_digest(files: Mapping[str, bytes]) -> str:
    hasher = hashlib.sha256()
    for path in sorted(files):
        _update_tree_hash(hasher, path, files[path])
    return "sha256:" + hasher.hexdigest()


def file_digests(files: Mapping[str, bytes]) -> Mapping[str, str]:
    return MappingProxyType({path: _content_ref(content) for path, content in files.items()})


def _read_committed_entries(root: Path, source_tree: str) -> dict[str, tuple[str, str]]:
    """Return regular blob paths/OIDs from an exact tree; reject gitlinks and links."""

    output = _git_bytes(root, "ls-tree", "-r", "-z", "--full-tree", source_tree)
    entries: dict[str, tuple[str, str]] = {}
    for raw_entry in output.split(b"\x00"):
        if not raw_entry:
            continue
        try:
            raw_metadata, raw_path = raw_entry.split(b"\t", 1)
            mode, object_type, raw_object_id = raw_metadata.split(b" ", 2)
            path = raw_path.decode("utf-8", errors="strict")
            object_id = raw_object_id.decode("ascii", errors="strict")
        except (ValueError, UnicodeError) as exc:
            raise SourceInfrastructureError("Git returned an invalid source tree entry") from exc
        _validate_relative_path(path)
        # M1 materializes every source file as 0644 on a noexec tmpfs.  Reject
        # executable Git entries so recorded and executed filesystem semantics
        # cannot diverge.
        if mode != b"100644" or object_type != b"blob":
            raise SourcePolicyError(f"unsupported committed source entry: {path}")
        if not _is_git_object_id(object_id):
            raise SourceInfrastructureError("Git returned an invalid source blob identity")
        if path in entries:
            raise SourceInfrastructureError("Git returned a duplicate source tree entry")
        entries[path] = (mode.decode("ascii"), object_id)
        if len(entries) > MAX_SOURCE_FILES:
            raise SourcePolicyError("source tree contains too many files")
    if not entries:
        raise SourcePolicyError("approved committed source tree is empty")
    return entries


def _build_canonical_archive(
    files: Mapping[str, bytes],
    entries: Mapping[str, tuple[str, str]],
) -> bytes:
    """Build the deterministic credential-free archive passed to fresh workers."""

    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:", format=tarfile.GNU_FORMAT) as archive:
        for path in sorted(files):
            content = files[path]
            info = tarfile.TarInfo(path)
            info.size = len(content)
            info.mode = 0o644
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = 0
            archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


def _read_safe_archive(archive: bytes) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
            members = tar.getmembers()
            if len(members) > MAX_SOURCE_FILES * 3:
                raise SourcePolicyError("source archive contains too many entries")
            for member in members:
                path = member.name.rstrip("/")
                if not path:
                    continue
                _validate_relative_path(path)
                if member.isdir():
                    continue
                if not member.isreg() or member.issym() or member.islnk():
                    raise SourcePolicyError(f"unsupported archive entry type: {path}")
                if path in files:
                    raise SourcePolicyError(f"duplicate source archive path: {path}")
                if member.size < 0 or member.size > MAX_FILE_BYTES:
                    raise SourcePolicyError(f"source file exceeds its byte limit: {path}")
                extracted = tar.extractfile(member)
                if extracted is None:
                    raise SourceInfrastructureError(f"could not read archive member: {path}")
                content = extracted.read(MAX_FILE_BYTES + 1)
                if len(content) != member.size or len(content) > MAX_FILE_BYTES:
                    raise SourcePolicyError(f"source file size is invalid: {path}")
                _validate_text(path, content)
                files[path] = content
                total += len(content)
                if len(files) > MAX_SOURCE_FILES or total > MAX_SOURCE_BYTES:
                    raise SourcePolicyError("source snapshot exceeds its bounded limits")
    except (tarfile.TarError, OSError) as exc:
        raise SourceInfrastructureError(f"could not parse trusted Git archive: {exc}") from exc
    return files


def _require_fixture_files(files: Mapping[str, bytes]) -> None:
    required = {
        ".firstrun/recipe.json",
        ".firstrun/target.json",
        "README.md",
        "package.json",
        "package-lock.json",
    }
    missing = sorted(required.difference(files))
    if missing:
        raise SourcePolicyError("approved source is missing: " + ", ".join(missing))


def _assert_plain_path(path: Path) -> None:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise SourcePolicyError(f"path does not resolve to an existing location: {path}") from exc
    if resolved != path:
        raise SourcePolicyError(f"path resolution is ambiguous: {path}")
    anchor = Path(resolved.anchor)
    current = anchor
    for part in resolved.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except OSError as exc:
            raise SourcePolicyError(f"could not inspect path component: {current}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise SourcePolicyError(f"symbolic links are not allowed: {current}")
        attributes = getattr(info, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if attributes & reparse_flag:
            raise SourcePolicyError(f"reparse points are not allowed: {current}")


def _validate_relative_path(value: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 240:
        raise SourcePolicyError("source path is empty or oversized")
    if (
        "\\" in value
        or ":" in value
        or any(
            ord(character) < 32
            or ord(character) == 127
            or 0xD800 <= ord(character) <= 0xDFFF
            for character in value
        )
    ):
        raise SourcePolicyError(f"source path is ambiguous: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value:
        raise SourcePolicyError(f"source path is not normalized and relative: {value!r}")


def _validate_text(path: str, content: bytes) -> None:
    if b"\x00" in content:
        raise SourcePolicyError(f"binary source content is unsupported: {path}")
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourcePolicyError(f"non-UTF-8 source content is unsupported: {path}") from exc
    if content.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
        raise SourcePolicyError(f"Git LFS pointer content is unsupported in M1: {path}")


def _git_text(root: Path, *args: str) -> str:
    try:
        return _git(root, args).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SourceInfrastructureError("Git returned non-UTF-8 controller output") from exc


def _git_bytes(root: Path, *args: str) -> bytes:
    return _git(root, args)


def _git(root: Path, args: tuple[str, ...]) -> bytes:
    selected = shutil.which("git")
    if selected is None:
        raise SourceInfrastructureError("trusted Git executable was not found")
    try:
        program = Path(selected).resolve(strict=True)
        program_info = program.stat()
    except OSError as exc:
        raise SourceInfrastructureError("trusted Git executable could not be inspected") from exc
    if not stat.S_ISREG(program_info.st_mode):
        raise SourceInfrastructureError("trusted Git executable is not a regular file")
    _assert_plain_path(program)
    try:
        program.relative_to(root.resolve())
    except ValueError:
        pass
    else:
        raise SourceInfrastructureError(
            "refused a Git executable beneath the controller source checkout"
        )
    environment = {
        name: value
        for name, value in os.environ.items()
        if name.upper() in {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
    }
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GCM_INTERACTIVE": "never",
        }
    )
    command = (
        str(program),
        "--no-pager",
        "--no-replace-objects",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "-c",
        "core.hooksPath=",
        "-c",
        "credential.helper=",
        "-c",
        "protocol.allow=never",
        "-C",
        str(root),
        *args,
    )
    process_options: dict[str, object] = {}
    if os.name == "nt":
        process_options["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    else:
        process_options["start_new_session"] = True
    try:
        process = subprocess.Popen(
            command,
            cwd=program.parent,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            **process_options,
        )
    except OSError as exc:
        raise SourceInfrastructureError("trusted Git process could not be started") from exc
    assert process.stdout is not None and process.stderr is not None
    stdout = bytearray()
    stderr = bytearray()
    stdout_overflow = threading.Event()

    def drain(
        stream: object,
        destination: bytearray,
        limit: int,
        *,
        kill_on_overflow: bool,
    ) -> None:
        try:
            while True:
                chunk = stream.read(65_536)  # type: ignore[attr-defined]
                if not chunk:
                    return
                remaining = max(0, limit - len(destination))
                destination.extend(chunk[:remaining])
                if len(chunk) > remaining and kill_on_overflow:
                    stdout_overflow.set()
                    _terminate_process_tree(process)
        except (OSError, ValueError):
            return

    stdout_thread = threading.Thread(
        target=drain,
        args=(process.stdout, stdout, MAX_ARCHIVE_BYTES),
        kwargs={"kill_on_overflow": True},
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=drain,
        args=(process.stderr, stderr, 16_384),
        kwargs={"kill_on_overflow": False},
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    timeout_error: subprocess.TimeoutExpired | None = None
    try:
        returncode = process.wait(timeout=20)
    except subprocess.TimeoutExpired as exc:
        timeout_error = exc
        returncode = -1
        _terminate_process_tree(process)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    stdout_thread.join(timeout=5)
    stderr_thread.join(timeout=5)
    drains_stuck = stdout_thread.is_alive() or stderr_thread.is_alive()
    if drains_stuck:
        _terminate_process_tree(process)
    for stream in (process.stdout, process.stderr):
        try:
            stream.close()
        except OSError:
            pass
    if drains_stuck:
        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)
        raise SourceInfrastructureError("trusted Git output streams did not terminate")
    if timeout_error is not None:
        raise SourceInfrastructureError("trusted Git command exceeded its deadline") from timeout_error
    if stdout_overflow.is_set():
        raise SourceInfrastructureError("trusted Git output exceeded its hard byte limit")
    if returncode != 0:
        raise SourceInfrastructureError(
            f"trusted Git command failed with exit {returncode}"
        )
    return bytes(stdout)


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Best-effort termination of the isolated Git process group."""

    process_id = getattr(process, "pid", None)
    if os.name != "nt" and isinstance(process_id, int):
        try:
            os.killpg(process_id, signal.SIGKILL)
        except OSError:
            pass
    try:
        process.kill()
    except OSError:
        pass


def _is_git_object_id(value: str) -> bool:
    return len(value) in (40, 64) and all(character in "0123456789abcdef" for character in value)


def _content_ref(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _update_tree_hash(hasher: object, path: str, content: bytes) -> None:
    # Explicit separators and lengths avoid concatenation ambiguity.
    encoded_path = path.encode("utf-8")
    hasher.update(len(encoded_path).to_bytes(4, "big"))  # type: ignore[attr-defined]
    hasher.update(encoded_path)  # type: ignore[attr-defined]
    hasher.update(len(content).to_bytes(8, "big"))  # type: ignore[attr-defined]
    hasher.update(content)  # type: ignore[attr-defined]
