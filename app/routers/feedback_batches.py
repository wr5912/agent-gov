from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, Query

from app.routers.error_helpers import ensure_found, raise_conflict, require_request
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.feedback_store import FeedbackStore
from app.runtime.feedback_workflow_response_schemas import (
    FeedbackOptimizationBatchAttributionResponse,
    FeedbackOptimizationBatchExecutionResponse,
    FeedbackOptimizationBatchRegressionResponse,
    FeedbackOptimizationBatchResponse,
    FeedbackOptimizationPlanTaskExecuteResponse,
)
from app.runtime.schemas import (
    FeedbackOptimizationBatchAttributionRequest,
    FeedbackOptimizationBatchCreateRequest,
    FeedbackOptimizationBatchPlanGenerateRequest,
    FeedbackOptimizationBatchPlanReviewRequest,
    FeedbackOptimizationPlanTaskExecuteRequest,
)
from app.services.execution_application import ExecutionApplicationService


def create_feedback_batches_router(
    *,
    feedback_store: FeedbackStore,
    runtime: ClaudeRuntime,
    execution_application: ExecutionApplicationService,
    require_api_key: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["feedback"], dependencies=[Depends(require_api_key)])

    def batch_plan_task(batch: dict[str, Any] | None, plan_task_id: str) -> dict[str, Any] | None:
        plan = batch.get("optimization_plan") if isinstance((batch or {}).get("optimization_plan"), dict) else None
        for item in (plan or {}).get("tasks") or []:
            if isinstance(item, dict) and str(item.get("plan_task_id") or "") == plan_task_id:
                return item
        return None

    @router.get(
        "/feedback-optimization-batches",
        response_model=list[FeedbackOptimizationBatchResponse],
        summary="List feedback optimization batches",
    )
    async def list_feedback_optimization_batches(
        status_filter: str | None = Query(default=None, alias="status"),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return feedback_store.list_optimization_batches(status=status_filter, limit=limit)

    @router.post(
        "/feedback-optimization-batches",
        response_model=FeedbackOptimizationBatchResponse,
        summary="Create one optimization batch from selected feedback sources",
    )
    async def create_feedback_optimization_batch(req: FeedbackOptimizationBatchCreateRequest) -> dict[str, Any]:
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
    async def get_feedback_optimization_batch(batch_id: str) -> dict[str, Any]:
        batch = feedback_store.find_optimization_batch(batch_id)
        return ensure_found(batch, "Feedback optimization batch not found")

    @router.post(
        "/feedback-optimization-batches/{batch_id}/attribution-jobs",
        response_model=FeedbackOptimizationBatchAttributionResponse,
        summary="Run attribution jobs for all feedback cases in one optimization batch",
    )
    async def run_feedback_optimization_batch_attribution(
        batch_id: str,
        req: FeedbackOptimizationBatchAttributionRequest | None = None,
    ) -> dict[str, Any]:
        batch = feedback_store.find_optimization_batch(batch_id)
        batch = ensure_found(batch, "Feedback optimization batch not found")
        request = req or FeedbackOptimizationBatchAttributionRequest()
        if request.force:
            batch = feedback_store.reset_batch_attribution(batch_id) or batch
        jobs: list[dict[str, Any]] = []
        for feedback_case_id in batch.get("feedback_case_ids") or []:
            job = await runtime.run_attribution_job(str(feedback_case_id), force=request.force)
            if job:
                jobs.append(job)
        updated = feedback_store.record_batch_attribution_jobs(batch_id, jobs)
        return {"batch": updated, "jobs": jobs}

    @router.post(
        "/feedback-optimization-batches/{batch_id}/optimization-plan",
        response_model=FeedbackOptimizationBatchResponse,
        summary="Generate one aggregated optimization plan from batch attribution results",
    )
    async def generate_feedback_optimization_batch_plan(
        batch_id: str,
        req: FeedbackOptimizationBatchPlanGenerateRequest | None = None,
    ) -> dict[str, Any]:
        batch = await runtime.run_batch_optimization_plan(
            batch_id,
            regeneration_instruction=(req or FeedbackOptimizationBatchPlanGenerateRequest()).regeneration_instruction,
            force=True,
        )
        if not batch:
            ensure_found(batch, "Feedback optimization batch not found")
        return batch

    @router.post(
        "/feedback-optimization-batches/{batch_id}/optimization-plan/approve",
        response_model=FeedbackOptimizationBatchExecutionResponse,
        summary="Execute one batch optimization plan, generate an execution plan, and apply controlled changes",
    )
    async def approve_feedback_optimization_batch_plan(
        batch_id: str,
        req: FeedbackOptimizationBatchPlanReviewRequest,
    ) -> dict[str, Any]:
        approved = feedback_store.approve_batch_optimization_plan(batch_id, comment=req.comment)
        if not approved:
            raise_conflict("Optimization plan cannot be approved")
        task = approved["optimization_task"]
        execution_job = await runtime.run_execution_job(task["optimization_task_id"], force=True)
        if not execution_job:
            feedback_store.record_batch_execution_result(batch_id, optimization_task=feedback_store.find_task(task["optimization_task_id"]))
            raise_conflict("Execution optimizer could not generate a plan")
        apply_result = None
        if execution_job.get("status") == "ready":
            apply_result = execution_application.apply_ready_execution_job(
                task["optimization_task_id"],
                execution_job["execution_job_id"],
                note=f"执行优化批次 {batch_id} 时由 execution-optimizer 自动应用。",
            )
        batch = feedback_store.record_batch_execution_result(
            batch_id,
            execution_job=execution_job,
            optimization_task=feedback_store.find_task(task["optimization_task_id"]),
            applied=apply_result,
        )
        return {
            "batch": batch,
            "optimization_task": feedback_store.find_task(task["optimization_task_id"]),
            "execution_job": execution_job,
            "apply_result": apply_result,
        }

    @router.post(
        "/feedback-optimization-batches/{batch_id}/optimization-plan/reject",
        response_model=FeedbackOptimizationBatchResponse,
        summary="Reject one batch optimization plan",
    )
    async def reject_feedback_optimization_batch_plan(
        batch_id: str,
        req: FeedbackOptimizationBatchPlanReviewRequest,
    ) -> dict[str, Any]:
        batch = feedback_store.reject_batch_optimization_plan(batch_id, comment=req.comment)
        return ensure_found(batch, "Feedback optimization batch or plan not found")

    @router.post(
        "/feedback-optimization-batches/{batch_id}/optimization-plan/tasks/{plan_task_id}/execute",
        response_model=FeedbackOptimizationPlanTaskExecuteResponse,
        summary="Execute one task from a batch optimization plan",
    )
    async def execute_feedback_optimization_plan_task(
        batch_id: str,
        plan_task_id: str,
        req: FeedbackOptimizationPlanTaskExecuteRequest,
    ) -> dict[str, Any]:
        batch = feedback_store.find_optimization_batch(batch_id)
        batch = ensure_found(batch, "Feedback optimization batch not found")
        plan = batch.get("optimization_plan") if isinstance(batch.get("optimization_plan"), dict) else None
        plan_task = next(
            (
                item
                for item in (plan or {}).get("tasks") or []
                if isinstance(item, dict) and str(item.get("plan_task_id") or "") == plan_task_id
            ),
            None,
        )
        if not plan_task:
            ensure_found(plan_task, "Optimization plan task not found")
        execution_kind = str(plan_task.get("execution_kind") or "")
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
        apply_result = None
        execution_job = None
        if task.get("applied_agent_version_id"):
            batch = feedback_store.record_batch_plan_task_execution_result(batch_id, plan_task_id, optimization_task=task)
            return {"batch": batch, "optimization_task": task, "plan_task": batch_plan_task(batch, plan_task_id), "execution_job": None, "apply_result": None}

        execution_job = await runtime.run_execution_job(task["optimization_task_id"], force=req.force)
        if not execution_job:
            batch = feedback_store.record_batch_plan_task_execution_result(
                batch_id,
                plan_task_id,
                optimization_task=feedback_store.find_task(task["optimization_task_id"]),
            )
            raise_conflict("Execution optimizer could not generate a plan")
        if execution_job.get("status") == "ready":
            apply_result = execution_application.apply_ready_execution_job(
                task["optimization_task_id"],
                execution_job["execution_job_id"],
                note=f"执行优化批次 {batch_id} 的任务 {plan_task_id} 时由 execution-optimizer 自动应用。",
            )
        batch = feedback_store.record_batch_plan_task_execution_result(
            batch_id,
            plan_task_id,
            execution_job=execution_job,
            optimization_task=feedback_store.find_task(task["optimization_task_id"]),
            applied=apply_result,
        )
        return {
            "batch": batch,
            "optimization_task": feedback_store.find_task(task["optimization_task_id"]),
            "plan_task": batch_plan_task(batch, plan_task_id),
            "execution_job": execution_job,
            "apply_result": apply_result,
        }

    @router.post(
        "/feedback-optimization-batches/{batch_id}/regression-runs",
        response_model=FeedbackOptimizationBatchRegressionResponse,
        summary="Run regression validation for all active eval cases in one optimization batch",
    )
    async def run_feedback_optimization_batch_regression(batch_id: str) -> dict[str, Any]:
        batch = feedback_store.find_optimization_batch(batch_id)
        batch = ensure_found(batch, "Feedback optimization batch not found")
        task_id = str(batch.get("optimization_task_id") or "")
        task = feedback_store.find_task(task_id)
        if not task or not task.get("applied_agent_version_id"):
            raise_conflict("Batch optimization must be applied before regression validation")
        eval_case_ids = [str(item) for item in batch.get("eval_case_ids") or [] if item]
        if not eval_case_ids:
            raise_conflict("No eval cases found for this batch")
        result = await runtime.run_feedback_eval(
            eval_case_ids=eval_case_ids,
            optimization_task_id=task_id,
            source="optimization_batch_regression",
        )
        if not result:
            raise_conflict("Regression run could not be started")
        batch = feedback_store.record_batch_regression_result(batch_id, result)
        return {"batch": batch, "eval_run": result}

    return router
