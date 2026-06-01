from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, Query

from app.routers.error_helpers import ensure_found
from app.runtime.response_schemas.agent_job_response_schemas import AgentJobResponse
from app.runtime.stores.feedback_store import FeedbackStore


def create_agent_jobs_router(
    *,
    feedback_store: FeedbackStore,
    require_api_key: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["feedback"], dependencies=[Depends(require_api_key)])

    @router.get(
        "/agent-jobs",
        response_model=list[AgentJobResponse],
        summary="List async feedback-loop Agent jobs",
    )
    async def list_agent_jobs(
        job_type: str | None = None,
        scope_kind: str | None = None,
        scope_id: str | None = None,
        status: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return feedback_store.list_agent_jobs(
            job_type=job_type,
            scope_kind=scope_kind,
            scope_id=scope_id,
            status=status,
            limit=limit,
        )

    @router.get(
        "/agent-jobs/{job_id}",
        response_model=AgentJobResponse,
        summary="Get one async feedback-loop Agent job",
    )
    async def get_agent_job(job_id: str) -> dict[str, Any]:
        return ensure_found(feedback_store.get_agent_job(job_id), "Agent job not found")

    return router
