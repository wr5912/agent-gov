from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends

from app.runtime.business_agent_workspace import initialize_business_agent_workspace
from app.runtime.schemas import AgentCreateRequest, AgentSummaryResponse
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_registry_store import AgentRegistryRecord, AgentRegistryStore


def _summary(record: AgentRegistryRecord) -> AgentSummaryResponse:
    return AgentSummaryResponse(
        agent_id=record.agent_id,
        name=record.name,
        category=record.category,
        workspace_dir=record.workspace_dir,
        created_at=record.created_at,
    )


def create_agents_router(*, settings: AppSettings, agent_registry_store: AgentRegistryStore, require_api_key: Callable) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["agents"], dependencies=[Depends(require_api_key)])

    @router.get(
        "/agent-registry",
        response_model=list[AgentSummaryResponse],
        summary="List registered business agents (governance objects)",
    )
    async def list_agents() -> list[AgentSummaryResponse]:
        return [_summary(record) for record in agent_registry_store.list_agents()]

    @router.post(
        "/agent-registry",
        response_model=AgentSummaryResponse,
        status_code=201,
        summary="Register a business agent (governance object)",
    )
    async def create_agent(req: AgentCreateRequest) -> AgentSummaryResponse:
        agent_id = (req.agent_id or "").strip() or f"biz-{uuid4().hex[:12]}"
        workspace_dir = str(settings.data_dir / "business-agents" / agent_id)
        record = agent_registry_store.create_business_agent(name=req.name, agent_id=agent_id, workspace_dir=workspace_dir)
        initialize_business_agent_workspace(Path(record.workspace_dir), agent_id=record.agent_id, name=record.name)
        return _summary(record)

    return router
