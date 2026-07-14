from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from scripts.bootstrap_runtime_volume import load_runtime_env

from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_paths import InvalidAgentId, business_agent_layout
from app.runtime.business_agent_workspace import (
    DEFAULT_TEMPLATE_ID,
    list_business_agent_templates,
    seed_business_agent_workspace,
    seed_declared_business_agent_workspace,
)
from app.runtime.errors import ConflictError
from app.runtime.managed_agent_policy import ManagedAgentPolicyError, require_runtime_workspace_policy
from app.runtime.schemas import (
    AgentCreateRequest,
    AgentDeleteResponse,
    AgentDeletionImpact,
    AgentLifecycleTransitionRequest,
    AgentSummaryResponse,
    BusinessAgentTemplatesResponse,
)
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_registry_store import AgentRegistryRecord, AgentRegistryStore
from app.runtime.stores.feedback_store import FeedbackStore
from app.runtime.stores.improvement_store import ImprovementStore
from app.services.agent_governance import AgentGovernanceService

_PASSED_EVAL_RESULT_STATUSES = {"passed", "passed_with_notes"}

_IMPACT_COUNT_CAP = 1000


def _summary(record: AgentRegistryRecord) -> AgentSummaryResponse:
    return AgentSummaryResponse(
        agent_id=record.agent_id,
        name=record.name,
        category=record.category,
        workspace_dir=record.workspace_dir,
        created_at=record.created_at,
        status=record.status,
        origin=record.origin,
        requires_web_hitl=record.requires_web_hitl,
    )


def _resolve_template_id(raw: str | None) -> str:
    """校验创建用 template_id（外部输入）；未知值投影为 422。"""
    template_id = (raw or DEFAULT_TEMPLATE_ID).strip() or DEFAULT_TEMPLATE_ID
    if template_id not in list_business_agent_templates():
        raise HTTPException(status_code=422, detail=f"Unknown template_id: {template_id!r}")
    return template_id


