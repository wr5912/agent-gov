from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends, Query

from app.routers.feedback_batch_regression import register_batch_regression_routes
from app.routers.error_helpers import ensure_found, raise_conflict, require_request
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.json_types import JsonObject
from app.runtime.response_schemas.agent_job_response_schemas import AgentJobResponse
from app.runtime.response_schemas.feedback_plan_response_schemas import FeedbackOptimizationPlanTaskResponse
from app.runtime.stores.feedback_store import FeedbackStore
from app.runtime.response_schemas.feedback_workflow_response_schemas import (
    FeedbackOptimizationBatchAttributionResponse,
    FeedbackOptimizationBatchExecutionResponse,
    FeedbackOptimizationBatchResponse,
    FeedbackOptimizationPlanTaskExecuteResponse,
)
from app.runtime.schemas import (
    EvalCaseResponse,
    FeedbackEvalCaseUpdateRequest,
    FeedbackOptimizationBatchAttributionRequest,
    FeedbackOptimizationBatchCreateRequest,
    FeedbackOptimizationBatchEvalCaseCreateRequest,
    FeedbackOptimizationBatchPlanGenerateRequest,
    FeedbackOptimizationBatchPlanReviewRequest,
    FeedbackOptimizationPlanTaskExecuteRequest,
)
from app.services.agent_governance import AgentGovernanceService
from app.services.execution_application import ExecutionApplicationService


def _batch_plan_task(batch: JsonObject | None, plan_task_id: str) -> FeedbackOptimizationPlanTaskResponse | None:
    plan = batch.get("optimization_plan") if isinstance((batch or {}).get("optimization_plan"), dict) else None
    for item in (plan or {}).get("tasks") or []:
        if isinstance(item, dict) and str(item.get("plan_task_id") or "") == plan_task_id:
            return FeedbackOptimizationPlanTaskResponse.model_validate(item)
    return None


def create_feedback_batches_router(
    *,
    feedback_store: FeedbackStore,
    runtime: ClaudeRuntime,
    execution_application: ExecutionApplicationService,
    agent_governance: AgentGovernanceService,
    require_api_key: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["feedback"], dependencies=[Depends(require_api_key)])
    _register_batch_crud_routes(router, feedback_store)
    _register_batch_eval_case_routes(router, feedback_store)
    _register_batch_analysis_routes(router, feedback_store, runtime)
    _register_batch_plan_review_routes(router, feedback_store, runtime, execution_application)
    _register_batch_plan_task_routes(router, feedback_store, runtime, execution_application)
    register_batch_regression_routes(router, feedback_store, runtime, agent_governance)
    return router


