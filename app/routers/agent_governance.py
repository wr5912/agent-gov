from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Query

from app.routers.error_helpers import ensure_found, raise_conflict
from app.runtime.json_types import JsonObject
from app.runtime.protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from app.runtime.response_schemas.agent_governance_response_schemas import (
    AgentChangeSetActionRequest,
    AgentChangeSetApproveRequest,
    AgentChangeSetCreateRequest,
    AgentChangeSetEventResponse,
    AgentChangeSetPublishRequest,
    AgentChangeSetResponse,
    AgentGitDiffResponse,
    AgentGitFileDiffResponse,
    AgentGitRefResponse,
    AgentReleaseResponse,
    AgentRepositoryStatusResponse,
)
from app.runtime.state_machines import AgentChangeSetStatus, AgentReleaseStatus
from app.services.agent_governance import AgentGovernanceService
from app.services.agent_release_provenance import source_feedback_case_ids_by_release


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
    def get_agent_repository_status(
        agent_id: str | None = Query(default=None, description=f"Defaults to {DEFAULT_BUSINESS_AGENT_ID}."),
    ) -> AgentRepositoryStatusResponse:
        return agent_governance.repository_status(agent_id)

    @router.get(
        "/agent-repository/current",
        response_model=AgentGitRefResponse,
        summary="Get the selected business Agent's current published Git ref",
    )
    def get_current_agent_ref(agent_id: str | None = Query(default=None)) -> AgentGitRefResponse:
        return agent_governance.current_ref(agent_id)


