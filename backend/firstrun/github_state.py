"""Durable single-controller state for the bounded M3 GitHub lane.

This module stores identities and sanitized controller evidence only.  GitHub App
private keys, webhook secrets, raw webhook bodies, and installation tokens never
belong in this database.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from firstrun.domain.outcomes import Outcome
from firstrun.verification.source import content_tree_digest


MAX_STATE_JSON_BYTES = 4 * 1024 * 1024
MIN_LEASE_SECONDS = 1_200
MAX_LEASE_SECONDS = 7_200
REPAIRABLE_PATHS = frozenset({".firstrun/recipe.json", "README.md"})

_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_LEASE_OWNER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{15,127}$")
_DELIVERY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,127}$")
_JOURNAL_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")

Digest = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=71,
        max_length=71,
        pattern=r"^sha256:[0-9a-f]{64}$",
    ),
]
GitSha = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=40,
        max_length=40,
        pattern=r"^[0-9a-f]{40}$",
    ),
]
CasePhase = Literal[
    "queued",
    "fetching",
    "baseline_running",
    "investigating",
    "needs_input",
    "proof_running",
    "verified",
    "repair_ready",
    "publishing",
    "reconcile_pending",
    "pr_open",
    "unresolved",
    "blocked",
    "stale",
    "cancelled",
    "interrupted",
    "quarantined",
]

_PHASES = frozenset(CasePhase.__args__)
_EXECUTION_PHASES = frozenset(
    {"baseline_running", "investigating", "proof_running"}
)
_CLAIMABLE_PHASES = (
    "reconcile_pending",
    "publishing",
    "repair_ready",
    "queued",
    "fetching",
)
_TERMINAL_PHASES = frozenset(
    {
        "needs_input",
        "verified",
        "pr_open",
        "unresolved",
        "blocked",
        "stale",
        "cancelled",
    }
)
_RECOVERY_BLOCKING_PHASES = frozenset({"interrupted", "quarantined"})
_UNSET = object()


class GitHubStateError(ValueError):
    """Base class for durable M3 state failures."""


class LeaseConflictError(GitHubStateError):
    """The caller does not own the current live fenced lease."""


class RecoveryRequiredError(GitHubStateError):
    """A repository is quarantined pending explicit execution cleanup."""


class JournalConflictError(GitHubStateError):
    """A stable write key was reused for different immutable content."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RepositoryRegistration(_FrozenModel):
    """Owner-approved identity and immutable verification basis for one repo."""

    app_id: Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]
    installation_id: Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]
    repository_id: Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]
    owner: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=39)]
    name: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=100)]
    branch: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=255)] = (
        "main"
    )
    approved_sha: GitSha
    source_prefix: Literal["", "fixtures/notes-app"] = ""
    target_digest: Digest
    approved_recipe_digest: Digest
    protected_digest: Digest
    verifier_digest: Digest
    policy_revision: Annotated[
        str,
        StringConstraints(
            strict=True,
            min_length=1,
            max_length=64,
            pattern=r"^[a-z][a-z0-9_-]{0,63}$",
        ),
    ]
    policy_digest: Digest
    runtime_digest: Digest
    runtime_image_id: Digest
    author_name: Annotated[
        str, StringConstraints(strict=True, min_length=1, max_length=100)
    ]
    author_email: Annotated[
        str, StringConstraints(strict=True, min_length=3, max_length=254)
    ]
    automatic_prs: bool = False

    @field_validator("owner")
    @classmethod
    def _safe_owner(cls, value: str) -> str:
        if _OWNER.fullmatch(value) is None:
            raise ValueError("owner is not a safe GitHub login")
        return value

    @field_validator("name")
    @classmethod
    def _safe_name(cls, value: str) -> str:
        if _REPOSITORY.fullmatch(value) is None or value in {".", ".."}:
            raise ValueError("name is not a safe GitHub repository name")
        return value

    @field_validator("branch")
    @classmethod
    def _safe_branch(cls, value: str) -> str:
        if not _is_safe_branch(value):
            raise ValueError("branch is not a safe normalized Git ref name")
        return value

    @field_validator("author_name")
    @classmethod
    def _safe_author_name(cls, value: str) -> str:
        if value != value.strip() or any(char in value for char in "\x00\r\n<>"):
            raise ValueError("author_name contains unsafe characters")
        return value

    @field_validator("author_email")
    @classmethod
    def _safe_author_email(cls, value: str) -> str:
        if (
            value != value.strip()
            or value.count("@") != 1
            or any(char in value for char in "\x00\r\n <>\t")
        ):
            raise ValueError("author_email is not a safe email address")
        local, domain = value.rsplit("@", 1)
        if not local or not domain or domain.startswith(".") or domain.endswith("."):
            raise ValueError("author_email is not a safe email address")
        return value

    @property
    def fingerprint(self) -> str:
        return _digest_json(self.model_dump(mode="json"))


