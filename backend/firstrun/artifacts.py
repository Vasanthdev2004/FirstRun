"""Bounded content-addressed local artifacts, reachable only by the controller.

State rows contain digest references. No URL/path supplied by a repository or
client can become an artifact filename, and content is verified on every read.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


MAX_ARTIFACT_BYTES = 4 * 1024 * 1024


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = Path(root).absolute()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink():
            raise ValueError("Artifact directory cannot be a symbolic link")

    def put(self, value: dict[str, Any]) -> str:
        data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        if len(data) > MAX_ARTIFACT_BYTES:
            raise ValueError("Artifact exceeds the bounded byte limit")
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        path = self._path(digest)
        if path.exists():
            if self.get(digest) != value:
                raise ValueError("Artifact content mismatch")
            return digest
        # Write, flush and atomically replace before the state transaction refers
        # to it. A crash may leave an unreferenced artifact, never a partial proof.
        descriptor, name = tempfile.mkstemp(prefix=".pending-", dir=self.root)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return digest

    def get(self, digest: str) -> dict[str, Any]:
        path = self._path(digest)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_ARTIFACT_BYTES:
            raise ValueError("Artifact is unavailable or unsafe")
        data = path.read_bytes()
        if "sha256:" + hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Artifact digest mismatch")
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ValueError("Artifact must be an object")
        return result

    def _path(self, digest: str) -> Path:
        if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError("Invalid artifact identity")
        return self.root / (digest[7:] + ".json")