def _register_and_seed_agent(req: AgentCreateRequest, settings: AppSettings, store: AgentRegistryStore) -> AgentSummaryResponse:
    """Stage, validate, version, atomically install, then register a business Agent."""
    template_id = _resolve_template_id(req.template_id)
    agent_id = (req.agent_id or "").strip() or f"biz-{uuid4().hex[:12]}"
    try:
        # 缺陷③：agent_id 直接作路径段，business_agent_layout 收敛了防穿越校验，非法 → 422。
        layout = business_agent_layout(settings.data_dir, agent_id)
    except InvalidAgentId as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if store.get_agent(agent_id) is not None:
        raise ConflictError(f"Business agent already exists: {agent_id}")
    layout.root.parent.mkdir(parents=True, exist_ok=True)
    staging_root = layout.root.parent / f".{agent_id}.staging-{uuid4().hex}"
    staging_workspace = staging_root / "workspace"
    try:
        env = dict(load_runtime_env(settings.settings_env_file)) if settings.settings_env_file else dict(os.environ)
        runtime_root = Path("/") if settings.data_dir.resolve() == Path("/data") else settings.data_dir.resolve().parent
        used_declared_seed = req.template_id is None and seed_declared_business_agent_workspace(
            staging_workspace,
            agent_id=agent_id,
            runtime_volume_mode=settings.runtime_volume_mode,
            env=env,
            runtime_root=runtime_root,
        )
        if not used_declared_seed:
            seed_business_agent_workspace(
                staging_workspace,
                agent_id=agent_id,
                name=req.name,
                template_id=template_id,
            )
        require_runtime_workspace_policy(
            workspace=staging_workspace,
            agent_id=agent_id,
            runtime_mode=settings.runtime_volume_mode,
            env=env,
            runtime_root=runtime_root,
        )
        GitAgentVersionStore(
            repository_dir=staging_workspace,
            worktrees_dir=staging_root / "version" / "worktrees",
            releases_dir=staging_root / "version" / "releases",
            repository_name=f"{agent_id}-config",
            git_user_name=settings.agent_git_user_name,
            git_user_email=settings.agent_git_user_email,
        ).ensure_bootstrap()
        if layout.root.exists():
            raise ConflictError(f"Business agent runtime directory already exists: {agent_id}")
        os.replace(staging_root, layout.root)
        try:
            record = store.create_business_agent(
                name=req.name,
                agent_id=agent_id,
                workspace_dir=str(layout.workspace),
            )
        except Exception:
            shutil.rmtree(layout.root, ignore_errors=True)
            raise
        return _summary(record)
    except ManagedAgentPolicyError as exc:
        raise HTTPException(status_code=422, detail=f"Business agent template violates managed policy: {exc}") from exc
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def create_agents_router(
    *,
    settings: AppSettings,
    agent_registry_store: AgentRegistryStore,
    feedback_store: FeedbackStore,
    improvement_store: ImprovementStore,
    agent_governance: AgentGovernanceService,
    require_api_key: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["agents"], dependencies=[Depends(require_api_key)])

    def _has_passed_eval(agent_id: str) -> bool:
        """该 Agent 是否有通过的评估运行（completed + passed/passed_with_notes），用于激活门。"""
        runs = feedback_store.list_eval_runs(agent_id=agent_id, status="completed", limit=_IMPACT_COUNT_CAP)
        return any(str(run.get("result_status")) in _PASSED_EVAL_RESULT_STATUSES for run in runs)

    @router.get(
        "/agent-registry",
        response_model=list[AgentSummaryResponse],
        summary="List registered business agents (governance objects)",
    )
    async def list_agents() -> list[AgentSummaryResponse]:
        return [_summary(record) for record in agent_registry_store.list_agents()]

    @router.get(
        "/agent-registry/templates",
        response_model=BusinessAgentTemplatesResponse,
        summary="List business agent creation templates (catalog)",
    )
    async def list_templates() -> BusinessAgentTemplatesResponse:
        return BusinessAgentTemplatesResponse(templates=list_business_agent_templates())

    @router.post(
        "/agent-registry",
        response_model=AgentSummaryResponse,
        status_code=201,
        summary="Register a business agent (governance object)",
    )
    async def create_agent(req: AgentCreateRequest) -> AgentSummaryResponse:
        return _register_and_seed_agent(req, settings, agent_registry_store)

    @router.post(
        "/agent-registry/{agent_id}/lifecycle",
        response_model=AgentSummaryResponse,
        summary="Transition a business agent's lifecycle status (rejects illegal transitions)",
    )
    async def transition_agent(agent_id: str, req: AgentLifecycleTransitionRequest) -> AgentSummaryResponse:
        # 生命周期转移（AGV-020）；非法转移由状态机拒绝并返回可理解错误（409）。
        # eval 门（AGV-027）：从 evaluating 进入 active 必须有该 Agent 通过的评估运行——
        # 复用场景包/配置变更后须评估通过才能激活，避免未验证配置直接上线。
        if req.status == "active":
            current = agent_registry_store.get_agent(agent_id)
            if current is not None and current.status == "evaluating" and not _has_passed_eval(agent_id):
                raise ConflictError(f"Agent {agent_id} cannot enter active from evaluating without a passed eval run")
        return _summary(agent_registry_store.transition_business_agent(agent_id, status=req.status))

    @router.delete(
        "/agent-registry/{agent_id}",
        response_model=AgentDeleteResponse,
        summary="Delete a business agent and report its governance impact",
    )
    async def delete_agent(agent_id: str) -> AgentDeleteResponse:
        # 删除前先给出影响面提示（该 Agent 归属的运行/反馈/优化/评估/版本计数），避免无声删除治理对象。
        impact = AgentDeletionImpact(
            runs=len(feedback_store.list_runs(agent_id=agent_id, limit=_IMPACT_COUNT_CAP)),
            feedback_signals=len(feedback_store.list_signals(agent_id=agent_id, limit=_IMPACT_COUNT_CAP)),
            improvements=len(improvement_store.list_improvements(agent_id=agent_id)),
            eval_runs=len(feedback_store.list_eval_runs(agent_id=agent_id, limit=_IMPACT_COUNT_CAP)),
            change_sets=len(agent_governance.list_change_sets(agent_id=agent_id, limit=_IMPACT_COUNT_CAP)),
            releases=len(agent_governance.list_releases(agent_id=agent_id, limit=_IMPACT_COUNT_CAP)),
        )
        deleted = agent_registry_store.delete_business_agent(agent_id)  # main 不可删→400，未知→404
        return AgentDeleteResponse(deleted=_summary(deleted), impact=impact)

    return router
