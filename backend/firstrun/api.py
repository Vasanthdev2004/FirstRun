"""M3 webhook ingress only. Long-running execution is a separate local worker.

No public case/artifact or mutation endpoints precede the M4 identity boundary.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request

from firstrun.github_state import GitHubConfig, SQLiteStore
from firstrun.webhooks import ingest_signed_push


MAX_WEBHOOK_BYTES = 512 * 1024


def load_github_config(path: Path) -> GitHubConfig:
    """Read configuration, not the referenced key or webhook-secret contents."""
    if path.stat().st_size > 32 * 1024:
        raise ValueError("GitHub configuration exceeds the 32KiB limit")
    return GitHubConfig.model_validate_json(path.read_bytes())


def create_app(config: GitHubConfig, database: Path) -> FastAPI:
    app = FastAPI(title="FirstRun webhook ingress", docs_url=None, redoc_url=None, openapi_url=None)
    store = SQLiteStore(database)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ready", "surface": "webhook ingress"}

    @app.post("/github/webhook", status_code=202)
    async def webhook(request: Request) -> dict[str, object]:
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
