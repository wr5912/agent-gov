from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Query

from app.runtime.config_file_schemas import (
    AgentConfigFileResponse,
    AgentConfigFileUpdateRequest,
    AgentConfigFileUpdateResponse,
)
from app.runtime.config_mapping import DEFAULT_AGENT_ID
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_registry_store import AgentRegistryStore
from app.services.agent_config_files import AgentConfigFileError, AgentConfigFileService


def create_agent_config_files_router(
    *,
    settings: AppSettings,
    agent_registry_store: AgentRegistryStore,
    session_store: LocalSessionStore,
    require_api_key: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["config"], dependencies=[Depends(require_api_key)])
    service = AgentConfigFileService(
        settings=settings,
        agent_registry_store=agent_registry_store,
        session_store=session_store,
    )

    @router.get(
        "/agent-config-file",
        response_model=AgentConfigFileResponse,
        summary="Read an editable business-agent project config file",
    )
    async def read_agent_config_file(
        agent_id: str = Query(default=DEFAULT_AGENT_ID, description="Business agent id from /api/agent-registry."),
        path: str = Query(description="Editable project config path. Currently only .mcp.json is supported."),
    ) -> AgentConfigFileResponse:
        try:
            return service.read_file(agent_id=agent_id, path=path)
        except AgentConfigFileError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    @router.put(
        "/agent-config-file",
        response_model=AgentConfigFileUpdateResponse,
        summary="Update an editable business-agent project config file",
    )
    async def update_agent_config_file(
        request: AgentConfigFileUpdateRequest,
        agent_id: str = Query(default=DEFAULT_AGENT_ID, description="Business agent id from /api/agent-registry."),
        path: str = Query(description="Editable project config path. Currently only .mcp.json is supported."),
    ) -> AgentConfigFileUpdateResponse:
        try:
            return service.update_file(agent_id=agent_id, path=path, request=request)
        except AgentConfigFileError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    return router