class GitHubConfig(_FrozenModel):
    """Paths to operator-managed credentials; secret bytes are never configuration."""

    registration: RepositoryRegistration
    private_key_path: Path
    webhook_secret_path: Path

    @field_validator("private_key_path", "webhook_secret_path")
    @classmethod
    def _safe_secret_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("credential paths must be absolute")
        normalized = Path(os.path.normpath(value))
        if normalized != value or value.name in {"", ".", ".."}:
            raise ValueError("credential paths must be normalized file paths")
        return value


@dataclass(frozen=True)
class DeliveryIngestResult:
    status: Literal["accepted", "duplicate"]
    provider: str
    delivery_id: str
    repository_id: int
    sha: str
    case_id: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "provider": self.provider,
            "delivery_id": self.delivery_id,
            "repository_id": self.repository_id,
            "sha": self.sha,
            "case_id": self.case_id,
        }


class SQLiteStore:
    """Small transactional store with durable dedupe, leases, and write journal."""

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        if not isinstance(path, Path):
            raise TypeError("SQLite path must be a pathlib.Path")
        if path.exists() and path.is_symlink():
            raise ValueError("SQLite path must not be a symlink")
        if not path.parent.exists() or not path.parent.is_dir():
            raise ValueError("SQLite parent directory must already exist")
        self.path = path.absolute()
        self._clock = clock
        self._initialize()

    def enqueue(self, registration: RepositoryRegistration, sha: str) -> str:
        _require_sha(sha)
        with self._transaction() as connection:
            self._recover_expired(connection)
            return self._enqueue(connection, registration, sha)

    def ingest_delivery(
        self,
        provider: str,
        delivery_id: str,
        event: str,
        registration: RepositoryRegistration,
        sha: str,
    ) -> DeliveryIngestResult:
        if provider != "github" or event != "push":
            raise ValueError("only GitHub push deliveries are supported")
        if _DELIVERY.fullmatch(delivery_id) is None:
            raise ValueError("delivery identifier is invalid")
        _require_sha(sha)
        now = self._now()
        with self._transaction() as connection:
            self._recover_expired(connection)
            existing = connection.execute(
                "SELECT * FROM deliveries WHERE provider = ? AND delivery_id = ?",
                (provider, delivery_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["event"] != event
                    or int(existing["repository_id"]) != registration.repository_id
                    or existing["sha"] != sha
                    or existing["registration_fingerprint"]
                    != registration.fingerprint
                ):
                    raise GitHubStateError(
                        "delivery identifier is already bound to another event"
                    )
                return DeliveryIngestResult(
                    "duplicate",
                    provider,
                    delivery_id,
                    int(existing["repository_id"]),
                    str(existing["sha"]),
                    existing["case_id"],
                )
            case_id = self._enqueue(connection, registration, sha)
            status: Literal["accepted"] = "accepted"
            connection.execute(
                """INSERT INTO deliveries
                   (provider, delivery_id, event, repository_id, sha,
                    registration_fingerprint, status, case_id, received_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    provider,
                    delivery_id,
                    event,
                    registration.repository_id,
                    sha,
                    registration.fingerprint,
                    status,
                    case_id,
                    now,
                ),
            )
            if case_id is not None:
                self._event(
                    connection,
                    case_id,
                    "webhook_accepted",
                    self._case_phase(connection, case_id),
                    {"provider": provider, "delivery_id": delivery_id, "sha": sha},
                )
            return DeliveryIngestResult(
                status,
                provider,
                delivery_id,
                registration.repository_id,
                sha,
                case_id,
            )

    def claim(self, owner: str, seconds: int) -> dict[str, object] | None:
        _require_lease_owner(owner)
        _require_lease_seconds(seconds)
        now = self._now()
        with self._transaction() as connection:
            self._recover_expired(connection)
            if connection.execute(
                "SELECT 1 FROM lease_claims WHERE owner = ?", (owner,)
            ).fetchone():
                raise LeaseConflictError("lease owner token has already been used")
            placeholders = ",".join("?" for _ in _CLAIMABLE_PHASES)
            row = connection.execute(
                f"""SELECT candidate.* FROM cases AS candidate
                    WHERE candidate.lease_owner IS NULL
                      AND candidate.phase IN ({placeholders})
                      AND NOT EXISTS (
                        SELECT 1 FROM cases AS active
                        WHERE active.repository_id = candidate.repository_id
                          AND active.lease_owner IS NOT NULL
                      )
                      AND NOT EXISTS (
                        SELECT 1 FROM cases AS blocked
                        WHERE blocked.repository_id = candidate.repository_id
                          AND blocked.phase IN ('interrupted', 'quarantined')
                      )
                    ORDER BY CASE candidate.phase
                        WHEN 'reconcile_pending' THEN 0
                        WHEN 'publishing' THEN 1
                        WHEN 'repair_ready' THEN 2
                        ELSE 3 END,
                      candidate.created_at, candidate.case_id
                    LIMIT 1""",
                _CLAIMABLE_PHASES,
            ).fetchone()
            if row is None:
                return None
            case_id = str(row["case_id"])
            expires = now + seconds
            connection.execute(
                "UPDATE cases SET lease_owner = ?, lease_expires_at = ?, updated_at = ? "
                "WHERE case_id = ? AND lease_owner IS NULL",
                (owner, expires, now, case_id),
            )
            connection.execute(
                "INSERT INTO lease_claims (owner, case_id, claimed_at) VALUES (?, ?, ?)",
                (owner, case_id, now),
            )
            self._event(connection, case_id, "lease_claimed", str(row["phase"]), {})
            return self._get_case(connection, case_id)

    def heartbeat(self, case_id: str, owner: str, seconds: int) -> dict[str, object]:
        _require_lease_owner(owner)
        _require_lease_seconds(seconds)
        now = self._now()
        with self._transaction() as connection:
            self._require_fence(connection, case_id, owner, now)
            connection.execute(
                "UPDATE cases SET lease_expires_at = ?, updated_at = ? WHERE case_id = ?",
                (now + seconds, now, case_id),
            )
            return self._get_case(connection, case_id)

    def update_case(
        self,
        case_id: str,
        owner: str,
        phase: CasePhase,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        _require_phase(phase)
        now = self._now()
        with self._transaction() as connection:
            row = self._require_fence(connection, case_id, owner, now)
            if str(row["phase"]) in _TERMINAL_PHASES | _RECOVERY_BLOCKING_PHASES:
                raise GitHubStateError("terminal or quarantined case cannot transition")
            merged = _decode_object(str(row["payload_json"]))
            delta = _json_object(payload or {})
            merged.update(delta)
            encoded = _encode_object(merged)
            connection.execute(
                "UPDATE cases SET phase = ?, payload_json = ?, updated_at = ? "
                "WHERE case_id = ?",
                (phase, encoded, now, case_id),
            )
            self._event(connection, case_id, "phase_updated", phase, delta)
            return self._get_case(connection, case_id)

    def finish(
        self,
        case_id: str,
        owner: str,
        phase: CasePhase,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        _require_phase(phase)
        if phase in _EXECUTION_PHASES or phase in {"queued", "fetching", "publishing"}:
            raise ValueError("finish requires a non-executing released phase")
        now = self._now()
        with self._transaction() as connection:
            row = self._require_fence(connection, case_id, owner, now)
            merged = _decode_object(str(row["payload_json"]))
            delta = _json_object(payload or {})
            merged.update(delta)
            connection.execute(
                """UPDATE cases SET phase = ?, payload_json = ?, lease_owner = NULL,
                   lease_expires_at = NULL, updated_at = ? WHERE case_id = ?""",
                (phase, _encode_object(merged), now, case_id),
            )
            self._event(connection, case_id, "finished", phase, delta)
            return self._get_case(connection, case_id)

    def release(self, case_id: str, owner: str) -> dict[str, object]:
        now = self._now()
        with self._transaction() as connection:
            row = self._require_fence(connection, case_id, owner, now)
            previous = str(row["phase"])
            if previous in _EXECUTION_PHASES:
                raise GitHubStateError("executing case cannot be released without cleanup")
            phase = "queued" if previous == "fetching" else previous
            if previous == "publishing":
                phase = "reconcile_pending"
            connection.execute(
                """UPDATE cases SET phase = ?, lease_owner = NULL,
                   lease_expires_at = NULL, updated_at = ? WHERE case_id = ?""",
                (phase, now, case_id),
            )
            self._event(connection, case_id, "lease_released", phase, {})
            return self._get_case(connection, case_id)

    def journal_intent(
        self,
        case_id: str,
        owner: str,
        key: str,
        method: str,
        path: str,
        body: Mapping[str, object],
    ) -> dict[str, object]:
        _require_journal_request(key, method, path)
        body_json = _encode_object(_json_object(body))
        request_digest = _digest_json(
            {"method": method, "path": path, "body": _decode_object(body_json)}
        )
        now = self._now()
        with self._transaction() as connection:
            row = self._require_fence(connection, case_id, owner, now)
            if str(row["phase"]) not in {"publishing", "reconcile_pending"}:
                raise GitHubStateError("write intents require a publishing case")
            existing = connection.execute(
                "SELECT * FROM write_journal WHERE case_id = ? AND journal_key = ?",
                (case_id, key),
            ).fetchone()
            if existing is not None:
                if (
                    existing["method"] != method
                    or existing["path"] != path
                    or existing["body_json"] != body_json
                    or existing["request_digest"] != request_digest
                ):
                    raise JournalConflictError(
                        "journal key is already bound to another request"
                    )
                return _journal_from_row(existing, created=False)
            connection.execute(
                """INSERT INTO write_journal
                   (case_id, journal_key, method, path, body_json, request_digest,
                    state, result_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?)""",
                (
                    case_id,
                    key,
                    method,
                    path,
                    body_json,
                    request_digest,
                    now,
                    now,
                ),
            )
            self._event(
                connection,
                case_id,
                "write_intent",
                str(row["phase"]),
                {"key": key, "request_digest": request_digest},
            )
            return self._get_journal(connection, case_id, key, created=True)

    def journal_result(
        self,
        case_id: str,
        owner: str,
        key: str,
        result: Mapping[str, object],
    ) -> dict[str, object]:
        if _JOURNAL_KEY.fullmatch(key) is None:
            raise ValueError("journal key is invalid")
        result_json = _encode_object(_json_object(result))
        now = self._now()
        with self._transaction() as connection:
            row = self._require_fence(connection, case_id, owner, now)
            existing = connection.execute(
                "SELECT * FROM write_journal WHERE case_id = ? AND journal_key = ?",
                (case_id, key),
            ).fetchone()
            if existing is None:
                raise JournalConflictError("write intent does not exist")
            if existing["state"] == "confirmed":
                if existing["result_json"] != result_json:
                    raise JournalConflictError(
                        "confirmed journal result cannot be replaced"
                    )
                return _journal_from_row(existing, created=False)
            changed = connection.execute(
                """UPDATE write_journal SET state = 'confirmed', result_json = ?,
                   updated_at = ? WHERE case_id = ? AND journal_key = ?
                   AND state = 'pending'""",
                (result_json, now, case_id, key),
            ).rowcount
            if changed != 1:
                raise JournalConflictError("write journal confirmation lost its CAS")
            self._event(
                connection,
                case_id,
                "write_confirmed",
                str(row["phase"]),
                {"key": key},
            )
            return self._get_journal(connection, case_id, key, created=False)

    def get_case(self, case_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM cases WHERE case_id = ?", (case_id,)
            ).fetchone()
            return _case_from_row(row) if row is not None else None

    def get_events(self, case_id: str) -> tuple[dict[str, object], ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM case_events WHERE case_id = ? ORDER BY event_id",
                (case_id,),
            ).fetchall()
        return tuple(
            {
                "event_id": int(row["event_id"]),
                "case_id": str(row["case_id"]),
                "kind": str(row["kind"]),
                "phase": str(row["phase"]),
                "payload": _decode_object(str(row["payload_json"])),
                "created_at": int(row["created_at"]),
            }
            for row in rows
        )

    def get_journal(self, case_id: str, key: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM write_journal WHERE case_id = ? AND journal_key = ?",
                (case_id, key),
            ).fetchone()
            return _journal_from_row(row, created=False) if row is not None else None

    def set_health(
        self,
        repository_id: int,
        *,
        head: str | None | object = _UNSET,
        checked_sha: str | None | object = _UNSET,
        outcome: Outcome | str | None | object = _UNSET,
    ) -> dict[str, object]:
        _require_positive_id(repository_id, "repository_id")
        if head is not _UNSET and head is not None:
            _require_sha(head)
        if checked_sha is not _UNSET and checked_sha is not None:
            _require_sha(checked_sha)
        if outcome is not _UNSET and outcome is not None:
            try:
                outcome = Outcome(outcome).value
            except ValueError as exc:
                raise ValueError("health outcome is invalid") from exc
        now = self._now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM repository_health WHERE repository_id = ?",
                (repository_id,),
            ).fetchone()
            values = {
                "head": row["head"] if row is not None else None,
                "checked_sha": row["checked_sha"] if row is not None else None,
                "outcome": row["outcome"] if row is not None else None,
            }
            for name, value in (
                ("head", head),
                ("checked_sha", checked_sha),
                ("outcome", outcome),
            ):
                if value is not _UNSET:
                    values[name] = value
            if values["outcome"] is not None and values["checked_sha"] is None:
                raise ValueError("health outcome requires checked_sha")
            connection.execute(
                """INSERT INTO repository_health
                   (repository_id, head, checked_sha, outcome, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(repository_id) DO UPDATE SET
                     head = excluded.head,
                     checked_sha = excluded.checked_sha,
                     outcome = excluded.outcome,
                     updated_at = excluded.updated_at""",
                (
                    repository_id,
                    values["head"],
                    values["checked_sha"],
                    values["outcome"],
                    now,
                ),
            )
            return self._get_health(connection, repository_id)

    def get_health(self, repository_id: int) -> dict[str, object] | None:
        _require_positive_id(repository_id, "repository_id")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM repository_health WHERE repository_id = ?",
                (repository_id,),
            ).fetchone()
            return _health_from_row(row) if row is not None else None

    def _enqueue(
        self,
        connection: sqlite3.Connection,
        registration: RepositoryRegistration,
        sha: str,
    ) -> str:
        if not isinstance(registration, RepositoryRegistration):
            raise TypeError("registration must be RepositoryRegistration")
        logical_key = _digest_json(
            {
                "provider": "github",
                "repository_id": registration.repository_id,
                "sha": sha,
                "registration_fingerprint": registration.fingerprint,
            }
        )
        existing = connection.execute(
            "SELECT case_id FROM cases WHERE logical_key = ?", (logical_key,)
        ).fetchone()
        if existing is not None:
            return str(existing["case_id"])
        now = self._now()
        stale_rows = connection.execute(
            """SELECT case_id FROM cases WHERE repository_id = ? AND sha != ?
               AND lease_owner IS NULL AND phase IN ('queued', 'fetching', 'repair_ready')""",
            (registration.repository_id, sha),
        ).fetchall()
        for stale in stale_rows:
            stale_id = str(stale["case_id"])
            connection.execute(
                "UPDATE cases SET phase = 'stale', updated_at = ? WHERE case_id = ?",
                (now, stale_id),
            )
            self._event(connection, stale_id, "superseded", "stale", {"new_sha": sha})
        case_id = "case_" + logical_key.split(":", 1)[1][:32]
        connection.execute(
            """INSERT INTO cases
               (case_id, logical_key, repository_id, sha, registration_json, phase,
                payload_json, lease_owner, lease_expires_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'queued', '{}', NULL, NULL, ?, ?)""",
            (
                case_id,
                logical_key,
                registration.repository_id,
                sha,
                _encode_object(registration.model_dump(mode="json")),
                now,
                now,
            ),
        )
        self._event(connection, case_id, "enqueued", "queued", {"sha": sha})
        return case_id

    def _recover_expired(self, connection: sqlite3.Connection) -> None:
        now = self._now()
        rows = connection.execute(
            "SELECT case_id, phase FROM cases WHERE lease_owner IS NOT NULL "
            "AND lease_expires_at <= ?",
            (now,),
        ).fetchall()
        for row in rows:
            previous = str(row["phase"])
            if previous in _EXECUTION_PHASES:
                recovered = "interrupted"
            elif previous == "fetching":
                recovered = "queued"
            elif previous == "publishing":
                recovered = "reconcile_pending"
            else:
                recovered = previous
            case_id = str(row["case_id"])
            connection.execute(
                """UPDATE cases SET phase = ?, lease_owner = NULL,
                   lease_expires_at = NULL, updated_at = ? WHERE case_id = ?""",
                (recovered, now, case_id),
            )
            self._event(
                connection,
                case_id,
                "lease_expired",
                recovered,
                {"previous_phase": previous},
            )

    def _require_fence(
        self, connection: sqlite3.Connection, case_id: str, owner: str, now: int
    ) -> sqlite3.Row:
        _require_lease_owner(owner)
        row = connection.execute(
            "SELECT * FROM cases WHERE case_id = ?", (case_id,)
        ).fetchone()
        if row is None:
            raise GitHubStateError("case does not exist")
        if row["lease_owner"] != owner or int(row["lease_expires_at"] or 0) <= now:
            self._recover_expired(connection)
            raise LeaseConflictError("caller does not hold the live fenced lease")
        return row

    def _event(
        self,
        connection: sqlite3.Connection,
        case_id: str,
        kind: str,
        phase: str,
        payload: Mapping[str, object],
    ) -> None:
        connection.execute(
            """INSERT INTO case_events
               (case_id, kind, phase, payload_json, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (case_id, kind, phase, _encode_object(_json_object(payload)), self._now()),
        )

    def _case_phase(self, connection: sqlite3.Connection, case_id: str) -> str:
        row = connection.execute(
            "SELECT phase FROM cases WHERE case_id = ?", (case_id,)
        ).fetchone()
        if row is None:
            raise GitHubStateError("case does not exist")
        return str(row["phase"])

    def _get_case(self, connection: sqlite3.Connection, case_id: str) -> dict[str, object]:
        row = connection.execute(
            "SELECT * FROM cases WHERE case_id = ?", (case_id,)
        ).fetchone()
        if row is None:
            raise GitHubStateError("case does not exist")
        return _case_from_row(row)

    def _get_journal(
        self,
        connection: sqlite3.Connection,
        case_id: str,
        key: str,
        *,
        created: bool,
    ) -> dict[str, object]:
        row = connection.execute(
            "SELECT * FROM write_journal WHERE case_id = ? AND journal_key = ?",
            (case_id, key),
        ).fetchone()
        if row is None:
            raise JournalConflictError("write journal record does not exist")
        return _journal_from_row(row, created=created)

    def _get_health(
        self, connection: sqlite3.Connection, repository_id: int
    ) -> dict[str, object]:
        row = connection.execute(
            "SELECT * FROM repository_health WHERE repository_id = ?",
            (repository_id,),
        ).fetchone()
        if row is None:
            raise GitHubStateError("repository health record does not exist")
        return _health_from_row(row)

    def _now(self) -> int:
        value = int(self._clock())
        if value < 0:
            raise ValueError("clock returned an invalid epoch")
        return value

    @contextmanager
    def _connect(self) -> Any:
        connection = sqlite3.connect(
            self.path, timeout=5.0, isolation_level=None, check_same_thread=False
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Any:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
        statements = (
            """CREATE TABLE IF NOT EXISTS cases (
                case_id TEXT PRIMARY KEY,
                logical_key TEXT NOT NULL UNIQUE,
                repository_id INTEGER NOT NULL,
                sha TEXT NOT NULL,
                registration_json TEXT NOT NULL,
                phase TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                lease_owner TEXT,
                lease_expires_at INTEGER,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL))
            )""",
            """CREATE UNIQUE INDEX IF NOT EXISTS one_live_repo_lease
               ON cases(repository_id) WHERE lease_owner IS NOT NULL""",
            """CREATE TABLE IF NOT EXISTS deliveries (
                provider TEXT NOT NULL,
                delivery_id TEXT NOT NULL,
                event TEXT NOT NULL,
                repository_id INTEGER NOT NULL,
                sha TEXT NOT NULL,
                registration_fingerprint TEXT NOT NULL,
                status TEXT NOT NULL,
                case_id TEXT REFERENCES cases(case_id),
                received_at INTEGER NOT NULL,
                PRIMARY KEY (provider, delivery_id)
            )""",
            """CREATE TABLE IF NOT EXISTS case_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id TEXT NOT NULL REFERENCES cases(case_id),
                kind TEXT NOT NULL,
                phase TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS lease_claims (
                owner TEXT PRIMARY KEY,
                case_id TEXT NOT NULL REFERENCES cases(case_id),
                claimed_at INTEGER NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS write_journal (
                case_id TEXT NOT NULL REFERENCES cases(case_id),
                journal_key TEXT NOT NULL,
                method TEXT NOT NULL,
                path TEXT NOT NULL,
                body_json TEXT NOT NULL,
                request_digest TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('pending', 'confirmed')),
                result_json TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (case_id, journal_key)
            )""",
            """CREATE TABLE IF NOT EXISTS repository_health (
                repository_id INTEGER PRIMARY KEY,
                head TEXT,
                checked_sha TEXT,
                outcome TEXT,
                updated_at INTEGER NOT NULL
            )""",
        )
        with self._transaction() as connection:
            for statement in statements:
                connection.execute(statement)


def protected_source_digest(files: Mapping[str, bytes]) -> str:
    """Hash every source byte except the two explicitly repairable paths."""

    protected = {path: content for path, content in files.items() if path not in REPAIRABLE_PATHS}
    if not protected:
        raise ValueError("protected source set cannot be empty")
    return content_tree_digest(protected)


def _is_safe_branch(value: str) -> bool:
    if (
        value != value.strip()
        or value.startswith(("/", ".", "refs/"))
        or value.endswith(("/", ".", ".lock"))
        or ".." in value
        or "@{" in value
        or "//" in value
        or any(char in value for char in "\\~^:?*[\x00\r\n\t ")
    ):
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and str(path) == value and all(
        part and not part.startswith(".") and not part.endswith(".lock")
        for part in path.parts
    )


def _require_sha(value: object) -> None:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise ValueError("Git SHA must be 40 lowercase hexadecimal characters")


def _require_positive_id(value: object, name: str) -> None:
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        raise ValueError(f"{name} must be a positive integer")


def _require_phase(value: object) -> None:
    if not isinstance(value, str) or value not in _PHASES:
        raise ValueError("case phase is invalid")


def _require_lease_owner(value: object) -> None:
    if not isinstance(value, str) or _LEASE_OWNER.fullmatch(value) is None:
        raise ValueError("lease owner must be a unique opaque token")


def _require_lease_seconds(value: object) -> None:
    if type(value) is not int or not MIN_LEASE_SECONDS <= value <= MAX_LEASE_SECONDS:
        raise ValueError(
            f"lease seconds must be between {MIN_LEASE_SECONDS} and {MAX_LEASE_SECONDS}"
        )


def _require_journal_request(key: str, method: str, path: str) -> None:
    if _JOURNAL_KEY.fullmatch(key) is None:
        raise ValueError("journal key is invalid")
    if method not in {"POST", "PUT", "PATCH"}:
        raise ValueError("journal method is not an allowed GitHub write")
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or len(path) > 1_024
        or "//" in path
        or any(char in path for char in "\x00\r\n\t ")
    ):
        raise ValueError("journal path is not a safe GitHub API path")


def _json_object(value: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("state payload must be a mapping")
    encoded = _encode_object(dict(value))
    return _decode_object(encoded)


def _encode_object(value: Mapping[str, object]) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("state payload must be bounded JSON") from exc
    if len(encoded.encode("ascii")) > MAX_STATE_JSON_BYTES:
        raise ValueError("state payload exceeds its byte limit")
    return encoded


def _decode_object(value: str) -> dict[str, object]:
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise GitHubStateError("stored JSON object is invalid")
    return decoded


def _digest_json(value: Mapping[str, object]) -> str:
    return "sha256:" + hashlib.sha256(_encode_object(value).encode("ascii")).hexdigest()


def _case_from_row(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": str(row["case_id"]),
        "repository_id": int(row["repository_id"]),
        "sha": str(row["sha"]),
        "registration": _decode_object(str(row["registration_json"])),
        "phase": str(row["phase"]),
        "payload": _decode_object(str(row["payload_json"])),
        "lease_owner": row["lease_owner"],
        "lease_expires_at": row["lease_expires_at"],
        "created_at": int(row["created_at"]),
        "updated_at": int(row["updated_at"]),
    }


def _journal_from_row(row: sqlite3.Row, *, created: bool) -> dict[str, object]:
    return {
        "case_id": str(row["case_id"]),
        "key": str(row["journal_key"]),
        "state": str(row["state"]),
        "created": created,
        "request": {
            "method": str(row["method"]),
            "path": str(row["path"]),
            "body": _decode_object(str(row["body_json"])),
        },
        "request_digest": str(row["request_digest"]),
        "result": (
            _decode_object(str(row["result_json"]))
            if row["result_json"] is not None
            else None
        ),
        "created_at": int(row["created_at"]),
        "updated_at": int(row["updated_at"]),
    }


def _health_from_row(row: sqlite3.Row) -> dict[str, object]:
    return {
        "repository_id": int(row["repository_id"]),
        "head": row["head"],
        "checked_sha": row["checked_sha"],
        "outcome": row["outcome"],
        "updated_at": int(row["updated_at"]),
    }


__all__ = [
    "DeliveryIngestResult",
    "GitHubConfig",
    "GitHubStateError",
    "JournalConflictError",
    "LeaseConflictError",
    "RecoveryRequiredError",
    "RepositoryRegistration",
    "SQLiteStore",
    "protected_source_digest",
]
