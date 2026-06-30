from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Query

from app.runtime.agent_paths import InvalidAgentId, validate_agent_id
from app.runtime.config_mapping import DEFAULT_AGENT_ID, build_config_mapping
from app.runtime.schemas import ConfigMappingResponse
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_registry_store import AgentRegistryStore


def create_config_router(
    *,
    settings: AppSettings,
    agent_registry_store: AgentRegistryStore,
    require_api_key: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["config"], dependencies=[Depends(require_api_key)])

    @router.get(
        "/config",
        response_model=ConfigMappingResponse,
        summary="Inspect Claude Code configuration mapping",
        description="Returns path, mount, scope, load, and git-policy metadata without exposing sensitive file contents.",
    )
    async def config_mapping(
        agent_id: str = Query(default=DEFAULT_AGENT_ID, description="Business agent id from /api/agent-registry."),
        include_host_mounts: bool = Query(default=False, description="Include host mount paths for operator diagnostics."),
    ) -> ConfigMappingResponse:
        try:
            safe_agent_id = validate_agent_id(agent_id)
        except InvalidAgentId as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if agent_registry_store.get_agent(safe_agent_id) is None:
            raise HTTPException(status_code=404, detail=f"Business agent not found: {safe_agent_id}")
        return build_config_mapping(settings, agent_id=safe_agent_id, expose_host_mount=include_host_mounts)

    return router
