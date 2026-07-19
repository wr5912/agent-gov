from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends

from app.agent_testing.schemas import AgentTestRunResponse
from app.agent_testing.service import AgentTestingService
from app.runtime.errors import BusinessRuleViolation, DataIntegrityError, NotFoundError
from app.runtime.improvement_content_schemas import (
    ExecutionResponse,
    RegressionGeneratedTest,
    RegressionTestDesignResponse,
)
from app.runtime.stores.improvement_content_store import (
    ExecutionRecord,
    ImprovementContentStore,
    RegressionTestDesignRecord,
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


def _test_design_response(
    record: RegressionTestDesignRecord,
    *,
    generated_test_files: list[str] | None = None,
    candidate_commit_sha: str = "",
    test_run: AgentTestRunResponse | None = None,
) -> RegressionTestDesignResponse:
    return RegressionTestDesignResponse(
        regression_test_design_id=record.regression_test_design_id,
        improvement_id=record.improvement_id,
        summary=record.summary,
        tests=[
            RegressionGeneratedTest(
                target_path=str(test.get("target_path", "")),
                test_code=str(test.get("test_code", "")),
                test_intent=str(test.get("test_intent", "")),
                assertion_rationale=str(test.get("assertion_rationale", "")),
            )
            for test in record.tests
        ],
        no_action_reason=record.no_action_reason,
        status=record.status,
        generated_by=record.generated_by,
        generation_trace_id=record.generation_trace_id,
        generation_trace_url=record.generation_trace_url,
        created_at=record.created_at,
        updated_at=record.updated_at,
        generated_test_files=generated_test_files or [],
        candidate_commit_sha=candidate_commit_sha,
        test_run=test_run,
    )


def _test_design_runtime_projection(
    improvement_id: str,
    *,
    content_store: ImprovementContentStore,
    agent_testing: AgentTestingService,
) -> tuple[list[str], str, AgentTestRunResponse | None]:
    execution = content_store.get_execution(improvement_id)
    if execution is None:
        return [], "", None

    generated_test_files = [str(path) for path in execution.changes_applied if str(path).startswith("tests/")]
    candidate_commit_sha = execution.applied_agent_version_id or execution.agent_version
    if not execution.change_set_id or not candidate_commit_sha:
        return generated_test_files, candidate_commit_sha, None

    latest_run = next(
        (
            run
            for run in agent_testing.store.list_runs(change_set_id=execution.change_set_id, limit=20)
            if str(run.get("commit_sha") or "") == candidate_commit_sha
        ),
        None,
    )
    if latest_run is None:
        return generated_test_files, candidate_commit_sha, None
    full_run = agent_testing.store.get_run(str(latest_run["test_run_id"])) or latest_run
    return generated_test_files, candidate_commit_sha, AgentTestRunResponse.model_validate(full_run)


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
        return _execution_response(content_store.set_execution_status(improvement_id, status="confirmed", advance_to_stage="execution"))


def _register_regression_routes(
    router: APIRouter,
    *,
    content_store: ImprovementContentStore,
    governor_service: ImprovementGovernorService,
    execution_service: ImprovementExecutionService,
    agent_testing: AgentTestingService,
    require_improvement: Callable[[str], None],
) -> None:
    @router.post(
        "/improvements/{improvement_id}/regression-test-design/generate",
        response_model=RegressionTestDesignResponse,
        summary="Generate regression test design candidates through the governor",
    )
    async def generate_regression(improvement_id: str) -> RegressionTestDesignResponse:
        require_improvement(improvement_id)
        execution = content_store.get_execution(improvement_id)
        if execution is None or execution.status != "confirmed" or not _has_applied_execution(execution):
            raise BusinessRuleViolation(f"Confirmed execution is required before regression test design: {improvement_id}")
        return _test_design_response(await governor_service.generate_regression_test_design(improvement_id, advance_to_stage="regression"))

    @router.get(
        "/improvements/{improvement_id}/regression-test-design",
        response_model=RegressionTestDesignResponse,
        summary="Get regression test design (404 if none)",
    )
    async def get_regression(improvement_id: str) -> RegressionTestDesignResponse:
        record = content_store.get_regression_test_design(improvement_id)
        if record is None:
            raise NotFoundError(f"No regression test design for improvement: {improvement_id}")
        generated_test_files, candidate_commit_sha, test_run = _test_design_runtime_projection(
            improvement_id,
            content_store=content_store,
            agent_testing=agent_testing,
        )
        return _test_design_response(
            record,
            generated_test_files=generated_test_files,
            candidate_commit_sha=candidate_commit_sha,
            test_run=test_run,
        )

    @router.post(
        "/improvements/{improvement_id}/regression-test-design/confirm",
        response_model=RegressionTestDesignResponse,
        summary="Confirm the regression test design before generating Workspace pytest files",
    )
    async def confirm_regression(improvement_id: str) -> RegressionTestDesignResponse:
        execution = content_store.get_execution(improvement_id)
        if execution is None or execution.status != "confirmed" or not _has_applied_execution(execution):
            raise BusinessRuleViolation(f"Confirmed execution is required before regression test design: {improvement_id}")
        materialized = execution_service.materialize_regression_tests(improvement_id)
        generated_test_files = materialized.get("generated_test_files")
        candidate_commit_sha = materialized.get("candidate_commit_sha")
        if not isinstance(generated_test_files, list) or not isinstance(candidate_commit_sha, str) or not candidate_commit_sha:
            raise DataIntegrityError("Regression test materialization returned an invalid candidate projection")
        record = content_store.set_regression_test_design_status(
            improvement_id,
            status="confirmed",
            advance_to_stage="regression",
        )
        return _test_design_response(
            record,
            generated_test_files=[str(value) for value in generated_test_files],
            candidate_commit_sha=candidate_commit_sha,
        )


def create_improvement_execution_router(
    *,
    improvement_store: ImprovementStore,
    content_store: ImprovementContentStore,
    governor_service: ImprovementGovernorService,
    execution_service: ImprovementExecutionService,
    agent_testing: AgentTestingService,
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
        execution_service=execution_service,
        agent_testing=agent_testing,
        require_improvement=require_improvement,
    )
    return router
