from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, Query

from app.routers.error_helpers import ensure_found, raise_conflict, require_request
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.response_schemas.agent_job_response_schemas import AgentJobResponse
from app.runtime.stores.feedback_store import FeedbackStore
from app.runtime.response_schemas.feedback_workflow_response_schemas import (
    ExecutionCompensationResponse,
    ExternalGovernanceItemResponse,
    ExternalGovernanceWebhookResponse,
    OptimizationExecutionApplyResponse,
    OptimizationTaskResponse,
)
from app.runtime.response_schemas.optimization_response_schemas import (
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
    execution_application: ExecutionApplicationService,
    require_api_key: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["feedback"], dependencies=[Depends(require_api_key)])
    _register_proposal_routes(router, feedback_store)
    _register_task_read_routes(router, feedback_store)
    _register_external_governance_routes(router, feedback_store)
    _register_execution_job_routes(router, runtime)
    _register_compensation_routes(router, feedback_store)
    _register_execution_application_routes(router, execution_application)
    _register_task_regression_routes(router, feedback_store, runtime)
    _register_task_creation_routes(router, feedback_store)
    return router


def _register_proposal_routes(router: APIRouter, feedback_store: FeedbackStore) -> None:

    @router.get(
        "/optimization-proposals",
        response_model=list[OptimizationProposalResponse],
        summary="List pending feedback-driven optimization proposals",
    )
    async def list_optimization_proposals(
        feedback_case_id: str | None = None,
        status: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[OptimizationProposalResponse]:
        return feedback_store.list_proposals(feedback_case_id=feedback_case_id, status=status, limit=limit)

    @router.get(
        "/optimization-proposals/{proposal_id}",
        response_model=OptimizationProposalResponse,
        summary="Get one feedback-driven optimization proposal",
    )
    async def get_optimization_proposal(proposal_id: str) -> OptimizationProposalResponse:
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


def _register_task_read_routes(router: APIRouter, feedback_store: FeedbackStore) -> None:

    @router.get(
        "/optimization-tasks",
        response_model=list[OptimizationTaskResponse],
        summary="List feedback-driven optimization tasks",
    )
    async def list_optimization_tasks(
        feedback_case_id: str | None = None,
        status: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[OptimizationTaskResponse]:
        return feedback_store.list_tasks(feedback_case_id=feedback_case_id, status=status, limit=limit)

    @router.get(
        "/optimization-tasks/{task_id}",
        response_model=OptimizationTaskResponse,
        summary="Get one feedback-driven optimization task",
    )
    async def get_optimization_task(task_id: str) -> OptimizationTaskResponse:
        task = feedback_store.find_task(task_id)
        return ensure_found(task, "Optimization task not found")


def _register_external_governance_routes(router: APIRouter, feedback_store: FeedbackStore) -> None:

    @router.get(
        "/external-governance-webhooks",
        response_model=list[ExternalGovernanceWebhookResponse],
        summary="List configured external governance webhook aliases",
    )
    async def list_external_governance_webhooks() -> list[ExternalGovernanceWebhookResponse]:
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
    ) -> list[ExternalGovernanceItemResponse]:
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
    ) -> ExternalGovernanceItemResponse:
        result = feedback_store.notify_external_governance_item(external_item_id, webhook_alias=req.webhook_alias)
        return ensure_found(result, "External governance item not found")


def _register_execution_job_routes(
    router: APIRouter,
    runtime: ClaudeRuntime,
) -> None:

    @router.post(
        "/optimization-tasks/{task_id}/execution-jobs",
        response_model=AgentJobResponse,
        summary="Queue one controlled execution plan for an optimization task",
    )
    async def create_optimization_execution_job(task_id: str, req: OptimizationExecutionCreateRequest) -> AgentJobResponse:
        job = runtime.queue_execution_job(task_id, force=req.force)
        if not job:
            raise_conflict("Optimization task cannot queue an execution plan")
        return job


def _register_compensation_routes(router: APIRouter, feedback_store: FeedbackStore) -> None:

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
    ) -> list[ExecutionCompensationResponse]:
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
    async def get_execution_compensation(compensation_id: str) -> ExecutionCompensationResponse:
        compensation = feedback_store.find_execution_compensation(compensation_id)
        return ensure_found(compensation, "Execution compensation not found")


