from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends, Query

from app.routers.error_helpers import ensure_found, require_request
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.response_schemas.agent_job_response_schemas import AgentJobResponse
from app.runtime.stores.feedback_store import FeedbackStore
from app.runtime.schemas import (
    AgentRunResponse,
    FeedbackEvalCaseGenerateRequest,
    FeedbackSignalCreateRequest,
    FeedbackSignalResponse,
    FeedbackSourceResponse,
    FeedbackSourceUpdateRequest,
    PendingCorrelationResponse,
    PendingCorrelationResolveRequest,
    SocEventIngestRequest,
    SocEventIngestResponse,
    SocEventResponse,
)


def create_feedback_workbench_router(
    *,
    feedback_store: FeedbackStore,
    runtime: ClaudeRuntime,
    require_api_key: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["feedback"], dependencies=[Depends(require_api_key)])
    _register_agent_run_routes(router, feedback_store)
    _register_feedback_signal_routes(router, feedback_store)
    _register_soc_event_routes(router, feedback_store)
    _register_pending_correlation_routes(router, feedback_store)
    _register_feedback_source_routes(router, feedback_store, runtime)
    return router


def _register_agent_run_routes(router: APIRouter, feedback_store: FeedbackStore) -> None:

    @router.get(
        "/agent-runs",
        response_model=list[AgentRunResponse],
        summary="List Agent run records used by feedback evidence packages",
    )
    async def list_agent_runs(
        run_id: str | None = None,
        session_id: str | None = None,
        alert_id: str | None = None,
        case_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[AgentRunResponse]:
        return feedback_store.list_runs(run_id=run_id, session_id=session_id, alert_id=alert_id, case_id=case_id, limit=limit)


def _register_feedback_signal_routes(router: APIRouter, feedback_store: FeedbackStore) -> None:

    @router.post(
        "/feedback-signals",
        response_model=FeedbackSignalResponse,
        summary="Collect one feedback signal without attribution or proposal generation",
    )
    async def create_feedback_signal(req: FeedbackSignalCreateRequest) -> FeedbackSignalResponse:
        return feedback_store.create_signal(req)

    @router.get(
        "/feedback-signals",
        response_model=list[FeedbackSignalResponse],
        summary="List collected feedback signals",
    )
    async def list_feedback_signals(
        run_id: str | None = None,
        session_id: str | None = None,
        alert_id: str | None = None,
        case_id: str | None = None,
        source_type: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[FeedbackSignalResponse]:
        return feedback_store.list_signals(
            run_id=run_id,
            session_id=session_id,
            alert_id=alert_id,
            case_id=case_id,
            source_type=source_type,
            limit=limit,
        )

    @router.get(
        "/feedback-signals/{signal_id}",
        response_model=FeedbackSignalResponse,
        summary="Get one feedback signal",
    )
    async def get_feedback_signal(signal_id: str) -> FeedbackSignalResponse:
        signal = feedback_store.find_signal(signal_id)
        return ensure_found(signal, "Feedback signal not found")


def _register_soc_event_routes(router: APIRouter, feedback_store: FeedbackStore) -> None:

    @router.post(
        "/soc-events",
        response_model=SocEventIngestResponse,
        summary="Collect one SOC event without attribution or proposal generation",
    )
    async def ingest_soc_event(req: SocEventIngestRequest) -> SocEventIngestResponse:
        return SocEventIngestResponse(**feedback_store.ingest_soc_event(req))

    @router.get(
        "/soc-events",
        response_model=list[SocEventResponse],
        summary="List collected SOC events",
    )
    async def list_soc_events(
        run_id: str | None = None,
        session_id: str | None = None,
        alert_id: str | None = None,
        case_id: str | None = None,
        event_type: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[SocEventResponse]:
        return feedback_store.list_events(
            run_id=run_id,
            session_id=session_id,
            alert_id=alert_id,
            case_id=case_id,
            event_type=event_type,
            limit=limit,
        )

    @router.get(
        "/soc-events/{event_id}",
        response_model=SocEventResponse,
        summary="Get one SOC event",
    )
    async def get_soc_event(event_id: str) -> SocEventResponse:
        event = feedback_store.find_event(event_id)
        return ensure_found(event, "SOC event not found")


def _register_pending_correlation_routes(router: APIRouter, feedback_store: FeedbackStore) -> None:

    @router.get(
        "/pending-correlations",
        response_model=list[PendingCorrelationResponse],
        summary="List pending feedback correlations",
    )
    async def list_pending_correlations(
        status: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[PendingCorrelationResponse]:
        return feedback_store.list_pending(status=status, limit=limit)

    @router.post(
        "/pending-correlations/{pending_id}/resolve",
        response_model=PendingCorrelationResponse,
        summary="Resolve one pending feedback correlation",
    )
    async def resolve_pending_correlation(pending_id: str, req: PendingCorrelationResolveRequest) -> PendingCorrelationResponse:
        resolved = feedback_store.resolve_pending(
            pending_id,
            run_id=req.run_id,
            session_id=req.session_id,
            alert_id=req.alert_id,
            case_id=req.case_id,
            comment=req.comment,
        )
        return ensure_found(resolved, "Pending correlation not found")


def _register_feedback_source_routes(router: APIRouter, feedback_store: FeedbackStore, runtime: ClaudeRuntime) -> None:

    @router.get(
        "/feedback-sources",
        response_model=list[FeedbackSourceResponse],
        summary="List unified feedback sources for the product workflow",
    )
    async def list_feedback_sources(limit: int = Query(default=500, ge=1, le=1000)) -> list[FeedbackSourceResponse]:
        return feedback_store.list_feedback_sources(limit=limit)

    @router.get(
        "/feedback-sources/{source_kind}/{source_id}",
        response_model=FeedbackSourceResponse,
        summary="Get one unified feedback source",
    )
    async def get_feedback_source(source_kind: str, source_id: str) -> FeedbackSourceResponse:
        source = feedback_store.find_feedback_source(source_kind, source_id)
        return ensure_found(source, "Feedback source not found")

    @router.patch(
        "/feedback-sources/{source_kind}/{source_id}",
        response_model=FeedbackSourceResponse,
        summary="Update developer annotations for one feedback source",
    )
    async def update_feedback_source(
        source_kind: str,
        source_id: str,
        req: FeedbackSourceUpdateRequest,
    ) -> FeedbackSourceResponse:
        source = feedback_store.update_feedback_source_annotation(source_kind, source_id, req.model_dump(exclude_unset=True))
        return ensure_found(source, "Feedback source not found")

    @router.post(
        "/feedback-sources/eval-cases/generate",
        response_model=AgentJobResponse,
        summary="Queue regression eval case generation for selected feedback sources",
    )
    async def generate_feedback_source_eval_cases(req: FeedbackEvalCaseGenerateRequest) -> AgentJobResponse:
        require_request(bool(req.source_refs), "source_refs is required")
        job = runtime.queue_eval_case_generation_job(
            source_refs=[item.model_dump(mode="json") for item in req.source_refs],
            force=req.force,
        )
        return ensure_found(job, "No feedback sources found for eval case generation")