def _register_change_set_read_routes(router: APIRouter, agent_governance: AgentGovernanceService) -> None:
    @router.get(
        "/agent-change-sets",
        response_model=list[AgentChangeSetResponse],
        summary="列出 Agent 待发布变更",
    )
    def list_agent_change_sets(
        status: AgentChangeSetStatus | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[AgentChangeSetResponse]:
        return agent_governance.list_change_sets(status=status, limit=limit)

    @router.post(
        "/agent-change-sets",
        response_model=AgentChangeSetResponse,
        summary="创建 Agent 待发布变更的隔离 worktree",
    )
    def create_agent_change_set(req: AgentChangeSetCreateRequest) -> AgentChangeSetResponse:
        return agent_governance.create_change_set(
            base_commit_sha=req.base_commit_sha,
            title=req.title,
            note=req.note,
        )

    @router.get(
        "/agent-change-sets/{change_set_id}",
        response_model=AgentChangeSetResponse,
        summary="获取一个 Agent 待发布变更",
    )
    def get_agent_change_set(change_set_id: str) -> AgentChangeSetResponse:
        return ensure_found(agent_governance.get_change_set(change_set_id), "Agent change set not found")

    @router.get(
        "/agent-change-sets/{change_set_id}/events",
        response_model=list[AgentChangeSetEventResponse],
        summary="列出一个 Agent 待发布变更的生命周期事件",
    )
    def list_agent_change_set_events(change_set_id: str) -> list[AgentChangeSetEventResponse]:
        ensure_found(agent_governance.get_change_set(change_set_id), "Agent change set not found")
        return agent_governance.list_change_set_events(change_set_id)

    @router.get(
        "/agent-change-sets/{change_set_id}/diff",
        response_model=AgentGitDiffResponse,
        summary="比较 Agent 待发布变更与修复前提交",
    )
    def diff_agent_change_set(change_set_id: str) -> AgentGitDiffResponse:
        change_set = ensure_found(agent_governance.get_change_set(change_set_id), "Agent change set not found")
        candidate = _require_candidate_commit(change_set)
        diff = agent_governance.change_set_diff(change_set, candidate)
        return ensure_found(diff, "Agent change set diff not found")

    @router.get(
        "/agent-change-sets/{change_set_id}/file-diff",
        response_model=AgentGitFileDiffResponse,
        summary="比较 Agent 待发布变更中的单个文件",
    )
    def diff_agent_change_set_file(change_set_id: str, path: str) -> AgentGitFileDiffResponse:
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
        summary="批准 Agent 待发布变更进入发布",
    )
    def approve_agent_change_set(change_set_id: str, req: AgentChangeSetApproveRequest) -> AgentChangeSetResponse:
        return agent_governance.approve_change_set(
            change_set_id,
            candidate_commit_sha=req.candidate_commit_sha,
            diff_digest=req.diff_digest,
            test_run_id=req.test_run_id,
            suite_digest=req.suite_digest,
            reviewed_files=[item.model_dump(mode="json") for item in req.reviewed_files],
            operator=req.operator,
            note=req.note,
        )

    @router.post(
        "/agent-change-sets/{change_set_id}/reject",
        response_model=AgentChangeSetResponse,
        summary="拒绝 Agent 待发布变更",
    )
    def reject_agent_change_set(change_set_id: str, req: AgentChangeSetActionRequest) -> AgentChangeSetResponse:
        return agent_governance.reject_change_set(change_set_id, operator=req.operator, note=req.note)

    @router.post(
        "/agent-change-sets/{change_set_id}/abandon",
        response_model=AgentChangeSetResponse,
        summary="放弃 Agent 待发布变更",
    )
    def abandon_agent_change_set(change_set_id: str, req: AgentChangeSetActionRequest) -> AgentChangeSetResponse:
        return agent_governance.abandon_change_set(change_set_id, operator=req.operator, note=req.note)

    @router.post(
        "/agent-change-sets/{change_set_id}/publish",
        response_model=AgentReleaseResponse,
        summary="发布已批准的 Agent 待发布变更",
    )
    async def publish_agent_change_set(change_set_id: str, req: AgentChangeSetPublishRequest) -> AgentReleaseResponse:
        release = await agent_governance.publish_change_set_async(
            change_set_id,
            operator=req.operator,
            tag_name=req.tag_name,
            note=req.force_reason if req.force else req.note,
            force=req.force,
            expected_candidate_commit_sha=req.expected_candidate_commit_sha,
            expected_diff_digest=req.expected_diff_digest,
            expected_test_run_id=req.expected_test_run_id,
            expected_suite_digest=req.expected_suite_digest,
        )
        sources = source_feedback_case_ids_by_release(agent_governance.feedback_store.Session, [release])
        return AgentReleaseResponse.model_validate({**release, "source_feedback_case_ids": sources.for_release(str(release["release_id"]))})

    @router.post(
        "/agent-change-sets/{change_set_id}/worktree-cleanup/retry",
        response_model=AgentChangeSetResponse,
        summary="重试终态 Agent 待发布变更的持久化 worktree 清理",
    )
    def retry_agent_change_set_worktree_cleanup(
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
    def list_agent_releases(
        status: AgentReleaseStatus | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[AgentReleaseResponse]:
        releases = agent_governance.list_releases(status=status, limit=limit)
        sources = source_feedback_case_ids_by_release(agent_governance.feedback_store.Session, releases)
        return [
            AgentReleaseResponse.model_validate({**release, "source_feedback_case_ids": sources.for_release(str(release["release_id"]))})
            for release in releases
        ]

    @router.get(
        "/agent-releases/{release_id}",
        response_model=AgentReleaseResponse,
        summary="Get one Agent release",
    )
    def get_agent_release(release_id: str) -> AgentReleaseResponse:
        release = ensure_found(agent_governance.get_release(release_id), "Agent release not found")
        sources = source_feedback_case_ids_by_release(agent_governance.feedback_store.Session, [release])
        return AgentReleaseResponse.model_validate({**release, "source_feedback_case_ids": sources.for_release(str(release["release_id"]))})


def _require_candidate_commit(change_set: JsonObject) -> str:
    candidate = change_set.get("candidate_commit_sha")
    if not isinstance(candidate, str) or not candidate:
        raise_conflict("Agent change set has no candidate commit")
    return candidate
