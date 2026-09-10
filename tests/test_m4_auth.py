"""Offline security checks for the M4 GitHub user-login boundary."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from firstrun.github_state import RepositoryRegistration
from firstrun.web_auth import (
    GitHubIdentity,
    GitHubOAuthProvider,
    OAuthTransportResponse,
    UserAuthorization,
    WebAuthConfig,
    WebAuthError,
    WebAuthManager,
    install_auth_routes,
)


def registration() -> RepositoryRegistration:
    return RepositoryRegistration(
        app_id=1,
        installation_id=2,
        repository_id=3,
        owner="owner",
        name="demo",
        branch="main",
        approved_sha="a" * 40,
        source_prefix="",
        target_digest="sha256:" + "1" * 64,
        approved_recipe_digest="sha256:" + "2" * 64,
        protected_digest="sha256:" + "3" * 64,
        verifier_digest="sha256:" + "4" * 64,
        policy_revision="m1-policy-v1",
        policy_digest="sha256:" + "5" * 64,
        runtime_digest="sha256:" + "6" * 64,
        runtime_image_id="sha256:" + "7" * 64,
        author_name="Owner",
        author_email="owner@example.com",
    )


class FakeOAuth:
    def __init__(self, permission: str = "write") -> None:
        self.permission = permission
        self.calls: list[tuple[str, str, str]] = []

    def authenticate(self, code: str, verifier: str, redirect_uri: str) -> UserAuthorization:
        self.calls.append((code, verifier, redirect_uri))
        return UserAuthorization(GitHubIdentity(42, "octocat"), self.permission)


class FakeInstallation:
    prefix = "/repos/owner/demo"

    def __init__(self, permission: str = "write") -> None:
        self.permission = permission
        self.calls = 0

    def request(self, method: str, path: str, body=None):
        self.calls += 1
        return {
            "permission": self.permission,
            "user": {"id": 42, "login": "octocat"},
        }


def request(
    method: str,
    token: str,
    *,
    origin: str | None = None,
    csrf: str | None = None,
    duplicate_origin: bool = False,
) -> Request:
    headers = [(b"cookie", f"firstrun_session={token}".encode("ascii"))]
    if origin is not None:
        headers.append((b"origin", origin.encode("ascii")))
        if duplicate_origin:
            headers.append((b"origin", origin.encode("ascii")))
    if csrf is not None:
        headers.append((b"x-csrf-token", csrf.encode("ascii")))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 1234),
            "server": ("localhost", 3000),
        }
    )


class AuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.secret = self.root / "github-client-secret"
        self.secret.write_text("s" * 40, encoding="ascii")
        self.config = WebAuthConfig(
            "Iv1.0123456789abcdef",
            self.secret,
            "http://localhost:3000",
            allow_insecure_localhost=True,
        )
        self.oauth = FakeOAuth()
        self.installation = FakeInstallation()
        self.manager = WebAuthManager(
            self.config,
            self.root / "state.sqlite",
            registration(),
            self.installation,
            oauth_provider=self.oauth,
            clock=lambda: 1_700_000_000,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def login(self):
        start = self.manager.start_login()
        state = parse_qs(urlsplit(start.authorization_url).query)["state"][0]
        return start, state, self.manager.finish_login(state, "code-123", start.browser_nonce)

    def test_config_requires_https_or_explicit_exact_loopback(self) -> None:
        with self.assertRaises(ValueError):
            WebAuthConfig("Iv1.0123456789abcdef", self.secret, "http://localhost:3000")
        with self.assertRaises(ValueError):
            WebAuthConfig(
                "Iv1.0123456789abcdef",
                self.secret,
                "http://localhost.example:3000",
                allow_insecure_localhost=True,
            )
        with self.assertRaises(ValueError):
            WebAuthConfig(
                "Iv1.0123456789abcdef",
                self.secret,
                "http://localhost:3000",
                allow_insecure_localhost="false",  # type: ignore[arg-type]
            )
        secure = WebAuthConfig("Iv1.0123456789abcdef", self.secret, "https://first.run")
        self.assertTrue(secure.secure_cookie)

    def test_pkce_state_is_browser_bound_one_use_and_session_token_is_hashed(self) -> None:
        start = self.manager.start_login()
        query = parse_qs(urlsplit(start.authorization_url).query)
        state = query["state"][0]
        self.assertEqual(query["code_challenge_method"], ["S256"])
        with closing(sqlite3.connect(self.root / "state.sqlite")) as connection:
            flow = connection.execute(
                "SELECT state_hash, browser_nonce_hash FROM web_oauth_flows"
            ).fetchone()
        self.assertEqual(flow[0], hashlib.sha256(state.encode("ascii")).hexdigest())
        self.assertNotEqual(flow[1], start.browser_nonce)
        with self.assertRaises(WebAuthError):
            self.manager.finish_login(state, "code-123", "A" * 43)
        grant = self.manager.finish_login(state, "code-123", start.browser_nonce)
        verifier = self.oauth.calls[0][1]
        expected = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(expected).rstrip(b"=").decode("ascii")
        self.assertEqual(query["code_challenge"], [challenge])
        with self.assertRaises(WebAuthError):
            self.manager.finish_login(state, "code-123", start.browser_nonce)
        with closing(sqlite3.connect(self.root / "state.sqlite")) as connection:
            row = connection.execute(
                "SELECT token_hash, github_user_id, login, csrf_nonce, expires_at FROM web_sessions"
            ).fetchone()
        self.assertEqual(row[0], hashlib.sha256(grant.session_token.encode("ascii")).hexdigest())
        self.assertNotEqual(row[0], grant.session_token)
        self.assertEqual(row[1:3], (42, "octocat"))

    def test_each_access_rechecks_membership_and_post_requires_write_origin_csrf(self) -> None:
        _, _, grant = self.login()
        read = self.manager.require(request("GET", grant.session_token))
        self.assertTrue(read.can_write)
        self.assertEqual(read.repository_id, 3)
        self.manager.require(
            request(
                "POST",
                grant.session_token,
                origin=self.config.public_origin,
                csrf=grant.session.csrf_token,
            ),
            require_csrf=True,
        )
        with self.assertRaises(WebAuthError):
            self.manager.require(
                request(
                    "POST",
                    grant.session_token,
                    origin=self.config.public_origin,
                    csrf=grant.session.csrf_token,
                    duplicate_origin=True,
                ),
                require_csrf=True,
            )
        self.installation.permission = "read"
        with self.assertRaisesRegex(WebAuthError, "write permission"):
            self.manager.require(
                request(
                    "POST",
                    grant.session_token,
                    origin=self.config.public_origin,
                    csrf=grant.session.csrf_token,
                ),
                require_csrf=True,
            )
        self.installation.permission = "none"
        with self.assertRaises(WebAuthError):
            self.manager.require(request("GET", grant.session_token))
        self.assertGreaterEqual(self.installation.calls, 4)

    def test_routes_expose_minimal_session_and_logout_with_hardened_cookies(self) -> None:
        unconfigured = FastAPI()
        install_auth_routes(unconfigured, None)
        with TestClient(unconfigured) as client:
            self.assertEqual(
                client.get("/api/session").json(),
                {"authenticated": False, "configured": False, "user": None,
                 "csrf_token": None, "can_write": False, "reason": "not_configured"},
            )
        app = FastAPI()
        install_auth_routes(app, self.manager)
        with TestClient(app) as client:
            begin = client.get("/api/auth/login", follow_redirects=False)
            self.assertEqual(begin.status_code, 302)
            self.assertIn("HttpOnly", begin.headers["set-cookie"])
            self.assertIn("SameSite=lax", begin.headers["set-cookie"])
            state = parse_qs(urlsplit(begin.headers["location"]).query)["state"][0]
            callback = client.get(
                "/api/auth/callback", params={"state": state, "code": "code-123"},
                follow_redirects=False,
            )
            self.assertEqual(callback.status_code, 303)
            self.assertIn("firstrun_session", callback.headers["set-cookie"])
            status = client.get("/api/session").json()
            self.assertEqual(status["user"], {"login": "octocat", "id": 42})
            self.assertTrue(status["authenticated"])
            logged_out = client.post(
                "/api/auth/logout",
                headers={"Origin": self.config.public_origin, "X-CSRF-Token": status["csrf_token"]},
            )
            self.assertEqual(logged_out.status_code, 200)
            self.assertFalse(client.get("/api/session").json()["authenticated"])

    def test_concrete_provider_uses_fixed_exchange_identity_and_permission_calls(self) -> None:
        class Transport:
            def __init__(self) -> None:
                self.calls = []

            def request(self, host, method, path, headers, body):
                self.calls.append((host, method, path, headers, body))
                if path == "/login/oauth/access_token":
                    return OAuthTransportResponse(
                        200, json.dumps({"access_token": "ghu_" + "x" * 40,
                                         "token_type": "bearer"}).encode()
                    )
                if path == "/user":
                    return OAuthTransportResponse(200, b'{"id":42,"login":"octocat"}')
                if path == "/repos/owner/demo":
                    return OAuthTransportResponse(
                        200, b'{"id":3,"full_name":"owner/demo"}'
                    )
                return OAuthTransportResponse(
                    200, b'{"permission":"read","user":{"id":42,"login":"octocat"}}'
                )

        transport = Transport()
        provider = GitHubOAuthProvider(self.config, registration(), transport=transport)
        result = provider.authenticate("code-123", "V" * 43, self.config.callback_url)
        self.assertEqual(result, UserAuthorization(GitHubIdentity(42, "octocat"), "read"))
        self.assertEqual(
            [(call[0], call[1], call[2]) for call in transport.calls],
            [
                ("github.com", "POST", "/login/oauth/access_token"),
                ("api.github.com", "GET", "/user"),
                ("api.github.com", "GET", "/repos/owner/demo"),
                ("api.github.com", "GET", "/repos/owner/demo/collaborators/octocat/permission"),
            ],
        )
        self.assertIn(b"repository_id=3", transport.calls[0][4])


if __name__ == "__main__":
    unittest.main()
