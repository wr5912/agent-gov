from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from app.runtime.agent_loader import discover_agents, discover_skills
from app.runtime.agent_paths import InvalidAgentId, validate_agent_id
from app.runtime.protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from app.runtime.schemas import AgentInfo, SkillInfo
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_registry_store import AgentRegistryStore


def create_catalog_router(
    *,
    settings: AppSettings,
    agent_registry_store: AgentRegistryStore,
    require_api_key: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["catalog"], dependencies=[Depends(require_api_key)])

    def _resolve_workspace(agent_id: str) -> Path:
        try:
            safe_agent_id = validate_agent_id(agent_id)
        except InvalidAgentId as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        record = agent_registry_store.get_agent(safe_agent_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"Business agent not found: {safe_agent_id}")
        return Path(record.workspace_dir)

    @router.get(
        "/agents",
        response_model=list[AgentInfo],
        summary="List configured AgentScope subagents",
    )
    async def list_agents(
        agent_id: str = Query(default=DEFAULT_BUSINESS_AGENT_ID, description="Business agent id from /api/agent-registry."),
    ) -> list[AgentInfo]:
        workspace_dir = _resolve_workspace(agent_id)
        return [
            AgentInfo(
                name=item["name"],
                path=item["path"],
                description=item.get("description"),
                model=item.get("model"),
                tools=item.get("tools") or [],
                skills=item.get("skills") or [],
            )
            for item in discover_agents(workspace_dir)
        ]

    @router.get(
        "/skills",
        response_model=list[SkillInfo],
        summary="List configured AgentScope skills",
    )
    async def list_skills(
        agent_id: str = Query(default=DEFAULT_BUSINESS_AGENT_ID, description="Business agent id from /api/agent-registry."),
    ) -> list[SkillInfo]:
        workspace_dir = _resolve_workspace(agent_id)
        return [
            SkillInfo(
                name=item["name"],
                path=item["path"],
                description=item.get("description"),
            )
            for item in discover_skills(workspace_dir)
        ]

    return router
