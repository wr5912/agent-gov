from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends

from app.runtime.schemas import AgentSummaryResponse
from app.runtime.stores.agent_registry_store import AgentRegistryStore


def create_agents_router(*, agent_registry_store: AgentRegistryStore, require_api_key: Callable) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["agents"], dependencies=[Depends(require_api_key)])

    @router.get(
        "/agent-registry",
        response_model=list[AgentSummaryResponse],
        summary="List registered business agents (governance objects)",
    )
    async def list_agents() -> list[AgentSummaryResponse]:
        return [
            AgentSummaryResponse(
                agent_id=record.agent_id,
                name=record.name,
                category=record.category,
                workspace_dir=record.workspace_dir,
                created_at=record.created_at,
            )
            for record in agent_registry_store.list_agents()
        ]

    return router
