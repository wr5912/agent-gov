from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, Query

from app.routers.error_helpers import ensure_found, raise_conflict
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.feedback_store import FeedbackStore
from app.runtime.schemas import (
    EvalCaseResponse,
    EvalRunResponse,
    FeedbackEvalCaseGenerateResponse,
    FeedbackEvalCaseUpdateRequest,
    FeedbackEvalDatasetSyncRequest,
    FeedbackEvalRunCreateRequest,
)


def create_eval_router(
    *,
    feedback_store: FeedbackStore,
    runtime: ClaudeRuntime,
    require_api_key: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["feedback"], dependencies=[Depends(require_api_key)])

    @router.post(
        "/eval-datasets/feedback/sync",
        response_model=FeedbackEvalCaseGenerateResponse,
        summary="Sync processed feedback cases into reusable eval cases",
    )
    async def sync_feedback_eval_dataset(req: FeedbackEvalDatasetSyncRequest) -> dict[str, Any]:
        return feedback_store.sync_feedback_eval_cases(feedback_case_id=req.feedback_case_id, limit=req.limit)

    @router.get(
        "/eval-cases",
        response_model=list[EvalCaseResponse],
        summary="List feedback-derived eval cases",
    )
    async def list_eval_cases(
        status_filter: str | None = Query(default=None, alias="status"),
        source_feedback_case_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return feedback_store.list_eval_cases(status=status_filter, source_feedback_case_id=source_feedback_case_id, limit=limit)

    @router.patch(
        "/eval-cases/{eval_case_id}",
        response_model=EvalCaseResponse,
        summary="Update one feedback-derived eval case",
    )
    async def update_eval_case(eval_case_id: str, req: FeedbackEvalCaseUpdateRequest) -> dict[str, Any]:
        updated = feedback_store.update_eval_case(eval_case_id, req.model_dump(exclude_unset=True))
        return ensure_found(updated, "Eval case not found")

    @router.post(
        "/eval-runs",
        response_model=EvalRunResponse,
        summary="Run a manual feedback dataset evaluation against the current main Agent",
    )
    async def create_eval_run(req: FeedbackEvalRunCreateRequest) -> dict[str, Any]:
        if not req.eval_case_ids:
            feedback_store.sync_feedback_eval_cases(limit=500)
        result = await runtime.run_feedback_eval(
            eval_case_ids=req.eval_case_ids or None,
            optimization_task_id=req.optimization_task_id,
            source="manual_feedback_dataset",
        )
        if not result:
            raise_conflict("No active eval cases found")
        return result

    @router.get(
        "/eval-runs",
        response_model=list[EvalRunResponse],
        summary="List feedback dataset eval runs",
    )
    async def list_eval_runs(
        optimization_task_id: str | None = None,
        agent_version_id: str | None = None,
        status_filter: str | None = Query(default=None, alias="status"),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return feedback_store.list_eval_runs(
            optimization_task_id=optimization_task_id,
            agent_version_id=agent_version_id,
            status=status_filter,
            limit=limit,
        )

    @router.get(
        "/eval-runs/{eval_run_id}",
        response_model=EvalRunResponse,
        summary="Get one feedback dataset eval run",
    )
    async def get_eval_run(eval_run_id: str) -> dict[str, Any]:
        eval_run = feedback_store.get_eval_run(eval_run_id)
        return ensure_found(eval_run, "Eval run not found")

    return router
