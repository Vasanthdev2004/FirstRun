"""Authenticated M4 routes. Repository/artifact bytes never become raw responses."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool

from firstrun.domain.web import (
    BaselineView, CandidateDiffView, CaseDetail, CasePage, CaseSummary,
    DecisionRequest, DecisionResult, EventPage, ProofView, RepositoryDetail,
    RepositorySummary, StartRunRequest,
)
from firstrun.web_service import (
    ConflictError, DataUnavailableError, FirstRunWebService, NotFoundError,
    WebServiceError,
)
from firstrun.web_auth import WebAuthError


def install_web_routes(app: FastAPI, manager, service: FirstRunWebService | None) -> None:
    def authorize(request: Request, repository_id: int | None = None, *, write: bool = False):
        if manager is None or service is None:
            raise HTTPException(503, "GitHub sign-in and an approved repository are not configured")
        try:
            session = manager.require(request, require_csrf=write)
        except WebAuthError as exc:
            raise HTTPException(exc.status, "Sign-in or repository permission is required") from None
        if repository_id is not None and repository_id != session.repository_id:
            raise HTTPException(404, "Repository not found")
        return session

    def call(method, *args, **kwargs):
        try:
            return method(*args, **kwargs)
        except NotFoundError:
            raise HTTPException(404, "Repository or case not found") from None
        except ConflictError:
            raise HTTPException(409, "State changed or action is unavailable; refresh before deciding") from None
        except DataUnavailableError:
            raise HTTPException(503, "Controller data is temporarily unavailable") from None
        except (WebServiceError, ValueError):
            raise HTTPException(400, "Invalid request") from None
        except Exception:
            # Do not serialize filesystem, transport, provider or credential context.
            raise HTTPException(503, "Controller data is temporarily unavailable") from None

    async def body(request: Request, model):
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 4096:
                raise HTTPException(413, "Decision request is too large")
        try:
            return model.model_validate_json(bytes(data))
        except ValueError:
            raise HTTPException(400, "Invalid decision request") from None

    @app.get("/api/repositories", response_model=list[RepositorySummary])
    def repositories(request: Request):
        session = authorize(request)
        return call(service.list_repositories, session.repository_id)

    @app.get("/api/repositories/{repository_id}", response_model=RepositoryDetail)
    def repository(repository_id: int, request: Request):
        authorize(request, repository_id)
        return call(service.get_repository, repository_id)

    @app.get("/api/repositories/{repository_id}/cases", response_model=CasePage)
    def cases(repository_id: int, request: Request, cursor: str | None = Query(None, max_length=256),
              limit: int = Query(20, ge=1, le=100)):
        authorize(request, repository_id)
        return call(service.list_cases, repository_id, cursor=cursor, limit=limit)

    @app.get("/api/repositories/{repository_id}/cases/{case_id}", response_model=CaseDetail)
    def case(repository_id: int, case_id: str, request: Request):
        authorize(request, repository_id)
        return call(service.get_case, repository_id, case_id)

    @app.get("/api/repositories/{repository_id}/cases/{case_id}/events", response_model=EventPage)
    def events(repository_id: int, case_id: str, request: Request,
               cursor: int | None = Query(None, ge=1), limit: int = Query(50, ge=1, le=100)):
        authorize(request, repository_id)
        return call(service.list_events, repository_id, case_id, cursor=cursor, limit=limit)

    @app.get("/api/repositories/{repository_id}/cases/{case_id}/baseline", response_model=BaselineView | None)
    def baseline(repository_id: int, case_id: str, request: Request):
        authorize(request, repository_id)
        return call(service.get_baseline, repository_id, case_id)

    @app.get("/api/repositories/{repository_id}/cases/{case_id}/proof", response_model=ProofView | None)
    def proof(repository_id: int, case_id: str, request: Request, exact_commit: bool = False):
        authorize(request, repository_id)
        return call(service.get_proof, repository_id, case_id, exact_commit=exact_commit)

    @app.get("/api/repositories/{repository_id}/cases/{case_id}/diff", response_model=CandidateDiffView | None)
    def diff(repository_id: int, case_id: str, request: Request):
        authorize(request, repository_id)
        return call(service.get_candidate_diff, repository_id, case_id)

    @app.post("/api/repositories/{repository_id}/runs", response_model=CaseSummary, status_code=202)
    async def start(repository_id: int, request: Request):
        await run_in_threadpool(authorize, request, repository_id, write=True)
        decision = await body(request, StartRunRequest)
        return await run_in_threadpool(call, service.start_run, repository_id, decision)

    @app.post("/api/repositories/{repository_id}/cases/{case_id}/decision", response_model=DecisionResult)
    async def decide(repository_id: int, case_id: str, request: Request):
        await run_in_threadpool(authorize, request, repository_id, write=True)
        decision = await body(request, DecisionRequest)
        return await run_in_threadpool(call, service.decide, repository_id, case_id, decision)
