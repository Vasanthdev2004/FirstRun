"""Webhook ingress and authenticated web API; execution stays in the worker."""

from __future__ import annotations

from pathlib import Path
from functools import lru_cache

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from firstrun.github_state import GitHubConfig, SQLiteStore
from firstrun.webhooks import ingest_signed_push


MAX_WEBHOOK_BYTES = 512 * 1024


def load_github_config(path: Path) -> GitHubConfig:
    """Read configuration, not the referenced key or webhook-secret contents."""
    if path.stat().st_size > 32 * 1024:
        raise ValueError("GitHub configuration exceeds the 32KiB limit")
    return GitHubConfig.model_validate_json(path.read_bytes())


def create_app(config: GitHubConfig | None, database: Path, *, auth_config=None,
               auth_manager=None, web_service=None) -> FastAPI:
    """Unconfigured mode is a real setup screen, never a development auth bypass.

    Optional injected dependencies are for isolated tests; there is no HTTP or
    environment switch which can fabricate an authenticated identity.
    """
    app = FastAPI(title="FirstRun", docs_url=None, redoc_url=None, openapi_url=None)
    store = SQLiteStore(database)

    from firstrun.web_auth import WebAuthManager, install_auth_routes
    from firstrun.web_api import install_web_routes

    if auth_config is not None:
        if config is None:
            raise ValueError("Web sign-in requires an approved GitHub registration")
        from firstrun.orchestration.github import make_client
        from firstrun.integrations.github import fetch_snapshot
        from firstrun.web_service import FirstRunWebService
        from firstrun.artifacts import ArtifactStore
        client = make_client(config)
        auth_manager = WebAuthManager(auth_config, database, config.registration, client)

        @lru_cache(maxsize=8)
        def source_loader(sha: str):
            return fetch_snapshot(client, sha, config.registration.source_prefix)

        web_service = FirstRunWebService(
            config, store, source_loader,
            artifact_store=ArtifactStore(database.absolute().parent / "artifacts"),
            head_loader=lambda: client.head(config.registration.branch),
        )
    install_auth_routes(app, auth_manager)
    install_web_routes(app, auth_manager, web_service)

    @app.middleware("http")
    async def private_responses(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # FastAPI's default response echoes invalid input. No submitted value
        # (including an accidentally pasted credential) belongs in error JSON.
        return JSONResponse(status_code=400, content={"detail": "Invalid request"})

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ready", "surface": "controller API"}

    @app.post("/github/webhook", status_code=202)
    async def webhook(request: Request) -> dict[str, object]:
        if config is None:
            raise HTTPException(503, "GitHub webhook is not configured")
        # Reject duplicate security headers before normalizing them into a dict.
        for name in ("x-hub-signature-256", "x-github-event", "x-github-delivery"):
            if len(request.headers.getlist(name)) != 1:
                raise HTTPException(status_code=400, detail="Missing or duplicate webhook header")
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_WEBHOOK_BYTES:
                raise HTTPException(status_code=413, detail="Webhook too large")
        try:
            return ingest_signed_push(bytes(data), dict(request.headers), config, store)
        except ValueError:
            # Neither body nor private exception context belongs in an HTTP response.
            raise HTTPException(status_code=403, detail="Webhook rejected") from None

    return app
