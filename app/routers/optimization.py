from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, Query

from app.routers.error_helpers import ensure_found, raise_conflict, require_request
from app.runtime.agent_version_store import AgentVersionStore
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.feedback_store import FeedbackStore
from app.runtime.feedback_workflow_response_schemas import (
    ExecutionCompensationResponse,
    ExternalGovernanceItemResponse,
    ExternalGovernanceWebhookResponse,
    OptimizationExecutionApplyResponse,
    OptimizationExecutionJobResponse,
    OptimizationTaskResponse,
)
from app.runtime.optimization_response_schemas import (
    OptimizationProposalResponse,
    OptimizationProposalReviewResponse,
)
from app.runtime.schemas import (
    EvalRunResponse,
    ExternalGovernanceNotifyRequest,
    FeedbackEvalRunCreateRequest,
    OptimizationExecutionApplyRequest,
    OptimizationExecutionCreateRequest,
    OptimizationProposalReviewRequest,
    OptimizationTaskCreateRequest,
    OptimizationTaskMarkAppliedRequest,
)
from app.services.execution_application import ExecutionApplicationService


def create_optimization_router(
    *,
    feedback_store: FeedbackStore,
    runtime: ClaudeRuntime,
    agent_version_store: AgentVersionStore,
    execution_application: ExecutionApplicationService,
    require_api_key: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["feedback"], dependencies=[Depends(require_api_key)])

    @router.get(
        "/optimization-proposals",
        response_model=list[OptimizationProposalResponse],
        summary="List pending feedback-driven optimization proposals",
    )
    async def list_optimization_proposals(
        feedback_case_id: str | None = None,
        status: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return feedback_store.list_proposals(feedback_case_id=feedback_case_id, status=status, limit=limit)

    @router.get(
        "/optimization-proposals/{proposal_id}",
        response_model=OptimizationProposalResponse,
        summary="Get one feedback-driven optimization proposal",
    )
    async def get_optimization_proposal(proposal_id: str) -> dict[str, Any]:
        proposal = feedback_store.find_proposal(proposal_id)
        return ensure_found(proposal, "Proposal not found")

    @router.post(
        "/optimization-proposals/{proposal_id}/approve",
        response_model=OptimizationProposalReviewResponse,
        summary="Approve one feedback-driven optimization proposal",
    )
    async def approve_optimization_proposal(
        proposal_id: str,
        req: OptimizationProposalReviewRequest,
    ) -> OptimizationProposalReviewResponse:
        result = feedback_store.review_proposal(proposal_id, action="approve", comment=req.comment)
        return OptimizationProposalReviewResponse(**ensure_found(result, "Proposal not found"))

    @router.post(
        "/optimization-proposals/{proposal_id}/reject",
        response_model=OptimizationProposalReviewResponse,
        summary="Reject one feedback-driven optimization proposal",
    )
    async def reject_optimization_proposal(
        proposal_id: str,
        req: OptimizationProposalReviewRequest,
    ) -> OptimizationProposalReviewResponse:
        result = feedback_store.review_proposal(proposal_id, action="reject", comment=req.comment)
        return OptimizationProposalReviewResponse(**ensure_found(result, "Proposal not found"))

    @router.post(
        "/optimization-proposals/{proposal_id}/request-more-analysis",
        response_model=OptimizationProposalReviewResponse,
        summary="Request more analysis for one feedback-driven optimization proposal",
    )
    async def request_more_analysis_for_proposal(
        proposal_id: str,
        req: OptimizationProposalReviewRequest,
    ) -> OptimizationProposalReviewResponse:
        result = feedback_store.review_proposal(proposal_id, action="request_more_analysis", comment=req.comment)
        return OptimizationProposalReviewResponse(**ensure_found(result, "Proposal not found"))

    @router.get(
        "/optimization-tasks",
        response_model=list[OptimizationTaskResponse],
        summary="List feedback-driven optimization tasks",
    )
    async def list_optimization_tasks(
        feedback_case_id: str | None = None,
        status: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return feedback_store.list_tasks(feedback_case_id=feedback_case_id, status=status, limit=limit)

    @router.get(
        "/optimization-tasks/{task_id}",
        response_model=OptimizationTaskResponse,
        summary="Get one feedback-driven optimization task",
    )
    async def get_optimization_task(task_id: str) -> dict[str, Any]:
        task = feedback_store.find_task(task_id)
        return ensure_found(task, "Optimization task not found")

    @router.get(
        "/external-governance-webhooks",
        response_model=list[ExternalGovernanceWebhookResponse],
        summary="List configured external governance webhook aliases",
    )
    async def list_external_governance_webhooks() -> list[dict[str, Any]]:
        return feedback_store.list_external_webhooks()

    @router.get(
        "/external-governance-items",
        response_model=list[ExternalGovernanceItemResponse],
        summary="List external governance items derived from external guidance",
    )
    async def list_external_governance_items(
        feedback_case_id: str | None = None,
        proposal_job_id: str | None = None,
        status: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return feedback_store.list_external_governance_items(
            feedback_case_id=feedback_case_id,
            proposal_job_id=proposal_job_id,
            status=status,
            limit=limit,
        )

    @router.post(
        "/external-governance-items/{external_item_id}/notify",
        response_model=ExternalGovernanceItemResponse,
        summary="Notify one configured external system about an external governance item",
    )
    async def notify_external_governance_item(
        external_item_id: str,
        req: ExternalGovernanceNotifyRequest,
    ) -> dict[str, Any]:
        result = feedback_store.notify_external_governance_item(external_item_id, webhook_alias=req.webhook_alias)
        return ensure_found(result, "External governance item not found")

    @router.post(
        "/optimization-tasks/{task_id}/execution-jobs",
        response_model=OptimizationExecutionJobResponse,
        summary="Generate one controlled execution plan for an optimization task",
    )
    async def create_optimization_execution_job(task_id: str, req: OptimizationExecutionCreateRequest) -> dict[str, Any]:
        job = await runtime.run_execution_job(task_id, force=req.force)
        if not job:
            raise_conflict("Optimization task cannot generate an execution plan")
        return job

    @router.get(
        "/optimization-tasks/{task_id}/execution-jobs",
        response_model=list[OptimizationExecutionJobResponse],
        summary="List controlled execution plans for one optimization task",
    )
    async def list_optimization_execution_jobs(task_id: str, limit: int = Query(default=100, ge=1, le=500)) -> list[dict[str, Any]]:
        if not feedback_store.find_task(task_id):
            ensure_found(None, "Optimization task not found")
        return feedback_store.list_execution_jobs(task_id, limit=limit)

    @router.get(
        "/execution-compensations",
        response_model=list[ExecutionCompensationResponse],
        summary="List execution application compensation records",
    )
    async def list_execution_compensations(
        status: str | None = None,
        optimization_task_id: str | None = None,
        execution_job_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return feedback_store.list_execution_compensations(
            status=status,
            optimization_task_id=optimization_task_id,
            execution_job_id=execution_job_id,
            limit=limit,
        )

    @router.get(
        "/execution-compensations/{compensation_id}",
        response_model=ExecutionCompensationResponse,
        summary="Get one execution application compensation record",
    )
    async def get_execution_compensation(compensation_id: str) -> dict[str, Any]:
        compensation = feedback_store.find_execution_compensation(compensation_id)
        return ensure_found(compensation, "Execution compensation not found")

    @router.post(
        "/optimization-tasks/{task_id}/execution-jobs/{execution_job_id}/apply",
        response_model=OptimizationExecutionApplyResponse,
        summary="Apply one reviewed controlled execution plan",
    )
    async def apply_optimization_execution_job(
        task_id: str,
        execution_job_id: str,
        req: OptimizationExecutionApplyRequest,
    ) -> dict[str, Any]:
        require_request(req.confirm, "confirm must be true")
        return execution_application.apply_ready_execution_job(task_id, execution_job_id, note=req.note)

    @router.post(
        "/execution-compensations/{compensation_id}/restore",
        response_model=ExecutionCompensationResponse,
        summary="Restore workspace files for one pending execution compensation",
    )
    async def restore_execution_compensation(compensation_id: str) -> dict[str, Any]:
        return execution_application.restore_execution_compensation(compensation_id)

    @router.post(
        "/optimization-tasks/{task_id}/mark-applied",
        response_model=OptimizationTaskResponse,
        summary="Mark one optimization task as manually applied and snapshot the main Agent version",
    )
    async def mark_optimization_task_applied(
        task_id: str,
        req: OptimizationTaskMarkAppliedRequest,
    ) -> dict[str, Any]:
        task = feedback_store.find_task(task_id)
        task = ensure_found(task, "Optimization task not found")
        if task.get("applied_agent_version_id"):
            return task
        if task.get("status") not in {"pending_execution", "failed", "needs_human_review"}:
            raise_conflict("Task cannot be marked applied from current status")
        version = agent_version_store.create_snapshot(
            reason="proposal_applied",
            source_proposal_ids=[str(item) for item in task.get("proposal_ids") or [] if item],
            note=req.note or f"优化任务 {task_id} 已人工应用，创建主智能体版本快照。",
        )
        updated = feedback_store.mark_task_applied(task_id, agent_version=version, note=req.note)
        return ensure_found(updated, "Optimization task not found")

    @router.post(
        "/optimization-tasks/{task_id}/regression-runs",
        response_model=EvalRunResponse,
        summary="Run manual regression validation for one optimization task",
    )
    async def create_optimization_task_regression_run(
        task_id: str,
        req: FeedbackEvalRunCreateRequest,
    ) -> dict[str, Any]:
        task = feedback_store.find_task(task_id)
        task = ensure_found(task, "Optimization task not found")
        if not task.get("applied_agent_version_id"):
            raise_conflict("Task must be marked applied before regression validation")
        if task.get("feedback_case_id"):
            feedback_store.sync_feedback_eval_cases(feedback_case_id=str(task["feedback_case_id"]))
        eval_case_ids = list(req.eval_case_ids or [])
        if not eval_case_ids and task.get("feedback_case_id"):
            eval_case_ids = [
                item["eval_case_id"]
                for item in feedback_store.list_eval_cases(
                    status="active",
                    source_feedback_case_id=str(task["feedback_case_id"]),
                    limit=100,
                )
            ]
        if not eval_case_ids:
            raise_conflict("No active eval cases found for this task")
        result = await runtime.run_feedback_eval(
            eval_case_ids=eval_case_ids,
            optimization_task_id=task_id,
            source="manual_task_regression",
        )
        if not result:
            raise_conflict("Regression run could not be started")
        return result

    @router.get(
        "/optimization-tasks/{task_id}/regression-runs",
        response_model=list[EvalRunResponse],
        summary="List regression validation runs for one optimization task",
    )
    async def list_optimization_task_regression_runs(
        task_id: str,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return feedback_store.list_eval_runs(optimization_task_id=task_id, limit=limit)

    @router.post(
        "/optimization-proposals/{proposal_id}/tasks",
        response_model=OptimizationTaskResponse,
        summary="Create one feedback-driven optimization task",
    )
    async def create_optimization_task(proposal_id: str, req: OptimizationTaskCreateRequest) -> dict[str, Any]:
        require_request(not req.proposal_id or req.proposal_id == proposal_id, "proposal_id path/body mismatch")
        task = feedback_store.create_task(
            proposal_id=proposal_id,
            execution_mode=req.execution_mode,
            comment=req.comment,
        )
        if not task:
            raise_conflict("Proposal is missing, not approved, or not actionable")
        return task

    return router
