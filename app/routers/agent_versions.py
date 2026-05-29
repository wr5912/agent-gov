from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, Query

from app.routers.error_helpers import ensure_found
from app.runtime.agent_version_store import AgentVersionStore
from app.runtime.response_schemas.agent_version_response_schemas import (
    AgentVersionDiffResponse,
    AgentVersionFileDiffResponse,
    AgentVersionManifestResponse,
    AgentVersionRestoreResponse,
    AgentVersionSummaryResponse,
)
from app.runtime.schemas import (
    AgentVersionRestoreRequest,
    AgentVersionSnapshotRequest,
)


def create_agent_versions_router(
    *,
    agent_version_store: AgentVersionStore,
    require_api_key: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["feedback"], dependencies=[Depends(require_api_key)])

    @router.get(
        "/agent-versions/main/current",
        response_model=AgentVersionSummaryResponse,
        summary="Get current Agent managed configuration version",
    )
    async def current_agent_version() -> dict[str, Any]:
        return agent_version_store.ensure_bootstrap()

    @router.get(
        "/agent-versions/main",
        response_model=list[AgentVersionSummaryResponse],
        summary="List Agent managed configuration versions",
    )
    async def list_agent_versions(limit: int = Query(default=100, ge=1, le=500)) -> list[dict[str, Any]]:
        agent_version_store.ensure_bootstrap()
        return agent_version_store.list_versions(limit=limit)

    @router.post(
        "/agent-versions/main/snapshots",
        response_model=AgentVersionSummaryResponse,
        summary="Create one Agent managed configuration snapshot",
    )
    async def create_agent_version_snapshot(req: AgentVersionSnapshotRequest) -> dict[str, Any]:
        return agent_version_store.create_snapshot(
            reason=req.reason or "manual_snapshot",
            source_proposal_ids=req.source_proposal_ids,
            note=req.note,
        )

    @router.post(
        "/agent-versions/main/{version_id}/rollback",
        response_model=AgentVersionRestoreResponse,
        summary="Restore one Agent managed configuration version",
    )
    async def restore_agent_version(version_id: str, req: AgentVersionRestoreRequest) -> AgentVersionRestoreResponse:
        result = agent_version_store.restore_version(version_id, note=req.note)
        return AgentVersionRestoreResponse(**ensure_found(result, "Agent version not found"))

    @router.get(
        "/agent-versions/main/diff",
        response_model=AgentVersionDiffResponse,
        summary="Diff two Agent managed configuration versions",
    )
    async def diff_agent_versions(from_version_id: str, to_version_id: str) -> dict[str, Any]:
        diff = agent_version_store.diff_versions(from_version_id, to_version_id)
        return ensure_found(diff, "Agent version not found")

    @router.get(
        "/agent-versions/main/file-diff",
        response_model=AgentVersionFileDiffResponse,
        summary="Diff one file between two Agent managed configuration versions",
    )
    async def diff_agent_version_file(from_version_id: str, to_version_id: str, path: str) -> dict[str, Any]:
        diff = agent_version_store.diff_version_file(from_version_id, to_version_id, path)
        return ensure_found(diff, "Agent version or file path not found")

    @router.get(
        "/agent-versions/main/{version_id}",
        response_model=AgentVersionManifestResponse,
        summary="Get one Agent version manifest",
    )
    async def get_agent_version(version_id: str) -> dict[str, Any]:
        manifest = agent_version_store.get_manifest(version_id)
        return ensure_found(manifest, "Agent version not found")

    return router
