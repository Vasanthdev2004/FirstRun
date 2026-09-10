"""Focused API-boundary regression tests, with no network/credential execution."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from fastapi.testclient import TestClient

from firstrun.api import create_app
from firstrun.domain.web import CaseSummary
from firstrun.web_auth import WebAuthError


class RouteAuth:
    """An injected test boundary, never part of CLI/application configuration."""
    def __init__(self):
        self.denied = False

    def require(self, request, require_csrf=False):
        if self.denied or request.headers.get("x-test-identity") != "owner":
            raise WebAuthError("unauthenticated", "Sign-in required", status=401)
        if require_csrf and (request.headers.get("origin") != "https://app.example"
                             or request.headers.get("x-csrf-token") != "test-csrf"):
            raise WebAuthError("csrf", "CSRF required", status=403)
        return SimpleNamespace(repository_id=33)


class ApiBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="firstrun-m4-api-")
        self.database = Path(self.temp.name) / "state.sqlite"
        self.auth = RouteAuth()
        self.service = MagicMock()
        # Auth route installation is deliberately real; only route policy lookup
        # is injected. The fake cannot log in or create an application session.
        self.client = TestClient(create_app(None, self.database, auth_manager=self.auth,
                                            web_service=self.service))
        self.owner = {"x-test-identity": "owner", "origin": "https://app.example",
                      "x-csrf-token": "test-csrf"}

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def test_all_read_and_artifact_routes_require_identity(self):
        paths = ["/api/repositories", "/api/repositories/33", "/api/repositories/33/cases",
                 "/api/repositories/33/cases/case", "/api/repositories/33/cases/case/events",
                 "/api/repositories/33/cases/case/baseline", "/api/repositories/33/cases/case/proof",
                 "/api/repositories/33/cases/case/diff"]
        for path in paths:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 401)
                self.assertIn("no-store", response.headers["cache-control"])
        self.assertEqual(self.service.mock_calls, [])

    def test_no_arbitrary_artifact_download_and_cross_repo_is_hidden(self):
        self.assertEqual(self.client.get("/api/artifacts/secret.json", headers=self.owner).status_code, 404)
        self.assertEqual(self.client.get("/api/repositories/34/cases/case/proof", headers=self.owner).status_code, 404)
        self.assertEqual(self.service.mock_calls, [])

    def test_write_auth_precedes_body_and_rejects_csrf(self):
        path = "/api/repositories/33/runs"
        self.assertEqual(self.client.post(path, content=b"not json").status_code, 401)
        self.assertEqual(self.client.post(path, json={"request_id": str(uuid4())},
                                         headers={"x-test-identity": "owner"}).status_code, 403)
        self.assertEqual(self.service.mock_calls, [])

    def test_invalid_body_is_bounded_and_not_echoed(self):
        path = "/api/repositories/33/runs"
        response = self.client.post(path, content=json.dumps({"credential": "PRIVATE-INPUT"}), headers=self.owner)
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("PRIVATE-INPUT", response.text)
        self.assertEqual(self.client.post(path, content=b"x" * 4097, headers=self.owner).status_code, 413)

    def test_accepted_run_queues_only_and_does_not_execute(self):
        self.service.start_run.return_value = CaseSummary(
            id="case", repository_id=33, sha="a" * 40, phase="queued", version=1,
            created_at=1, updated_at=1,
        )
        response = self.client.post("/api/repositories/33/runs", json={"request_id": str(uuid4())}, headers=self.owner)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["phase"], "queued")
        self.assertEqual(len(self.service.mock_calls), 1)

    def test_revocation_applies_on_next_poll(self):
        self.service.list_repositories.return_value = []
        self.assertEqual(self.client.get("/api/repositories", headers=self.owner).status_code, 200)
        self.auth.denied = True
        self.assertEqual(self.client.get("/api/repositories", headers=self.owner).status_code, 401)

    def test_unconfigured_setup_is_public_but_data_is_not(self):
        with TestClient(create_app(None, self.database)) as client:
            response = client.get("/api/session")
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()["configured"])
            self.assertFalse(response.json()["authenticated"])
            self.assertEqual(client.get("/api/repositories").status_code, 503)
            self.assertEqual(client.post("/github/webhook", json={}).status_code, 503)


if __name__ == "__main__":
    unittest.main()
