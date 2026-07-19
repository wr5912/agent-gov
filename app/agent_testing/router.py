from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Query, Response, status

from .schemas import (
    AgentTestMessageRequest,
    AgentTestMessageResponse,
    AgentTestRunCreateRequest,
    AgentTestRunResponse,
    AgentTestSessionCreateRequest,
    AgentTestSessionResponse,
    AgentTestSuiteSummary,
)
from .service import AgentTestingError, AgentTestingService
from .store import AgentTestRunNotFound


def create_agent_testing_router(*, service: AgentTestingService, require_api_key: Callable) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["agent-testing"], dependencies=[Depends(require_api_key)])

    @router.get("/agent-registry/{agent_id}/test-suite", response_model=AgentTestSuiteSummary)
    def inspect_agent_test_suite(agent_id: str, commit_sha: str | None = None) -> AgentTestSuiteSummary:
        return service.inspect_suite(agent_id, commit_sha=commit_sha)

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

    return router