def _register_batch_crud_routes(router: APIRouter, feedback_store: FeedbackStore) -> None:

    @router.get(
        "/feedback-optimization-batches",
        response_model=list[FeedbackOptimizationBatchResponse],
        summary="List feedback optimization batches",
    )
    async def list_feedback_optimization_batches(
        status: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[FeedbackOptimizationBatchResponse]:
        return feedback_store.list_optimization_batches(status=status, limit=limit)

    @router.post(
        "/feedback-optimization-batches",
        response_model=FeedbackOptimizationBatchResponse,
        summary="Create one optimization batch from selected feedback sources",
    )
    async def create_feedback_optimization_batch(req: FeedbackOptimizationBatchCreateRequest) -> FeedbackOptimizationBatchResponse:
        require_request(bool(req.source_refs), "source_refs is required")
        batch = feedback_store.create_optimization_batch(
            [item.model_dump(mode="json") for item in req.source_refs],
            title=req.title,
            priority=req.priority,
        )
        if not batch:
            raise_conflict("No selected feedback source can create an optimization batch")
        return batch

    @router.get(
        "/feedback-optimization-batches/{batch_id}",
        response_model=FeedbackOptimizationBatchResponse,
        summary="Get one feedback optimization batch",
    )
    async def get_feedback_optimization_batch(batch_id: str) -> FeedbackOptimizationBatchResponse:
        batch = feedback_store.find_optimization_batch(batch_id)
        return ensure_found(batch, "Feedback optimization batch not found")


def _register_batch_eval_case_routes(router: APIRouter, feedback_store: FeedbackStore) -> None:

    @router.get(
        "/feedback-optimization-batches/{batch_id}/eval-cases",
        response_model=list[EvalCaseResponse],
        summary="List regression eval cases associated with one optimization batch",
    )
    async def list_feedback_optimization_batch_eval_cases(batch_id: str) -> list[EvalCaseResponse]:
        eval_cases = feedback_store.list_batch_eval_cases(batch_id)
        if eval_cases is None:
            ensure_found(eval_cases, "Feedback optimization batch not found")
        return eval_cases

    @router.post(
        "/feedback-optimization-batches/{batch_id}/eval-cases",
        response_model=EvalCaseResponse,
        summary="Create and associate one manual regression eval case with an optimization batch",
    )
    async def create_feedback_optimization_batch_eval_case(
        batch_id: str,
        req: FeedbackOptimizationBatchEvalCaseCreateRequest,
    ) -> EvalCaseResponse:
        eval_case = feedback_store.create_batch_eval_case(batch_id, req.model_dump())
        return ensure_found(eval_case, "Feedback optimization batch not found")

    @router.patch(
        "/feedback-optimization-batches/{batch_id}/eval-cases/{eval_case_id}",
        response_model=EvalCaseResponse,
        summary="Update one regression eval case associated with an optimization batch",
    )
    async def update_feedback_optimization_batch_eval_case(
        batch_id: str,
        eval_case_id: str,
        req: FeedbackEvalCaseUpdateRequest,
    ) -> EvalCaseResponse:
        updated = feedback_store.update_batch_eval_case(batch_id, eval_case_id, req.model_dump(exclude_unset=True))
        return ensure_found(updated, "Feedback optimization batch eval case not found")

    @router.delete(
        "/feedback-optimization-batches/{batch_id}/eval-cases/{eval_case_id}",
        response_model=FeedbackOptimizationBatchResponse,
        summary="Remove one regression eval case association from an optimization batch",
    )
    async def remove_feedback_optimization_batch_eval_case(batch_id: str, eval_case_id: str) -> FeedbackOptimizationBatchResponse:
        batch = feedback_store.remove_batch_eval_case(batch_id, eval_case_id)
        return ensure_found(batch, "Feedback optimization batch eval case not found")


def _register_batch_analysis_routes(
    router: APIRouter,
    feedback_store: FeedbackStore,
    runtime: ClaudeRuntime,
) -> None:

    @router.post(
        "/feedback-optimization-batches/{batch_id}/attribution-jobs",
        response_model=FeedbackOptimizationBatchAttributionResponse,
        summary="Run attribution jobs for all feedback cases in one optimization batch",
    )
    async def run_feedback_optimization_batch_attribution(
        batch_id: str,
        req: FeedbackOptimizationBatchAttributionRequest | None = None,
    ) -> FeedbackOptimizationBatchAttributionResponse:
        batch = feedback_store.find_optimization_batch(batch_id)
        batch = ensure_found(batch, "Feedback optimization batch not found")
        request = req or FeedbackOptimizationBatchAttributionRequest()
        if request.force:
            batch = feedback_store.reset_batch_attribution(batch_id) or batch
        jobs: list[AgentJobResponse] = []
        for feedback_case_id in batch.get("feedback_case_ids") or []:
            job = runtime.queue_attribution_job(str(feedback_case_id), force=request.force)
            if job:
                jobs.append(job)
        job_payloads = [job.model_dump(mode="json") for job in jobs]
        updated = feedback_store.record_batch_attribution_jobs(batch_id, job_payloads)
        return {"batch": updated, "jobs": jobs}

    @router.post(
        "/feedback-optimization-batches/{batch_id}/optimization-plan",
        response_model=AgentJobResponse,
        summary="Queue one aggregated optimization plan from batch attribution results",
    )
    async def generate_feedback_optimization_batch_plan(
        batch_id: str,
        req: FeedbackOptimizationBatchPlanGenerateRequest | None = None,
    ) -> AgentJobResponse:
        job = runtime.queue_batch_optimization_plan(
            batch_id,
            regeneration_instruction=(req or FeedbackOptimizationBatchPlanGenerateRequest()).regeneration_instruction,
            force=True,
        )
        if not job:
            ensure_found(feedback_store.find_optimization_batch(batch_id), "Feedback optimization batch not found")
            raise_conflict("Batch cannot queue an optimization plan without actionable attributions")
        return job


def _register_batch_plan_review_routes(
    router: APIRouter,
    feedback_store: FeedbackStore,
    runtime: ClaudeRuntime,
    execution_application: ExecutionApplicationService,
) -> None:

    @router.post(
        "/feedback-optimization-batches/{batch_id}/optimization-plan/approve",
        response_model=FeedbackOptimizationBatchExecutionResponse,
        summary="Execute one batch optimization plan, generate an execution plan, and apply controlled changes",
    )
    async def approve_feedback_optimization_batch_plan(
        batch_id: str,
        req: FeedbackOptimizationBatchPlanReviewRequest,
    ) -> FeedbackOptimizationBatchExecutionResponse:
        approved = feedback_store.approve_batch_optimization_plan(batch_id, comment=req.comment)
        if not approved:
            raise_conflict("Optimization plan cannot be approved")
        task = approved["optimization_task"]
        queued_job = runtime.queue_execution_job(task["optimization_task_id"], force=True)
        if not queued_job:
            feedback_store.record_batch_execution_result(batch_id, optimization_task=task)
            raise_conflict("Execution optimizer could not be queued")
        execution_job = feedback_store.get_execution_job(queued_job.job_id)
        batch = feedback_store.record_batch_execution_result(
            batch_id,
            execution_job=execution_job,
            optimization_task=task,
        )
        return {
            "batch": batch,
            "optimization_task": task,
            "execution_job": execution_job,
            "apply_result": None,
        }

    @router.post(
        "/feedback-optimization-batches/{batch_id}/optimization-plan/reject",
        response_model=FeedbackOptimizationBatchResponse,
        summary="Reject one batch optimization plan",
    )
    async def reject_feedback_optimization_batch_plan(
        batch_id: str,
        req: FeedbackOptimizationBatchPlanReviewRequest,
    ) -> FeedbackOptimizationBatchResponse:
        batch = feedback_store.reject_batch_optimization_plan(batch_id, comment=req.comment)
        return ensure_found(batch, "Feedback optimization batch or plan not found")


def _register_batch_plan_task_routes(
    router: APIRouter,
    feedback_store: FeedbackStore,
    runtime: ClaudeRuntime,
    execution_application: ExecutionApplicationService,
) -> None:

    @router.post(
        "/feedback-optimization-batches/{batch_id}/optimization-plan/tasks/{plan_task_id}/execute",
        response_model=FeedbackOptimizationPlanTaskExecuteResponse,
        summary="Execute one task from a batch optimization plan",
    )
    async def execute_feedback_optimization_plan_task(
        batch_id: str,
        plan_task_id: str,
        req: FeedbackOptimizationPlanTaskExecuteRequest,
    ) -> FeedbackOptimizationPlanTaskExecuteResponse:
        batch = feedback_store.find_optimization_batch(batch_id)
        batch = ensure_found(batch, "Feedback optimization batch not found")
        plan_task = ensure_found(_batch_plan_task(batch, plan_task_id), "Optimization plan task not found")
        execution_kind = plan_task.execution_kind
        if execution_kind == "external_webhook":
            require_request(bool(req.webhook_alias), "webhook_alias is required for external tasks")
            result = feedback_store.notify_batch_plan_task_external(batch_id, plan_task_id, webhook_alias=req.webhook_alias)
            return ensure_found(result, "Optimization plan task not found")
        if execution_kind != "workspace_execution":
            raise_conflict("Optimization plan task requires manual review")

        prepared = feedback_store.prepare_batch_plan_task_execution(
            batch_id,
            plan_task_id,
            comment=f"执行优化批次 {batch_id} 的任务 {plan_task_id}",
        )
        if not prepared:
            ensure_found(prepared, "Optimization plan task not found")
        task = prepared["optimization_task"]
        if task.get("applied_agent_version_id"):
            batch = feedback_store.record_batch_plan_task_execution_result(batch_id, plan_task_id, optimization_task=task)
            return {"batch": batch, "optimization_task": task, "plan_task": _batch_plan_task(batch, plan_task_id), "execution_job": None, "apply_result": None}

        queued_job = runtime.queue_execution_job(task["optimization_task_id"], force=req.force)
        task = feedback_store.find_task(task["optimization_task_id"]) or task
        execution_job = feedback_store.get_execution_job(queued_job.job_id) if queued_job else None
        if not execution_job:
            batch = feedback_store.record_batch_plan_task_execution_result(
                batch_id,
                plan_task_id,
                optimization_task=task,
            )
            raise_conflict("Execution optimizer could not be queued")
        batch = feedback_store.record_batch_plan_task_execution_result(
            batch_id,
            plan_task_id,
            execution_job=execution_job,
            optimization_task=task,
        )
        return {
            "batch": batch,
            "optimization_task": task,
            "plan_task": _batch_plan_task(batch, plan_task_id),
            "execution_job": execution_job,
            "apply_result": None,
        }
