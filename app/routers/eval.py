from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends, Query

from app.routers.error_helpers import ensure_found, raise_conflict
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.response_schemas.agent_job_response_schemas import AgentJobResponse
from app.runtime.stores.feedback_store import FeedbackStore
from app.runtime.schemas import (
    EvalCaseResponse,
    EvalRunResponse,
    FeedbackEvalCaseUpdateRequest,
    FeedbackEvalDatasetSyncRequest,
    FeedbackEvalRunCreateRequest,
    RegressionImpactAnalysisResponse,
)


def create_eval_router(
    *,
    feedback_store: FeedbackStore,
    runtime: ClaudeRuntime,
    require_api_key: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["feedback"], dependencies=[Depends(require_api_key)])
    _register_eval_dataset_routes(router, runtime)
    _register_eval_case_routes(router, feedback_store)
    _register_eval_run_routes(router, feedback_store, runtime)
    _register_eval_impact_routes(router, feedback_store, runtime)
    return router


def _register_eval_dataset_routes(router: APIRouter, runtime: ClaudeRuntime) -> None:
    @router.post(
        "/eval-datasets/feedback/sync",
        response_model=AgentJobResponse,
        summary="Queue processed feedback cases into reusable eval cases",
    )
    async def sync_feedback_eval_dataset(req: FeedbackEvalDatasetSyncRequest) -> AgentJobResponse:
        job = runtime.queue_eval_case_generation_job(feedback_case_id=req.feedback_case_id, limit=req.limit)
        return ensure_found(job, "No feedback cases found for eval case generation")


def _register_eval_case_routes(router: APIRouter, feedback_store: FeedbackStore) -> None:
    @router.get(
        "/eval-cases",
        response_model=list[EvalCaseResponse],
        summary="List feedback-derived eval cases",
    )
    async def list_eval_cases(
        status: str | None = None,
        source_feedback_case_id: str | None = None,
        asset_layer: str | None = None,
        promotion_status: str | None = None,
        blocking_policy: str | None = None,
        flaky_status: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[EvalCaseResponse]:
        return feedback_store.list_eval_cases(
            status=status,
            source_feedback_case_id=source_feedback_case_id,
            asset_layer=asset_layer,
            promotion_status=promotion_status,
            blocking_policy=blocking_policy,
            flaky_status=flaky_status,
            limit=limit,
        )

    @router.patch(
        "/eval-cases/{eval_case_id}",
        response_model=EvalCaseResponse,
        summary="Update one feedback-derived eval case",
    )
    async def update_eval_case(eval_case_id: str, req: FeedbackEvalCaseUpdateRequest) -> EvalCaseResponse:
        updated = feedback_store.update_eval_case(eval_case_id, req.model_dump(exclude_unset=True))
        return ensure_found(updated, "Eval case not found")


def _register_eval_run_routes(
    router: APIRouter,
    feedback_store: FeedbackStore,
    runtime: ClaudeRuntime,
) -> None:
    @router.post(
        "/eval-runs",
        response_model=EvalRunResponse,
        summary="Run a manual feedback dataset evaluation against the current main Agent",
    )
    async def create_eval_run(req: FeedbackEvalRunCreateRequest) -> EvalRunResponse:
        return await _run_manual_feedback_eval(feedback_store=feedback_store, runtime=runtime, req=req)

    @router.get(
        "/eval-runs",
        response_model=list[EvalRunResponse],
        summary="List feedback dataset eval runs",
    )
    async def list_eval_runs(
        optimization_task_id: str | None = None,
        agent_version_id: str | None = None,
        status: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[EvalRunResponse]:
        return feedback_store.list_eval_runs(
            optimization_task_id=optimization_task_id,
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


def _register_eval_impact_routes(router: APIRouter, feedback_store: FeedbackStore, runtime: ClaudeRuntime) -> None:
    @router.post(
        "/eval-runs/{eval_run_id}/impact-analysis",
        response_model=AgentJobResponse,
        summary="Queue regression impact analysis for one eval run",
    )
    async def create_regression_impact_analysis(eval_run_id: str) -> AgentJobResponse:
        analysis = runtime.queue_regression_impact_analysis_job(eval_run_id)
        return ensure_found(analysis, "Eval run not found")

    @router.get(
        "/eval-runs/{eval_run_id}/impact-analysis",
        response_model=RegressionImpactAnalysisResponse,
        summary="Get regression impact analysis for one eval run",
    )
    async def get_regression_impact_analysis(eval_run_id: str) -> RegressionImpactAnalysisResponse:
        analysis = feedback_store.get_regression_impact_analysis(eval_run_id)
        return ensure_found(analysis, "Regression impact analysis not found")


async def _run_manual_feedback_eval(
    *,
    feedback_store: FeedbackStore,
    runtime: ClaudeRuntime,
    req: FeedbackEvalRunCreateRequest,
) -> EvalRunResponse:
    result = await runtime.run_feedback_eval(
        eval_case_ids=req.eval_case_ids or None,
        optimization_task_id=req.optimization_task_id,
        source="manual_feedback_dataset",
    )
    if not result:
        raise_conflict("No active eval cases found")
    return EvalRunResponse.model_validate(result)
