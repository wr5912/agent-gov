from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Query

from app.routers.error_helpers import ensure_found, raise_conflict
from app.runtime.json_types import JsonObject
from app.runtime.response_schemas.agent_governance_response_schemas import (
    AgentChangeSetActionRequest,
    AgentChangeSetCreateRequest,
    AgentChangeSetEventResponse,
    AgentChangeSetPublishRequest,
    AgentChangeSetResponse,
    AgentGitDiffResponse,
    AgentGitFileDiffResponse,
    AgentGitRefResponse,
    AgentReleaseResponse,
    AgentReleaseRestoreRequest,
    AgentReleaseRestoreResponse,
    AgentReleaseRollbackRequest,
    AgentRepositoryDiscardChangesRequest,
    AgentRepositorySnapshotRequest,
    AgentRepositoryStatusResponse,
)
from app.services.agent_governance import AgentGovernanceService


def create_agent_governance_router(
    *,
    agent_governance: AgentGovernanceService,
    require_api_key: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["feedback"], dependencies=[Depends(require_api_key)])
    _register_repository_routes(router, agent_governance)
    _register_change_set_read_routes(router, agent_governance)
    _register_change_set_action_routes(router, agent_governance)
    _register_release_routes(router, agent_governance)
    return router


def _register_repository_routes(router: APIRouter, agent_governance: AgentGovernanceService) -> None:
    @router.get(
        "/agent-repository",
        response_model=AgentRepositoryStatusResponse,
        summary="Get Git-backed Agent repository status",
    )
    async def get_agent_repository_status(agent_id: str | None = Query(default=None)) -> AgentRepositoryStatusResponse:
        return agent_governance.repository_status(agent_id)

    @router.post(
        "/agent-repository/discard-changes",
        response_model=AgentRepositoryStatusResponse,
        summary="Discard confirmed uncommitted changes from a business Agent workspace (default main-agent)",
    )
    async def discard_agent_repository_changes(
        req: AgentRepositoryDiscardChangesRequest, agent_id: str | None = Query(default=None)
    ) -> AgentRepositoryStatusResponse:
        return agent_governance.discard_repository_changes(req.paths, agent_id)

    @router.post(
        "/agent-repository/snapshot",
        response_model=AgentGitRefResponse,
        summary="Save a business Agent workspace as an Agent version (default main-agent)",
    )
    async def snapshot_agent_repository(req: AgentRepositorySnapshotRequest, agent_id: str | None = Query(default=None)) -> AgentGitRefResponse:
        return agent_governance.snapshot_repository(operator=req.operator, note=req.note, agent_id=agent_id)

    @router.get(
        "/agent-repository/current",
        response_model=AgentGitRefResponse,
        summary="Get current published Agent Git ref (default main-agent)",
    )
    async def get_current_agent_ref(agent_id: str | None = Query(default=None)) -> AgentGitRefResponse:
        return agent_governance.current_ref(agent_id)


