from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Query, Response, status

from .schedule import AgentTestScheduleService
from .schemas import (
    AgentTestAssetSummaryResponse,
    AgentTestMessageRequest,
    AgentTestMessageResponse,
    AgentTestRunCreateRequest,
    AgentTestRunHistoryResponse,
    AgentTestRunResponse,
    AgentTestScheduleEventResponse,
    AgentTestScheduleResponse,
    AgentTestScheduleUpdateRequest,
    AgentTestSessionCreateRequest,
    AgentTestSessionResponse,
    AgentTestSuiteFileResponse,
    AgentTestSuiteSummary,
)
from .service import AgentTestingError, AgentTestingService
from .store import AgentTestRunNotFound


def create_agent_testing_router(
    *,
    service: AgentTestingService,
    require_api_key: Callable,
    schedule_service: AgentTestScheduleService | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["agent-testing"], dependencies=[Depends(require_api_key)])
    _register_test_asset_routes(router, service, schedule_service)
    _register_test_run_routes(router, service)
    _register_test_session_routes(router, service)
    return router


def _register_test_asset_routes(
    router: APIRouter,
    service: AgentTestingService,
    schedule_service: AgentTestScheduleService | None,
) -> None:

    @router.get("/agent-registry/{agent_id}/test-suite", response_model=AgentTestSuiteSummary)
    def inspect_agent_test_suite(agent_id: str, commit_sha: str | None = None) -> AgentTestSuiteSummary:
        return service.inspect_suite(agent_id, commit_sha=commit_sha)

    @router.get("/agent-test-assets", response_model=list[AgentTestAssetSummaryResponse])
    def list_agent_test_assets() -> list[AgentTestAssetSummaryResponse]:
        return [AgentTestAssetSummaryResponse.model_validate(item) for item in service.list_test_assets()]

    @router.get("/agent-registry/{agent_id}/test-suite/file", response_model=AgentTestSuiteFileResponse)
    def get_agent_test_suite_file(
        agent_id: str,
        path: str = Query(min_length=1),
        commit_sha: str | None = None,
    ) -> AgentTestSuiteFileResponse:
        return AgentTestSuiteFileResponse.model_validate(service.get_suite_file(agent_id, path=path, commit_sha=commit_sha))

    @router.get("/agent-registry/{agent_id}/test-schedule", response_model=AgentTestScheduleResponse)
    def get_agent_test_schedule(agent_id: str) -> AgentTestScheduleResponse:
        schedules = _require_schedule_service(schedule_service)
        return AgentTestScheduleResponse.model_validate(schedules.read_schedule(agent_id))

    @router.put("/agent-registry/{agent_id}/test-schedule", response_model=AgentTestScheduleResponse)
    def update_agent_test_schedule(agent_id: str, request: AgentTestScheduleUpdateRequest) -> AgentTestScheduleResponse:
        schedules = _require_schedule_service(schedule_service)
        return AgentTestScheduleResponse.model_validate(
            schedules.update_schedule(
                agent_id,
                enabled=request.enabled,
                cron_expression=request.cron_expression,
                timezone_name=request.timezone,
            )
        )

    @router.get("/agent-registry/{agent_id}/test-schedule/events", response_model=list[AgentTestScheduleEventResponse])
    def list_agent_test_schedule_events(
        agent_id: str,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[AgentTestScheduleEventResponse]:
        schedules = _require_schedule_service(schedule_service)
        return [AgentTestScheduleEventResponse.model_validate(item) for item in schedules.list_events(agent_id, limit=limit)]


def _register_test_run_routes(router: APIRouter, service: AgentTestingService) -> None:
    @router.post("/agent-test-runs", response_model=AgentTestRunResponse, status_code=status.HTTP_202_ACCEPTED)
    def create_agent_test_run(request: AgentTestRunCreateRequest) -> AgentTestRunResponse:
        return AgentTestRunResponse.model_validate(
            service.create_run(
                agent_id=request.agent_id,
                commit_sha=request.commit_sha,
                change_set_id=None,
                source="manual",
            )
        )

    @router.post(
        "/agent-change-sets/{change_set_id}/test-runs",
        response_model=AgentTestRunResponse,
        status_code=status.HTTP_202_ACCEPTED,
        summary="为 Agent 待发布变更创建平台测试运行",
    )
    def create_agent_change_set_test_run(change_set_id: str) -> AgentTestRunResponse:
        return AgentTestRunResponse.model_validate(service.create_change_set_run(change_set_id))

    @router.get("/agent-test-runs", response_model=list[AgentTestRunResponse])
    def list_agent_test_runs(
        agent_id: str | None = None,
        change_set_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[AgentTestRunResponse]:
        return [AgentTestRunResponse.model_validate(item) for item in service.store.list_runs(agent_id=agent_id, change_set_id=change_set_id, limit=limit)]

    @router.get("/agent-test-runs/history", response_model=AgentTestRunHistoryResponse)
    def list_agent_test_run_history(
        agent_id: str | None = None,
        run_status: str | None = Query(default=None, alias="status"),
        source: str | None = None,
        commit_sha: str | None = None,
        cursor: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> AgentTestRunHistoryResponse:
        return AgentTestRunHistoryResponse.model_validate(
            service.list_run_history(
                agent_id=agent_id,
                status=run_status,
                source=source,
                commit_sha=commit_sha,
                cursor=cursor,
                limit=limit,
            )
        )

    @router.get("/agent-test-runs/{test_run_id}", response_model=AgentTestRunResponse)
    def get_agent_test_run(test_run_id: str) -> AgentTestRunResponse:
        payload = service.store.get_run(test_run_id)
        if payload is None:
            raise AgentTestingError(404, "AGENT_TEST_RUN_NOT_FOUND", f"Agent test run not found: {test_run_id}")
        return AgentTestRunResponse.model_validate(payload)

    @router.post("/agent-test-runs/{test_run_id}/cancel", response_model=AgentTestRunResponse)
    def cancel_agent_test_run(test_run_id: str) -> AgentTestRunResponse:
        try:
            return AgentTestRunResponse.model_validate(service.runner.cancel(test_run_id))
        except AgentTestRunNotFound as exc:
            raise AgentTestingError(404, "AGENT_TEST_RUN_NOT_FOUND", f"Agent test run not found: {test_run_id}") from exc


def _register_test_session_routes(router: APIRouter, service: AgentTestingService) -> None:
    @router.post("/agent-test-sessions", response_model=AgentTestSessionResponse, status_code=status.HTTP_201_CREATED)
    def create_agent_test_session(request: AgentTestSessionCreateRequest) -> AgentTestSessionResponse:
        return AgentTestSessionResponse.model_validate(
            service.create_session(agent_id=request.agent_id, commit_sha=request.commit_sha, change_set_id=request.change_set_id)
        )

    @router.post("/agent-test-sessions/{test_session_id}/messages", response_model=AgentTestMessageResponse)
    async def send_agent_test_message(test_session_id: str, request: AgentTestMessageRequest) -> AgentTestMessageResponse:
        result = await service.invoke(test_session_id, message=request.message, metadata=request.metadata)
        return AgentTestMessageResponse.model_validate(result.model_dump(mode="python"))

    @router.delete("/agent-test-sessions/{test_session_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_agent_test_session(test_session_id: str) -> Response:
        service.delete_session(test_session_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)


def _require_schedule_service(service: AgentTestScheduleService | None) -> AgentTestScheduleService:
    if service is None:
        raise AgentTestingError(503, "AGENT_TEST_SCHEDULER_UNAVAILABLE", "Agent test scheduler is not configured")
    return service
