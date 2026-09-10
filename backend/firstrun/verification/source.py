"""Trusted, immutable source capture for the controlled M1 fixture lane.

The local CLI is deliberately not a general repository runner.  It accepts one
controller-owned fixture path, verifies that the checked-out fixture matches Git,
then creates a bounded archive of the exact committed subtree.  Docker receives a
new materialization of these captured bytes for every phase; it never receives a
bind mount of the live checkout.
"""

from __future__ import annotations

import hashlib
import io
import stat
import subprocess
import tarfile
import tempfile
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


@dataclass
class MaterializedSource:
    """A fresh short-lived directory containing only captured source bytes."""

    root: Path
    files: Mapping[str, bytes]
    _temporary_directory: tempfile.TemporaryDirectory[str]

    def cleanup(self) -> None:
        self._temporary_directory.cleanup()

    def __enter__(self) -> MaterializedSource:
        return self

    def __exit__(self, *_: object) -> None:
        self.cleanup()


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

    dirty = _git_text(
        repository_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        relative_repo,
    )
    if dirty.strip():
        raise SourcePolicyError(
            "approved fixture has dirty or untracked bytes; commit or remove them first"
        )

    base_commit = _git_text(repository_root, "rev-parse", "--verify", "HEAD").strip()
    base_tree = _git_text(repository_root, "rev-parse", "HEAD^{tree}").strip()
    source_tree = _git_text(
        repository_root, "rev-parse", f"HEAD:{relative_repo}"
    ).strip()
    for name, value in (
        ("base commit", base_commit),
        ("base tree", base_tree),
        ("source tree", source_tree),
    ):
        if not _is_git_object_id(value):
            raise SourceInfrastructureError(f"Git returned an invalid {name}")

    archive = _git_bytes(
        repository_root,
        "archive",
        "--format=tar",
        f"HEAD:{relative_repo}",
    )
    if not archive or len(archive) > MAX_ARCHIVE_BYTES:
        raise SourcePolicyError("approved source archive is empty or exceeds its byte limit")
    files = _read_safe_archive(archive)
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


def materialize_source(
    snapshot: SourceSnapshot,
    *,
    candidate: CandidatePatch | None = None,
) -> MaterializedSource:
    """Create a new workspace from immutable bytes, optionally applying a candidate."""

    files = dict(snapshot.files)
    if candidate is not None:
        for path, content in candidate.replacements.items():
            if path not in ALLOWED_CANDIDATE_PATHS or path not in files:
                raise SourcePolicyError(f"candidate escaped the allowlist: {path}")
            files[path] = content
        if content_tree_digest(files) != candidate.candidate_tree_digest:
            raise SourcePolicyError("candidate tree digest does not match replacement bytes")

    temporary = tempfile.TemporaryDirectory(prefix="firstrun-m1-")
    root = Path(temporary.name).resolve()
    try:
        for relative, content in files.items():
            destination = root.joinpath(*PurePosixPath(relative).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
    except BaseException:
        temporary.cleanup()
        raise
    return MaterializedSource(root=root, files=MappingProxyType(files), _temporary_directory=temporary)


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
    content = _git_bytes(
        snapshot.repository_root,
        "show",
        f"{snapshot.base_commit}:{relative_path}",
    )
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
    if "\\" in value or ":" in value or "\x00" in value:
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


def _git_text(root: Path, *args: str) -> str:
    return _git(root, args).decode("utf-8", errors="strict")


def _git_bytes(root: Path, *args: str) -> bytes:
    return _git(root, args)


def _git(root: Path, args: tuple[str, ...]) -> bytes:
    try:
        completed = subprocess.run(
            ("git", "-C", str(root), *args),
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SourceInfrastructureError(f"trusted Git command failed: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace")[:1000].strip()
        raise SourceInfrastructureError(f"trusted Git command failed: {detail}")
    return completed.stdout


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
