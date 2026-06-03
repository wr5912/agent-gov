from __future__ import annotations

from fastapi import APIRouter, Query

from app.routers.error_helpers import ensure_found, raise_conflict
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.response_schemas.agent_job_response_schemas import AgentJobResponse
from app.runtime.response_schemas.feedback_workflow_response_schemas import FeedbackOptimizationBatchRegressionResponse
from app.runtime.schemas import (
    EvalRunResponse,
    FeedbackOptimizationBatchRegressionRunRequest,
    RegressionGateOverrideRequest,
    RegressionGateOverrideResponse,
    RegressionPlanCreateRequest,
    RegressionPlanResponse,
)
from app.runtime.stores.feedback_store import FeedbackStore
from app.services.agent_governance import AgentGovernanceService


def register_batch_regression_routes(
    router: APIRouter,
    feedback_store: FeedbackStore,
    runtime: ClaudeRuntime,
    agent_governance: AgentGovernanceService,
) -> None:
    _register_batch_regression_plan_routes(router, feedback_store)
    _register_batch_regression_run_routes(router, feedback_store, runtime, agent_governance)
    _register_batch_regression_gate_routes(router, feedback_store, runtime)


def _register_batch_regression_plan_routes(router: APIRouter, feedback_store: FeedbackStore) -> None:
    @router.post(
        "/feedback-optimization-batches/{batch_id}/regression-plan",
        response_model=RegressionPlanResponse,
        summary="Create or reuse a regression plan for one optimization batch",
    )
    async def create_feedback_optimization_batch_regression_plan(
        batch_id: str,
        req: RegressionPlanCreateRequest | None = None,
    ) -> RegressionPlanResponse:
        plan = feedback_store.create_regression_plan(batch_id, force=bool((req or RegressionPlanCreateRequest()).force))
        return ensure_found(plan, "Feedback optimization batch not found")

    @router.get(
        "/feedback-optimization-batches/{batch_id}/regression-plan",
        response_model=RegressionPlanResponse,
        summary="Get latest regression plan for one optimization batch",
    )
    async def get_feedback_optimization_batch_regression_plan(batch_id: str) -> RegressionPlanResponse:
        ensure_found(feedback_store.find_optimization_batch(batch_id), "Feedback optimization batch not found")
        plan = feedback_store.get_latest_regression_plan(batch_id)
        return ensure_found(plan, "Regression plan not found")


def _register_batch_regression_run_routes(
    router: APIRouter,
    feedback_store: FeedbackStore,
    runtime: ClaudeRuntime,
    agent_governance: AgentGovernanceService,
) -> None:
    @router.post(
        "/feedback-optimization-batches/{batch_id}/regression-runs",
        response_model=FeedbackOptimizationBatchRegressionResponse,
        summary="Run regression validation for one optimization batch from an explicit regression plan",
    )
    async def run_feedback_optimization_batch_regression(
        batch_id: str,
        req: FeedbackOptimizationBatchRegressionRunRequest,
    ) -> FeedbackOptimizationBatchRegressionResponse:
        batch = feedback_store.find_optimization_batch(batch_id)
        batch = ensure_found(batch, "Feedback optimization batch not found")
        task_id = str(batch.get("optimization_task_id") or "")
        task = feedback_store.find_task(task_id)
        change_set = (task or {}).get("latest_change_set") if isinstance((task or {}).get("latest_change_set"), dict) else None
        if not task or not change_set or not change_set.get("candidate_commit_sha"):
            raise_conflict("Batch optimization must have a candidate Agent change set before regression validation")
        plan = feedback_store.get_regression_plan(req.regression_plan_id)
        if not plan or plan.get("batch_id") != batch_id:
            raise_conflict("Regression plan does not belong to this batch")
        eval_case_ids = [str(item) for item in plan.get("eval_case_ids") or [] if item]
        if not eval_case_ids:
            raise_conflict("No approved active eval cases found for this regression plan")
        agent_governance.mark_regression_running(str(change_set["change_set_id"]), eval_run_id="pending")
        result = await runtime.run_feedback_eval(
            eval_case_ids=eval_case_ids,
            optimization_task_id=task_id,
            source="optimization_batch_regression",
            regression_plan_id=req.regression_plan_id,
            change_set_id=str(change_set["change_set_id"]),
            candidate_commit_sha=str(change_set["candidate_commit_sha"]),
            candidate_worktree_path=str(change_set["worktree_path"]),
        )
        if not result:
            raise_conflict("Regression run could not be started")
        eval_run = EvalRunResponse.model_validate(result)
        agent_governance.complete_regression(str(change_set["change_set_id"]), eval_run=eval_run.model_dump(mode="json"))
        impact_job = runtime.queue_regression_impact_analysis_job(eval_run.eval_run_id)
        impact = feedback_store.get_regression_impact_analysis(eval_run.eval_run_id)
        batch = feedback_store.record_batch_regression_result(batch_id, eval_run.model_dump(mode="json"))
        return {"batch": batch, "eval_run": eval_run, "regression_plan": plan, "impact_analysis": impact, "impact_analysis_job": impact_job}


def _register_batch_regression_gate_routes(router: APIRouter, feedback_store: FeedbackStore, runtime: ClaudeRuntime) -> None:
    @router.post(
        "/feedback-optimization-batches/{batch_id}/regression-runs/{eval_run_id}/impact-analysis",
        response_model=AgentJobResponse,
        summary="Queue impact analysis for a batch regression run",
    )
    async def create_batch_regression_impact_analysis(
        batch_id: str,
        eval_run_id: str,
        force: bool = Query(default=False),
    ) -> AgentJobResponse:
        ensure_found(feedback_store.find_optimization_batch(batch_id), "Feedback optimization batch not found")
        analysis = runtime.queue_regression_impact_analysis_job(eval_run_id, force=force)
        return ensure_found(analysis, "Eval run not found")

    @router.post(
        "/feedback-optimization-batches/{batch_id}/regression-runs/{eval_run_id}/gate-overrides",
        response_model=RegressionGateOverrideResponse,
        summary="Record a break-glass override for a regression gate result",
    )
    async def create_batch_regression_gate_override(
        batch_id: str,
        eval_run_id: str,
        req: RegressionGateOverrideRequest,
    ) -> RegressionGateOverrideResponse:
        ensure_found(feedback_store.find_optimization_batch(batch_id), "Feedback optimization batch not found")
        override = feedback_store.record_regression_gate_override(batch_id, eval_run_id, req.model_dump())
        return ensure_found(override, "Eval run not found")
