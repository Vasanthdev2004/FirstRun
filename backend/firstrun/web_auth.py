"""GitHub App user login and repository-scoped web sessions.

GitHub user access tokens live only inside :class:`GitHubOAuthProvider` while it
identifies the caller.  Browser sessions are independent opaque values whose
hashes, bounded identity, CSRF nonce, and expiry are stored in the controller's
existing SQLite database.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
import sqlite3
import ssl
import stat
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlencode, urlsplit

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from firstrun.github_state import RepositoryRegistration
from firstrun.domain.web_session import SessionView
from firstrun.integrations.github import API_VERSION


AUTH_HOST = "github.com"
API_HOST = "api.github.com"
SESSION_COOKIE = "firstrun_session"
OAUTH_NONCE_COOKIE = "firstrun_oauth_nonce"
FLOW_TTL_SECONDS = 600
SESSION_TTL_SECONDS = 3600
MAX_AUTH_RESPONSE_BYTES = 256 * 1024

_CLIENT_ID = re.compile(r"^[A-Za-z0-9._-]{8,128}$")
_OPAQUE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_PERMISSIONS = frozenset({"none", "read", "write", "admin"})


class WebAuthError(RuntimeError):
    """A bounded error safe to turn into a generic web response."""

    def __init__(self, code: str, message: str, *, status: int = 401) -> None:
        super().__init__(message[:160])
        self.code = code[:64]
        self.status = status


@dataclass(frozen=True, slots=True)
class WebAuthConfig:
    client_id: str
    client_secret_path: Path
    public_origin: str
    allow_insecure_localhost: bool = False

    def __post_init__(self) -> None:
        if type(self.allow_insecure_localhost) is not bool:
            raise ValueError("allow_insecure_localhost must be a boolean")
        if not isinstance(self.client_id, str) or _CLIENT_ID.fullmatch(self.client_id) is None:
            raise ValueError("client_id is not a bounded GitHub client identifier")
        if not isinstance(self.client_secret_path, Path) or not self.client_secret_path.is_absolute():
            raise ValueError("client_secret_path must be an absolute pathlib.Path")
        normalized = Path(os.path.normpath(self.client_secret_path))
        if normalized != self.client_secret_path or self.client_secret_path.name in {"", ".", ".."}:
            raise ValueError("client_secret_path must be a normalized file path")
        _validate_origin(self.public_origin, self.allow_insecure_localhost)

    @property
    def callback_url(self) -> str:
        return f"{self.public_origin}/api/auth/callback"

    @property
    def secure_cookie(self) -> bool:
        return urlsplit(self.public_origin).scheme == "https"


@dataclass(frozen=True, slots=True)
class GitHubIdentity:
    id: int
    login: str


@dataclass(frozen=True, slots=True)
class UserAuthorization:
    user: GitHubIdentity
    permission: str

    @property
    def can_write(self) -> bool:
        return self.permission in {"write", "admin"}


@dataclass(frozen=True, slots=True)
class LoginStart:
    authorization_url: str
    browser_nonce: str


@dataclass(frozen=True, slots=True)
class AuthSession:
    user: GitHubIdentity
    repository_id: int
    csrf_token: str
    expires_at: int
    can_write: bool


@dataclass(frozen=True, slots=True)
class SessionGrant:
    session_token: str
    session: AuthSession


class OAuthProvider(Protocol):
    def authenticate(
        self, code: str, code_verifier: str, redirect_uri: str
    ) -> UserAuthorization:
        ...


class InstallationPermissionClient(Protocol):
    @property
    def prefix(self) -> str:
        ...

    def request(
        self, method: str, path: str, body: Mapping[str, object] | None = None
    ) -> Any:
        ...


@dataclass(frozen=True, slots=True)
class OAuthTransportResponse:
    status: int
    body: bytes


class OAuthTransport(Protocol):
    def request(
        self,
        host: str,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> OAuthTransportResponse:
        ...


class StdlibOAuthTransport:
    """Fixed-origin HTTPS transport which never follows redirects."""

    def request(
        self,
        host: str,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> OAuthTransportResponse:
        if host not in {AUTH_HOST, API_HOST} or method not in {"GET", "POST"} or not _safe_path(path):
            raise WebAuthError("invalid_oauth_request", "GitHub login request was invalid.")
        connection = http.client.HTTPSConnection(
            host, 443, timeout=10.0, context=ssl.create_default_context()
        )
        try:
            connection.request(method, path, body=body, headers=dict(headers))
            response = connection.getresponse()
            content = response.read(MAX_AUTH_RESPONSE_BYTES + 1)
            if len(content) > MAX_AUTH_RESPONSE_BYTES:
                raise WebAuthError(
                    "oauth_response_too_large", "GitHub login returned too much data.", status=502
                )
            return OAuthTransportResponse(response.status, content)
        except WebAuthError:
            raise
        except (OSError, TimeoutError, http.client.HTTPException):
            raise WebAuthError(
                "oauth_unavailable", "GitHub login is temporarily unavailable.", status=503
            ) from None
        finally:
            connection.close()


class GitHubOAuthProvider:
    """Exchange a one-use code, authorize the configured repo, then discard it."""

    def __init__(
        self,
        config: WebAuthConfig,
        registration: RepositoryRegistration,
        *,
        transport: OAuthTransport | None = None,
    ) -> None:
        self.config = config
        self.registration = registration
        self.transport = transport or StdlibOAuthTransport()

    def authenticate(
        self, code: str, code_verifier: str, redirect_uri: str
    ) -> UserAuthorization:
        _require_opaque(code, "authorization code", minimum=1, maximum=2048)
        _require_opaque(code_verifier, "PKCE verifier")
        if redirect_uri != self.config.callback_url:
            raise WebAuthError("redirect_mismatch", "GitHub login redirect did not match.")
        secret = _read_client_secret(self.config.client_secret_path)
        form = urlencode(
            {
                "client_id": self.config.client_id,
                "client_secret": secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
                "repository_id": str(self.registration.repository_id),
            }
        ).encode("ascii")
        token_value = self._json_request(
            AUTH_HOST,
            "POST",
            "/login/oauth/access_token",
            {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            form,
        )
        token = token_value.get("access_token")
        token_type = token_value.get("token_type")
        if (
            not isinstance(token, str)
            or not 8 <= len(token) <= 8192
            or any(character.isspace() or ord(character) < 32 for character in token)
            or not isinstance(token_type, str)
            or token_type.casefold() != "bearer"
        ):
            raise WebAuthError("oauth_exchange_rejected", "GitHub login could not be completed.")
        try:
            headers = {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "FirstRun/0.1",
                "X-GitHub-Api-Version": API_VERSION,
            }
            user_value = self._json_request(API_HOST, "GET", "/user", headers, None)
            user = _identity(user_value)
            repository_path = (
                f"/repos/{quote(self.registration.owner, safe='')}/"
                f"{quote(self.registration.name, safe='')}"
            )
            repository_value = self._json_request(
                API_HOST, "GET", repository_path, headers, None
            )
            expected_name = f"{self.registration.owner}/{self.registration.name}"
            if (
                repository_value.get("id") != self.registration.repository_id
                or not isinstance(repository_value.get("full_name"), str)
                or repository_value["full_name"].casefold() != expected_name.casefold()
            ):
                raise WebAuthError(
                    "repository_access_denied",
                    "Repository membership is required.",
                    status=403,
                )
            path = (
                f"{repository_path}/collaborators/"
                f"{quote(user.login, safe='')}/permission"
            )
            permission_value = self._json_request(API_HOST, "GET", path, headers, None)
            return _authorization(permission_value, user)
        finally:
            # Do not hand the GitHub token to the manager, a model, SQLite, or a browser.
            token = ""

    def _json_request(
        self,
        host: str,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> dict[str, object]:
        try:
            response = self.transport.request(host, method, path, headers, body)
        except WebAuthError:
            raise
        except Exception:
            raise WebAuthError(
                "oauth_unavailable", "GitHub login is temporarily unavailable.", status=503
            ) from None
        if not isinstance(response, OAuthTransportResponse):
            raise WebAuthError("oauth_invalid_response", "GitHub login returned invalid data.", status=502)
        if not 200 <= response.status < 300:
            status = 401 if response.status in {400, 401, 403, 404} else 503
            raise WebAuthError("oauth_rejected", "GitHub login was rejected.", status=status)
        try:
            value = json.loads(
                response.body.decode("utf-8", errors="strict"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (UnicodeError, ValueError, json.JSONDecodeError):
            raise WebAuthError(
                "oauth_invalid_response", "GitHub login returned invalid data.", status=502
            ) from None
        if not isinstance(value, dict):
            raise WebAuthError("oauth_invalid_response", "GitHub login returned invalid data.", status=502)
        return value


class WebAuthManager:
    """One configured repository's durable, short-lived web session authority."""

    def __init__(
        self,
        config: WebAuthConfig,
        database: Path,
        registration: RepositoryRegistration,
        installation_client: InstallationPermissionClient,
        *,
        oauth_provider: OAuthProvider | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(config, WebAuthConfig):
            raise TypeError("config must be WebAuthConfig")
        if not isinstance(database, Path):
            raise TypeError("database must be a pathlib.Path")
        if database.exists() and database.is_symlink():
            raise ValueError("SQLite path must not be a symlink")
        if not database.parent.exists() or not database.parent.is_dir():
            raise ValueError("SQLite parent directory must already exist")
        if not isinstance(registration, RepositoryRegistration):
            raise TypeError("registration must be RepositoryRegistration")
        expected_prefix = (
            f"/repos/{quote(registration.owner, safe='')}/{quote(registration.name, safe='')}"
        )
        if getattr(installation_client, "prefix", None) != expected_prefix:
            raise ValueError("installation client is not bound to the registered repository")
        client_config = getattr(installation_client, "config", None)
        if client_config is not None and getattr(client_config, "repository_id", None) != registration.repository_id:
            raise ValueError("installation client repository identity does not match registration")
        self.config = config
        self.database = database.absolute()
        self.registration = registration
        self.installation_client = installation_client
        self.oauth_provider = oauth_provider or GitHubOAuthProvider(config, registration)
        self._clock = clock
        self._initialize()

    def start_login(self) -> LoginStart:
        state = _random_token()
        browser_nonce = _random_token()
        verifier = _pkce_verifier(_read_client_secret(self.config.client_secret_path), state)
        challenge = _base64url(hashlib.sha256(verifier.encode("ascii")).digest())
        now = self._now()
        with self._transaction() as connection:
            self._purge(connection, now)
            connection.execute(
                "INSERT INTO web_oauth_flows (state_hash, browser_nonce_hash, expires_at) "
                "VALUES (?, ?, ?)",
                (_hash_token(state), _hash_token(browser_nonce), now + FLOW_TTL_SECONDS),
            )
        query = urlencode(
            {
                "client_id": self.config.client_id,
                "redirect_uri": self.config.callback_url,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "allow_signup": "false",
            }
        )
        return LoginStart(f"https://{AUTH_HOST}/login/oauth/authorize?{query}", browser_nonce)

    def finish_login(self, state: str, code: str, browser_nonce: str) -> SessionGrant:
        _require_opaque(state, "OAuth state")
        _require_opaque(browser_nonce, "browser nonce")
        _require_opaque(code, "authorization code", minimum=1, maximum=2048)
        now = self._now()
        state_hash = _hash_token(state)
        with self._transaction() as connection:
            self._purge(connection, now)
            row = connection.execute(
                "SELECT browser_nonce_hash, expires_at FROM web_oauth_flows WHERE state_hash = ?",
                (state_hash,),
            ).fetchone()
            if (
                row is None
                or int(row["expires_at"]) <= now
                or not hmac.compare_digest(str(row["browser_nonce_hash"]), _hash_token(browser_nonce))
            ):
                raise WebAuthError("invalid_oauth_state", "GitHub login state was invalid.")
            deleted = connection.execute(
                "DELETE FROM web_oauth_flows WHERE state_hash = ?", (state_hash,)
            )
            if deleted.rowcount != 1:
                raise WebAuthError("invalid_oauth_state", "GitHub login state was invalid.")
        verifier = _pkce_verifier(_read_client_secret(self.config.client_secret_path), state)
        try:
            authorization = self.oauth_provider.authenticate(
                code, verifier, self.config.callback_url
            )
        except WebAuthError:
            raise
        except Exception:
            raise WebAuthError(
                "oauth_unavailable", "GitHub login is temporarily unavailable.", status=503
            ) from None
        authorization = _validated_authorization(authorization)
        if authorization.permission == "none":
            raise WebAuthError("repository_access_denied", "Repository membership is required.", status=403)
        session_token = _random_token()
        csrf_token = _random_token()
        expires_at = self._now() + SESSION_TTL_SECONDS
        with self._transaction() as connection:
            self._purge(connection, self._now())
            connection.execute(
                """INSERT INTO web_sessions
                   (token_hash, github_user_id, login, csrf_nonce, expires_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    _hash_token(session_token),
                    authorization.user.id,
                    authorization.user.login,
                    csrf_token,
                    expires_at,
                    self._now(),
                ),
            )
        return SessionGrant(
            session_token,
            AuthSession(
                authorization.user,
                self.registration.repository_id,
                csrf_token,
                expires_at,
                authorization.can_write,
            ),
        )

    def require(
        self, request: Request, *, require_csrf: bool = False, require_write: bool = False
    ) -> AuthSession:
        token = _request_cookie(request, SESSION_COOKIE)
        _require_opaque(token, "session token")
        now = self._now()
        token_hash = _hash_token(token)
        with self._transaction() as connection:
            self._purge(connection, now)
            row = connection.execute(
                "SELECT * FROM web_sessions WHERE token_hash = ?", (token_hash,)
            ).fetchone()
        if row is None or int(row["expires_at"]) <= now:
            raise WebAuthError("session_required", "A current login session is required.")
        user = _identity({"id": row["github_user_id"], "login": row["login"]})
        csrf_token = str(row["csrf_nonce"])
        if require_csrf:
            self._require_csrf(request, csrf_token)
        try:
            value = self.installation_client.request(
                "GET", f"{self.installation_client.prefix}/collaborators/{quote(user.login, safe='')}/permission"
            )
        except Exception:
            raise WebAuthError(
                "authorization_unavailable",
                "Current repository authorization could not be checked.",
                status=503,
            ) from None
        try:
            authorization = _authorization(value, user)
        except WebAuthError:
            self._delete_session(token_hash)
            raise
        if authorization.permission == "none":
            self._delete_session(token_hash)
            raise WebAuthError(
                "authorization_revoked", "Repository access is no longer available.", status=403
            )
        if (require_write or require_csrf) and not authorization.can_write:
            raise WebAuthError(
                "write_access_required", "Repository write permission is required.", status=403
            )
        return AuthSession(
            user,
            self.registration.repository_id,
            csrf_token,
            int(row["expires_at"]),
            authorization.can_write,
        )

    def logout(self, request: Request) -> None:
        token = _request_cookie(request, SESSION_COOKIE)
        _require_opaque(token, "session token")
        token_hash = _hash_token(token)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT csrf_nonce, expires_at FROM web_sessions WHERE token_hash = ?", (token_hash,)
            ).fetchone()
            if row is None or int(row["expires_at"]) <= self._now():
                raise WebAuthError("session_required", "A current login session is required.")
            self._require_csrf(request, str(row["csrf_nonce"]))
            connection.execute("DELETE FROM web_sessions WHERE token_hash = ?", (token_hash,))

    def _require_csrf(self, request: Request, expected: str) -> None:
        origins = request.headers.getlist("origin")
        csrf_values = request.headers.getlist("x-csrf-token")
        if (
            len(origins) != 1
            or origins[0] != self.config.public_origin
            or len(csrf_values) != 1
            or not hmac.compare_digest(csrf_values[0], expected)
        ):
            raise WebAuthError("csrf_rejected", "Request origin or CSRF proof was invalid.", status=403)

    def _delete_session(self, token_hash: str) -> None:
        with self._transaction() as connection:
            connection.execute("DELETE FROM web_sessions WHERE token_hash = ?", (token_hash,))

    def _now(self) -> int:
        value = int(self._clock())
        if value < 0:
            raise ValueError("clock returned an invalid epoch")
        return value

    @contextmanager
    def _connect(self) -> Any:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                self.database, timeout=5.0, isolation_level=None, check_same_thread=False
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            yield connection
        except sqlite3.Error:
            raise WebAuthError(
                "auth_store_unavailable", "Login state is temporarily unavailable.", status=503
            ) from None
        finally:
            if connection is not None:
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
        with self._transaction() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS web_oauth_flows (
                    state_hash TEXT PRIMARY KEY,
                    browser_nonce_hash TEXT NOT NULL,
                    expires_at INTEGER NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS web_sessions (
                    token_hash TEXT PRIMARY KEY,
                    github_user_id INTEGER NOT NULL,
                    login TEXT NOT NULL,
                    csrf_nonce TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                )"""
            )

    @staticmethod
    def _purge(connection: sqlite3.Connection, now: int) -> None:
        connection.execute("DELETE FROM web_oauth_flows WHERE expires_at <= ?", (now,))
        connection.execute("DELETE FROM web_sessions WHERE expires_at <= ?", (now,))


def install_auth_routes(app: FastAPI, manager: WebAuthManager | None) -> None:
    """Mount the narrow M4 auth surface; ``None`` is a fail-closed configuration."""

    @app.get("/api/session")
    def session_status(request: Request) -> Response:
        if manager is None:
            return _no_store({"authenticated": False, "configured": False, "user": None,
                              "csrf_token": None, "can_write": False, "reason": "not_configured"})
        try:
            session = manager.require(request)
        except WebAuthError as exc:
            return _no_store({"authenticated": False, "configured": True, "user": None,
                              "csrf_token": None, "can_write": False, "reason": exc.code})
        return _no_store({
            "authenticated": True,
            "configured": True,
            "user": {"login": session.user.login, "id": session.user.id},
            "csrf_token": session.csrf_token,
            "can_write": session.can_write,
            "reason": None,
        })

    @app.get("/api/auth/login")
    def login() -> Response:
        if manager is None:
            return _error_response("GitHub login is not configured.", 503)
        try:
            start = manager.start_login()
        except (WebAuthError, OSError, sqlite3.Error):
            return _error_response("GitHub login is unavailable.", 503)
        response = RedirectResponse(start.authorization_url, status_code=302)
        _secure_response(response)
        response.set_cookie(
            OAUTH_NONCE_COOKIE,
            start.browser_nonce,
            max_age=FLOW_TTL_SECONDS,
            httponly=True,
            secure=manager.config.secure_cookie,
            samesite="lax",
            path="/",
        )
        return response

    @app.get("/api/auth/callback")
    def callback(request: Request, state: str = "", code: str = "") -> Response:
        if manager is None:
            return _error_response("GitHub login is not configured.", 503)
        try:
            nonce = _request_cookie(request, OAUTH_NONCE_COOKIE)
            grant = manager.finish_login(state, code, nonce)
        except (WebAuthError, OSError, sqlite3.Error):
            response = _error_response("GitHub login could not be completed.", 401)
            _delete_cookie(response, OAUTH_NONCE_COOKIE, manager.config.secure_cookie)
            return response
        response = RedirectResponse(manager.config.public_origin, status_code=303)
        _secure_response(response)
        response.set_cookie(
            SESSION_COOKIE,
            grant.session_token,
            max_age=max(0, grant.session.expires_at - manager._now()),
            httponly=True,
            secure=manager.config.secure_cookie,
            samesite="lax",
            path="/",
        )
        _delete_cookie(response, OAUTH_NONCE_COOKIE, manager.config.secure_cookie)
        return response

    @app.post("/api/auth/logout")
    def logout(request: Request) -> Response:
        if manager is None:
            return _error_response("GitHub login is not configured.", 503)
        try:
            manager.logout(request)
        except WebAuthError as exc:
            return _error_response(str(exc), exc.status)
        response = _no_store({"authenticated": False, "configured": True, "user": None,
                              "csrf_token": None, "can_write": False, "reason": None})
        _delete_cookie(response, SESSION_COOKIE, manager.config.secure_cookie)
        return response


def _validated_authorization(value: object) -> UserAuthorization:
    if not isinstance(value, UserAuthorization) or not isinstance(value.user, GitHubIdentity):
        raise WebAuthError("oauth_invalid_response", "GitHub login returned invalid data.", status=502)
    user = _identity({"id": value.user.id, "login": value.user.login})
    if value.permission not in _PERMISSIONS:
        raise WebAuthError("oauth_invalid_response", "GitHub login returned invalid data.", status=502)
    return UserAuthorization(user, value.permission)


def _authorization(value: object, expected_user: GitHubIdentity) -> UserAuthorization:
    if not isinstance(value, dict):
        raise WebAuthError("authorization_invalid", "Repository authorization was invalid.", status=502)
    permission = value.get("permission")
    user_value = value.get("user")
    if not isinstance(permission, str) or permission not in _PERMISSIONS or not isinstance(user_value, dict):
        raise WebAuthError("authorization_invalid", "Repository authorization was invalid.", status=502)
    actual = _identity(user_value)
    if actual.id != expected_user.id or actual.login.casefold() != expected_user.login.casefold():
        raise WebAuthError("authorization_revoked", "Repository authorization no longer matches.", status=403)
    return UserAuthorization(expected_user, permission)


def _identity(value: Mapping[str, object]) -> GitHubIdentity:
    user_id = value.get("id")
    login = value.get("login")
    if type(user_id) is not int or not 1 <= user_id <= 2**63 - 1 or not isinstance(login, str) or _LOGIN.fullmatch(login) is None:
        raise WebAuthError("identity_invalid", "GitHub identity was invalid.", status=502)
    return GitHubIdentity(user_id, login)


def _validate_origin(origin: str, allow_insecure_localhost: bool) -> None:
    if not isinstance(origin, str) or not 1 <= len(origin) <= 2048 or origin != origin.strip():
        raise ValueError("public_origin must be a bounded absolute origin")
    parsed = urlsplit(origin)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("public_origin has an invalid port") from exc
    if (
        parsed.scheme not in {"https", "http"}
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.hostname
        or parsed.path
        or parsed.query
        or parsed.fragment
        or any(ord(character) > 127 or ord(character) < 33 for character in origin)
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError("public_origin must contain only scheme and authority")
    if parsed.scheme == "http" and not (
        allow_insecure_localhost and parsed.hostname.casefold() in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ValueError("public_origin must use HTTPS unless explicit loopback development is enabled")


def _read_client_secret(path: Path) -> str:
    descriptor = -1
    try:
        before = os.lstat(path)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode) or not 16 <= before.st_size <= 8192:
            raise OSError
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise OSError
        content = os.read(descriptor, 8193)
    except OSError:
        raise WebAuthError("client_secret_unavailable", "GitHub login is unavailable.", status=503) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        secret = content.decode("ascii", errors="strict")
    except UnicodeError:
        raise WebAuthError("client_secret_unavailable", "GitHub login is unavailable.", status=503) from None
    if not 16 <= len(secret) <= 8192 or any(character.isspace() or ord(character) < 33 for character in secret):
        raise WebAuthError("client_secret_unavailable", "GitHub login is unavailable.", status=503)
    return secret


def _pkce_verifier(secret: str, state: str) -> str:
    return _base64url(hmac.new(secret.encode("ascii"), b"firstrun-pkce-v1:" + state.encode("ascii"), hashlib.sha256).digest())


def _random_token() -> str:
    return secrets.token_urlsafe(32)


def _hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _require_opaque(value: object, label: str, *, minimum: int = 32, maximum: int = 128) -> None:
    if (
        not isinstance(value, str)
        or not minimum <= len(value) <= maximum
        or (minimum >= 32 and _OPAQUE.fullmatch(value) is None)
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise WebAuthError("invalid_auth_value", f"{label} was invalid.")


def _request_cookie(request: Request, name: str) -> str:
    values: list[str] = []
    for header in request.headers.getlist("cookie"):
        for item in header.split(";"):
            key, separator, value = item.strip().partition("=")
            if separator and key == name:
                values.append(value)
    if len(values) != 1:
        raise WebAuthError("session_required", "A current login session is required.")
    return values[0]


def _safe_path(path: str) -> bool:
    return (
        isinstance(path, str)
        and path.startswith("/")
        and not path.startswith("//")
        and len(path) <= 4096
        and "\\" not in path
        and "#" not in path
        and not any(ord(character) < 32 or ord(character) == 127 for character in path)
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_constant(_value: str) -> None:
    raise ValueError("unsupported JSON constant")


def _secure_response(response: JSONResponse | RedirectResponse) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"


def _no_store(value: dict[str, object], status: int = 200) -> JSONResponse:
    if "authenticated" in value:
        value = SessionView.model_validate(value).model_dump(mode="json")
    response = JSONResponse(value, status_code=status)
    _secure_response(response)
    return response


def _error_response(message: str, status: int) -> JSONResponse:
    return _no_store({"detail": message[:160]}, status)


def _delete_cookie(response: JSONResponse | RedirectResponse, name: str, secure: bool) -> None:
    response.delete_cookie(name, path="/", secure=secure, httponly=True, samesite="lax")


__all__ = [
    "AuthSession",
    "GitHubIdentity",
    "GitHubOAuthProvider",
    "LoginStart",
    "OAuthProvider",
    "SessionGrant",
    "UserAuthorization",
    "WebAuthConfig",
    "WebAuthError",
    "WebAuthManager",
    "install_auth_routes",
]
