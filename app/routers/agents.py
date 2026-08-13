from __future__ import annotations

import re
from collections.abc import Callable
from typing import Literal

from fastapi import APIRouter, Depends, Header, Query, Response, status

from app.agent_testing.schedule import AgentTestScheduleService
from app.agent_testing.store import AgentTestingStore
from app.runtime.agent_governance_schemas import AgentDeleteResponse, AgentPresentationResponse
from app.runtime.agent_governance_schemas import agent_summary_response as _summary
from app.runtime.agent_paths import AgentId
from app.runtime.errors import ConflictError, NotFoundError
from app.runtime.schemas import (
    AgentLifecycleTransitionRequest,
    AgentSummaryResponse,
)
from app.runtime.stores.agent_deletion_store import AgentDeletionOperation
from app.runtime.stores.agent_registry_store import AgentRegistryStore
from app.services.business_agent_deletion import BusinessAgentDeletionService, parse_agent_if_match
from app.services.business_agent_presentation import business_agent_presentation

_IMPACT_COUNT_CAP = 1000
_SAFE_DELETION_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


def create_agents_router(
    *,
    agent_registry_store: AgentRegistryStore,
    agent_testing_store: AgentTestingStore,
    deletion_service: BusinessAgentDeletionService,
    agent_test_schedule_service: AgentTestScheduleService | None = None,
    require_api_key: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["agents"], dependencies=[Depends(require_api_key)])

    def _has_passed_test(agent_id: str) -> bool:
        return any(str(run.get("status")) == "passed" for run in agent_testing_store.list_runs(agent_id=agent_id, limit=_IMPACT_COUNT_CAP))

    @router.get(
        "/agent-registry",
        response_model=list[AgentSummaryResponse],
        summary="List registered business agents (governance objects)",
    )
    async def list_agents() -> list[AgentSummaryResponse]:
        return [_summary(record) for record in agent_registry_store.list_agents()]

    @router.get(
        "/agent-registry/{agent_id}/presentation",
        response_model=AgentPresentationResponse,
        summary="Read structured Welcome Card content for a registered business agent",
    )
    async def get_agent_presentation(agent_id: AgentId) -> AgentPresentationResponse:
        record = agent_registry_store.get_agent(agent_id)
        if record is None:
            raise NotFoundError(f"Business agent not found: {agent_id}")
        return business_agent_presentation(record)

    @router.post(
        "/agent-registry/{agent_id}/lifecycle",
        response_model=AgentSummaryResponse,
        summary="Transition a business agent's lifecycle status (rejects illegal transitions)",
    )
    async def transition_agent(agent_id: AgentId, req: AgentLifecycleTransitionRequest) -> AgentSummaryResponse:
        # 生命周期转移（AGV-020）；非法转移由状态机拒绝并返回可理解错误（409）。
        # eval 门（AGV-027）：从 evaluating 进入 active 必须有该 Agent 通过的评估运行——
        # 复用能力配置或修改配置后须评估通过才能激活，避免未验证配置直接上线。
        if req.status == "active":
            current = agent_registry_store.get_agent(agent_id)
            if current is not None and current.status == "evaluating" and not _has_passed_test(agent_id):
                raise ConflictError(f"Agent {agent_id} cannot enter active from evaluating without a passed platform test run")
        transitioned = agent_registry_store.transition_business_agent(agent_id, status=req.status)
        if transitioned.status == "archived" and agent_test_schedule_service is not None:
            agent_test_schedule_service.disable_agent_schedule(agent_id)
        return _summary(transitioned)

    _register_deletion_routes(router, deletion_service)
    return router


def _register_deletion_routes(
    router: APIRouter,
    deletion_service: BusinessAgentDeletionService,
) -> None:
    @router.delete(
        "/agent-registry/{agent_id}",
        response_model=AgentDeleteResponse,
        responses={
            202: {
                "model": AgentDeleteResponse,
                "description": "Agent 已下线，后台磁盘清理仍待完成。",
                "headers": {
                    "Location": {
                        "description": "脱敏 deletion operation 状态查询入口。",
                        "schema": {"type": "string"},
                    }
                },
            }
        },
        summary="Delete a business agent and report its governance impact",
    )
    async def delete_agent(
        agent_id: AgentId,
        response: Response,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        if_match: str | None = Header(default=None, alias="If-Match"),
    ) -> AgentDeleteResponse:
        operation = deletion_service.delete(
            agent_id=agent_id,
            agent_instance_etag=parse_agent_if_match(if_match),
            idempotency_key=idempotency_key or "",
        )
        if operation.state == "completed":
            response.status_code = status.HTTP_200_OK
        else:
            response.status_code = status.HTTP_202_ACCEPTED
            response.headers["Location"] = f"/api/agent-deletion-operations/{operation.operation_id}"
        return _deletion_response(operation)

    @router.get(
        "/agent-deletion-operations",
        response_model=list[AgentDeleteResponse],
        summary="Discover bounded pending or recent business-Agent deletion operations",
    )
    async def list_agent_deletion_operations(
        state: Literal["cleanup_pending", "completed"] = Query(
            default="cleanup_pending",
            description="Durable deletion state to discover; pending is the recovery default.",
        ),
        limit: int = Query(
            default=20,
            ge=1,
            le=100,
            description="Maximum number of newest deletion operations to return.",
        ),
    ) -> list[AgentDeleteResponse]:
        return [_deletion_response(operation) for operation in deletion_service.list_statuses(state=state, limit=limit)]

    @router.get(
        "/agent-deletion-operations/{operation_id}",
        response_model=AgentDeleteResponse,
        summary="Read one durable business-Agent deletion operation",
    )
    async def get_agent_deletion_operation(operation_id: str) -> AgentDeleteResponse:
        return _deletion_response(deletion_service.get_status(operation_id))


def _deletion_response(operation: AgentDeletionOperation) -> AgentDeleteResponse:
    completed = operation.state == "completed"
    return AgentDeleteResponse.model_validate(
        {
            "operation_id": operation.operation_id,
            "state": operation.state,
            "deleted": operation.deleted,
            "impact": operation.impact,
            "workspace_removed": completed and operation.purge_confirmed,
            "cleanup_complete": completed,
            "last_error_code": _last_error_code(operation),
            "attempt_count": operation.attempt_count,
            "updated_at": operation.updated_at,
        }
    )


def _last_error_code(operation: AgentDeletionOperation) -> str | None:
    value = operation.error.get("error_code")
    if not isinstance(value, str) or _SAFE_DELETION_ERROR_CODE.fullmatch(value) is None:
        return None
    return value