def _register_change_set_read_routes(router: APIRouter, agent_governance: AgentGovernanceService) -> None:
    @router.get(
        "/agent-change-sets",
        response_model=list[AgentChangeSetResponse],
        summary="List Agent change sets",
    )
    async def list_agent_change_sets(
        status: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[AgentChangeSetResponse]:
        return agent_governance.list_change_sets(status=status, limit=limit)

    @router.post(
        "/agent-change-sets",
        response_model=AgentChangeSetResponse,
        summary="Create an Agent change set worktree",
    )
    async def create_agent_change_set(req: AgentChangeSetCreateRequest) -> AgentChangeSetResponse:
        return agent_governance.create_change_set(
            base_commit_sha=req.base_commit_sha,
            title=req.title,
            note=req.note,
        )

    @router.get(
        "/agent-change-sets/{change_set_id}",
        response_model=AgentChangeSetResponse,
        summary="Get one Agent change set",
    )
    async def get_agent_change_set(change_set_id: str) -> AgentChangeSetResponse:
        return ensure_found(agent_governance.get_change_set(change_set_id), "Agent change set not found")

    @router.get(
        "/agent-change-sets/{change_set_id}/events",
        response_model=list[AgentChangeSetEventResponse],
        summary="List lifecycle events for one Agent change set",
    )
    async def list_agent_change_set_events(change_set_id: str) -> list[AgentChangeSetEventResponse]:
        ensure_found(agent_governance.get_change_set(change_set_id), "Agent change set not found")
        return agent_governance.list_change_set_events(change_set_id)

    @router.get(
        "/agent-change-sets/{change_set_id}/diff",
        response_model=AgentGitDiffResponse,
        summary="Diff an Agent change set against its base commit",
    )
    async def diff_agent_change_set(change_set_id: str) -> AgentGitDiffResponse:
        change_set = ensure_found(agent_governance.get_change_set(change_set_id), "Agent change set not found")
        candidate = _require_candidate_commit(change_set)
        diff = agent_governance.change_set_diff(change_set, candidate)
        return ensure_found(diff, "Agent change set diff not found")

    @router.get(
        "/agent-change-sets/{change_set_id}/file-diff",
        response_model=AgentGitFileDiffResponse,
        summary="Diff one file in an Agent change set",
    )
    async def diff_agent_change_set_file(change_set_id: str, path: str) -> AgentGitFileDiffResponse:
        change_set = ensure_found(agent_governance.get_change_set(change_set_id), "Agent change set not found")
        candidate = _require_candidate_commit(change_set)
        diff = agent_governance.change_set_file_diff(change_set, candidate, path)
        return ensure_found(diff, "Agent change set file diff not found")


def _register_change_set_action_routes(
    router: APIRouter,
    agent_governance: AgentGovernanceService,
) -> None:
    @router.post(
        "/agent-change-sets/{change_set_id}/approve",
        response_model=AgentChangeSetResponse,
        summary="Approve an Agent change set for release",
    )
    async def approve_agent_change_set(change_set_id: str, req: AgentChangeSetActionRequest) -> AgentChangeSetResponse:
        return agent_governance.approve_change_set(change_set_id, operator=req.operator, note=req.note)

    @router.post(
        "/agent-change-sets/{change_set_id}/reject",
        response_model=AgentChangeSetResponse,
        summary="Reject an Agent change set",
    )
    async def reject_agent_change_set(change_set_id: str, req: AgentChangeSetActionRequest) -> AgentChangeSetResponse:
        return agent_governance.reject_change_set(change_set_id, operator=req.operator, note=req.note)

    @router.post(
        "/agent-change-sets/{change_set_id}/abandon",
        response_model=AgentChangeSetResponse,
        summary="Abandon an Agent change set",
    )
    async def abandon_agent_change_set(change_set_id: str, req: AgentChangeSetActionRequest) -> AgentChangeSetResponse:
        return agent_governance.abandon_change_set(change_set_id, operator=req.operator, note=req.note)

    @router.post(
        "/agent-change-sets/{change_set_id}/publish",
        response_model=AgentReleaseResponse,
        summary="Publish an approved Agent change set",
    )
    async def publish_agent_change_set(change_set_id: str, req: AgentChangeSetPublishRequest) -> AgentReleaseResponse:
        return agent_governance.publish_change_set(
            change_set_id,
            operator=req.operator,
            tag_name=req.tag_name,
            note=req.note,
            force=req.force,
        )

    @router.post(
        "/agent-change-sets/{change_set_id}/worktree-cleanup/retry",
        response_model=AgentChangeSetResponse,
        summary="Retry durable worktree cleanup for a terminal Agent change set",
    )
    async def retry_agent_change_set_worktree_cleanup(
        change_set_id: str,
        req: AgentChangeSetActionRequest,
    ) -> AgentChangeSetResponse:
        return agent_governance.retry_worktree_cleanup(
            change_set_id,
            operator=req.operator,
            force=True,
        )


def _register_release_routes(router: APIRouter, agent_governance: AgentGovernanceService) -> None:
    @router.get(
        "/agent-releases",
        response_model=list[AgentReleaseResponse],
        summary="List published Agent releases",
    )
    async def list_agent_releases(status: str | None = None, limit: int = Query(default=100, ge=1, le=500)) -> list[AgentReleaseResponse]:
        return agent_governance.list_releases(status=status, limit=limit)

    @router.get(
        "/agent-releases/{release_id}",
        response_model=AgentReleaseResponse,
        summary="Get one Agent release",
    )
    async def get_agent_release(release_id: str) -> AgentReleaseResponse:
        return ensure_found(agent_governance.get_release(release_id), "Agent release not found")

    @router.post(
        "/agent-releases/{release_id}/restore",
        response_model=AgentReleaseRestoreResponse,
        summary="Restore the main Agent workspace to one release",
    )
    async def restore_agent_release(release_id: str, req: AgentReleaseRestoreRequest) -> AgentReleaseRestoreResponse:
        return agent_governance.restore_release(release_id, operator=req.operator, note=req.note)

    @router.post(
        "/agent-releases/{release_id}/rollback",
        response_model=AgentReleaseResponse,
        summary="Rollback the main Agent workspace to one release",
    )
    async def rollback_agent_release(release_id: str, req: AgentReleaseRollbackRequest) -> AgentReleaseResponse:
        return agent_governance.rollback_release(release_id, operator=req.operator, note=req.note)


def _require_candidate_commit(change_set: JsonObject) -> str:
    candidate = change_set.get("candidate_commit_sha")
    if not isinstance(candidate, str) or not candidate:
        raise_conflict("Agent change set has no candidate commit")
    return candidate
