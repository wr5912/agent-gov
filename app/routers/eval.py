from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Query

from app.routers.error_helpers import ensure_found, raise_conflict
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.schemas import (
    EvalRunResponse,
    FeedbackEvalRunCreateRequest,
)
from app.runtime.stores.feedback_store import FeedbackStore


def create_eval_router(
    *,
    feedback_store: FeedbackStore,
    runtime: ClaudeRuntime,
    require_api_key: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["feedback"], dependencies=[Depends(require_api_key)])
    _register_eval_run_routes(router, feedback_store, runtime)
    return router


def _register_eval_run_routes(
    router: APIRouter,
    feedback_store: FeedbackStore,
    runtime: ClaudeRuntime,
) -> None:
    @router.post(
        "/eval-runs",
        response_model=EvalRunResponse,
        summary="Run a manual TestDataset evaluation against its owning business Agent",
    )
    async def create_eval_run(req: FeedbackEvalRunCreateRequest) -> EvalRunResponse:
        return await _run_manual_feedback_eval(feedback_store=feedback_store, runtime=runtime, req=req)

    @router.get(
        "/eval-runs",
        response_model=list[EvalRunResponse],
        summary="List feedback dataset eval runs",
    )
    async def list_eval_runs(
        agent_version_id: str | None = None,
        status: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[EvalRunResponse]:
        return feedback_store.list_eval_runs(
            agent_version_id=agent_version_id,
            status=status,
            limit=limit,
        )

    @router.get(
        "/eval-runs/{eval_run_id}",
        response_model=EvalRunResponse,
        summary="Get one feedback dataset eval run",
    )
    async def get_eval_run(eval_run_id: str) -> EvalRunResponse:
        eval_run = feedback_store.get_eval_run(eval_run_id)
        return ensure_found(eval_run, "Eval run not found")


async def _run_manual_feedback_eval(
    *,
    feedback_store: FeedbackStore,
    runtime: ClaudeRuntime,
    req: FeedbackEvalRunCreateRequest,
) -> EvalRunResponse:
    result = await runtime.run_feedback_eval(
        dataset_id=req.dataset_id,
        source="manual_feedback_dataset",
    )
    if not result:
        raise_conflict("Eval runtime is unavailable")
    return EvalRunResponse.model_validate(result)
