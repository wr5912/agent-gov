from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends

from app.runtime.errors import BusinessRuleViolation, NotFoundError
from app.runtime.improvement_content_schemas import (
    ExecutionResponse,
    RegressionAssessmentResponse,
    RegressionCase,
)
from app.runtime.stores.improvement_content_store import (
    ExecutionRecord,
    ImprovementContentStore,
    RegressionAssessmentRecord,
)
from app.runtime.stores.improvement_store import ImprovementStore
from app.services.improvement_execution_service import ImprovementExecutionService
from app.services.improvement_governor_service import ImprovementGovernorService


def _execution_response(record: ExecutionRecord) -> ExecutionResponse:
    return ExecutionResponse(
        execution_id=record.execution_id,
        improvement_id=record.improvement_id,
        summary=record.summary,
        changes_applied=list(record.changes_applied),
        agent_version=record.agent_version,
        risk_level=record.risk_level,
        rollback_strategy=record.rollback_strategy,
        rollback_instructions=list(record.rollback_instructions),
        status=record.status,
        generated_by=record.generated_by,
        change_set_id=record.change_set_id,
        applied_agent_version_id=record.applied_agent_version_id,
        applied_diff=dict(record.applied_diff),
        generation_trace_id=record.generation_trace_id,
        generation_trace_url=record.generation_trace_url,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _regression_response(record: RegressionAssessmentRecord) -> RegressionAssessmentResponse:
    return RegressionAssessmentResponse(
        regression_assessment_id=record.regression_assessment_id,
        improvement_id=record.improvement_id,
        summary=record.summary,
        cases=[
            RegressionCase(
                prompt=str(case.get("prompt", "")),
                expected_behavior=str(case.get("expected_behavior", "")),
                checkpoints=[str(value) for value in (case.get("checkpoints") or [])],
            )
            for case in record.cases
        ],
        suggested_gate_thresholds={
            str(key): str(value) for key, value in (record.suggested_gate_thresholds or {}).items()
        },
        status=record.status,
        generated_by=record.generated_by,
        generation_trace_id=record.generation_trace_id,
        generation_trace_url=record.generation_trace_url,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _has_applied_execution(record: ExecutionRecord | None) -> bool:
    if record is None:
        return False
    bound_candidate = bool(record.change_set_id and record.applied_agent_version_id and record.applied_diff)
    historical_manual_evidence = bool(record.changes_applied and record.agent_version.strip())
    return bound_candidate or historical_manual_evidence


def _register_execution_routes(
    router: APIRouter,
    *,
    content_store: ImprovementContentStore,
    execution_service: ImprovementExecutionService,
    require_improvement: Callable[[str], None],
) -> None:
    @router.post(
        "/improvements/{improvement_id}/execution/apply",
        response_model=ExecutionResponse,
        summary="Apply the confirmed plan in an isolated worktree and create a candidate Agent version",
    )
    async def apply_execution(improvement_id: str) -> ExecutionResponse:
        require_improvement(improvement_id)
        return _execution_response(await execution_service.generate_and_apply_execution(improvement_id))

    @router.get(
        "/improvements/{improvement_id}/execution",
        response_model=ExecutionResponse,
        summary="Get execution record (404 if none)",
    )
    async def get_execution(improvement_id: str) -> ExecutionResponse:
        record = content_store.get_execution(improvement_id)
        if record is None:
            raise NotFoundError(f"No execution record for improvement: {improvement_id}")
        return _execution_response(record)

    @router.post(
        "/improvements/{improvement_id}/execution/confirm",
        response_model=ExecutionResponse,
        summary="Confirm execution evidence bound to a candidate Agent version",
    )
    async def confirm_execution(improvement_id: str) -> ExecutionResponse:
        if not _has_applied_execution(content_store.get_execution(improvement_id)):
            raise BusinessRuleViolation(f"Applied execution evidence is required before confirmation: {improvement_id}")
        return _execution_response(
            content_store.set_execution_status(improvement_id, status="confirmed", advance_to_stage="execution")
        )


def _register_regression_routes(
    router: APIRouter,
    *,
    content_store: ImprovementContentStore,
    governor_service: ImprovementGovernorService,
    require_improvement: Callable[[str], None],
) -> None:
    @router.post(
        "/improvements/{improvement_id}/regression-assessment/generate",
        response_model=RegressionAssessmentResponse,
        summary="Generate regression assessment candidates through the governor",
    )
    async def generate_regression(improvement_id: str) -> RegressionAssessmentResponse:
        require_improvement(improvement_id)
        execution = content_store.get_execution(improvement_id)
        if execution is None or execution.status != "confirmed" or not _has_applied_execution(execution):
            raise BusinessRuleViolation(f"Confirmed execution is required before regression assessment: {improvement_id}")
        return _regression_response(
            await governor_service.generate_regression_assessment(improvement_id, advance_to_stage="regression")
        )

    @router.get(
        "/improvements/{improvement_id}/regression-assessment",
        response_model=RegressionAssessmentResponse,
        summary="Get regression assessment (404 if none)",
    )
    async def get_regression(improvement_id: str) -> RegressionAssessmentResponse:
        record = content_store.get_regression_assessment(improvement_id)
        if record is None:
            raise NotFoundError(f"No regression assessment for improvement: {improvement_id}")
        return _regression_response(record)

    @router.post(
        "/improvements/{improvement_id}/regression-assessment/confirm",
        response_model=RegressionAssessmentResponse,
        summary="Confirm regression assessment for typed TestDataset adoption",
    )
    async def confirm_regression(improvement_id: str) -> RegressionAssessmentResponse:
        execution = content_store.get_execution(improvement_id)
        if execution is None or execution.status != "confirmed" or not _has_applied_execution(execution):
            raise BusinessRuleViolation(f"Confirmed execution is required before regression assessment: {improvement_id}")
        return _regression_response(
            content_store.set_regression_assessment_status(
                improvement_id,
                status="confirmed",
                advance_to_stage="regression",
            )
        )


def create_improvement_execution_router(
    *,
    improvement_store: ImprovementStore,
    content_store: ImprovementContentStore,
    governor_service: ImprovementGovernorService,
    execution_service: ImprovementExecutionService,
    require_api_key: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["improvements"], dependencies=[Depends(require_api_key)])

    def require_improvement(improvement_id: str) -> None:
        if improvement_store.get_improvement(improvement_id) is None:
            raise NotFoundError(f"ImprovementItem not found: {improvement_id}")

    _register_execution_routes(
        router,
        content_store=content_store,
        execution_service=execution_service,
        require_improvement=require_improvement,
    )
    _register_regression_routes(
        router,
        content_store=content_store,
        governor_service=governor_service,
        require_improvement=require_improvement,
    )
    return router