def _register_execution_application_routes(
    router: APIRouter,
    execution_application: ExecutionApplicationService,
) -> None:

    @router.post(
        "/optimization-tasks/{task_id}/execution-jobs/{execution_job_id}/apply",
        response_model=OptimizationExecutionApplyResponse,
        summary="Apply one reviewed controlled execution plan",
    )
    async def apply_optimization_execution_job(
        task_id: str,
        execution_job_id: str,
        req: OptimizationExecutionApplyRequest,
    ) -> OptimizationExecutionApplyResponse:
        require_request(req.confirm, "confirm must be true")
        return execution_application.apply_ready_execution_job(task_id, execution_job_id, note=req.note)

    @router.post(
        "/execution-compensations/{compensation_id}/restore",
        response_model=ExecutionCompensationResponse,
        summary="Restore workspace files for one pending execution compensation",
    )
    async def restore_execution_compensation(compensation_id: str) -> ExecutionCompensationResponse:
        return execution_application.restore_execution_compensation(compensation_id)

    @router.post(
        "/optimization-tasks/{task_id}/mark-applied",
        response_model=OptimizationTaskResponse,
        summary="Mark one optimization task as manually applied and snapshot the main Agent version",
    )
    async def mark_optimization_task_applied(
        task_id: str,
        req: OptimizationTaskMarkAppliedRequest,
    ) -> OptimizationTaskResponse:
        return execution_application.mark_task_applied_manually(task_id, note=req.note)


def _task_regression_eval_case_ids(
    feedback_store: FeedbackStore,
    task: dict[str, Any],
    requested_eval_case_ids: list[str] | None,
) -> list[str]:
    eval_case_ids = list(requested_eval_case_ids or [])
    if eval_case_ids or not task.get("feedback_case_id"):
        return eval_case_ids
    return [
        item["eval_case_id"]
        for item in feedback_store.list_eval_cases(
            status="active",
            source_feedback_case_id=str(task["feedback_case_id"]),
            limit=100,
        )
    ]


def _register_task_regression_routes(
    router: APIRouter,
    feedback_store: FeedbackStore,
    runtime: ClaudeRuntime,
) -> None:

    @router.post(
        "/optimization-tasks/{task_id}/regression-runs",
        response_model=EvalRunResponse,
        summary="Run manual regression validation for one optimization task",
    )
    async def create_optimization_task_regression_run(
        task_id: str,
        req: FeedbackEvalRunCreateRequest,
    ) -> EvalRunResponse:
        task = feedback_store.find_task(task_id)
        task = ensure_found(task, "Optimization task not found")
        if not task.get("applied_agent_version_id"):
            raise_conflict("Task must be marked applied before regression validation")
        eval_case_ids = _task_regression_eval_case_ids(feedback_store, task, req.eval_case_ids)
        if not eval_case_ids:
            raise_conflict("No active eval cases found for this task")
        result = await runtime.run_feedback_eval(
            eval_case_ids=eval_case_ids,
            optimization_task_id=task_id,
            source="manual_task_regression",
        )
        if not result:
            raise_conflict("Regression run could not be started")
        return EvalRunResponse.model_validate(result)

    @router.get(
        "/optimization-tasks/{task_id}/regression-runs",
        response_model=list[EvalRunResponse],
        summary="List regression validation runs for one optimization task",
    )
    async def list_optimization_task_regression_runs(
        task_id: str,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[EvalRunResponse]:
        return feedback_store.list_eval_runs(optimization_task_id=task_id, limit=limit)


def _register_task_creation_routes(router: APIRouter, feedback_store: FeedbackStore) -> None:

    @router.post(
        "/optimization-proposals/{proposal_id}/tasks",
        response_model=OptimizationTaskResponse,
        summary="Create one feedback-driven optimization task",
    )
    async def create_optimization_task(proposal_id: str, req: OptimizationTaskCreateRequest) -> OptimizationTaskResponse:
        require_request(not req.proposal_id or req.proposal_id == proposal_id, "proposal_id path/body mismatch")
        task = feedback_store.create_task(
            proposal_id=proposal_id,
            execution_mode=req.execution_mode,
            comment=req.comment,
        )
        if not task:
            raise_conflict("Proposal is missing, not approved, or not actionable")
        return task
