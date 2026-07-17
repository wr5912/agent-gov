from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException

from app.runtime.agent_governance_schemas import agent_summary_response as _summary
from app.runtime.agent_paths import InvalidAgentId, business_agent_layout, validate_agent_id
from app.runtime.business_agent_seed_catalog import declared_business_agent_ids, runtime_seed_catalog_dir
from app.runtime.business_agent_workspace import (
    DEFAULT_TEMPLATE_ID,
    InvalidDeclaredBusinessAgentSeed,
    WorkspaceSafetyError,
    list_business_agent_templates,
    prepare_business_agent_workspace,
    prepare_declared_business_agent_workspace,
)
from app.runtime.errors import ConflictError
from app.runtime.managed_agent_policy import ManagedAgentPolicyError, plan_workspace_policy, raise_for_policy_violations
from app.runtime.schemas import (
    AgentCreateRequest,
    AgentDeleteResponse,
    AgentDeletionImpact,
    AgentLifecycleTransitionRequest,
    AgentSummaryResponse,
    BusinessAgentTemplatesResponse,
)
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_registry_store import AgentRegistryStore
from app.runtime.stores.feedback_store import FeedbackStore
from app.runtime.stores.improvement_store import ImprovementStore
from app.services.agent_governance import AgentGovernanceService
from app.services.business_agent_deletion import purge_business_agent_storage
from app.services.business_agent_provisioning import provision_business_agent

_PASSED_EVAL_RESULT_STATUSES = {"passed", "passed_with_notes"}

_IMPACT_COUNT_CAP = 1000


def _resolve_template_id(raw: str | None) -> str:
    """校验创建用 template_id（外部输入）；未知值投影为 422。"""
    template_id = (raw or DEFAULT_TEMPLATE_ID).strip() or DEFAULT_TEMPLATE_ID
    if template_id not in list_business_agent_templates():
        raise HTTPException(status_code=422, detail=f"Unknown template_id: {template_id!r}")
    return template_id


def _resolve_source_seed_id(raw: str | None, *, seed_root: Path) -> str | None:
    if raw is None:
        return None
    try:
        source_seed_id = validate_agent_id(raw)
    except InvalidAgentId as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if source_seed_id not in declared_business_agent_ids(seed_root=seed_root):
        raise HTTPException(status_code=422, detail=f"Unknown source_seed_id: {source_seed_id!r}")
    return source_seed_id


def _register_and_seed_agent(req: AgentCreateRequest, settings: AppSettings, store: AgentRegistryStore) -> AgentSummaryResponse:
    """Stage, validate, version, atomically install, then register a business Agent."""
    # seed 实例化源是运行态 catalog，不是仓库出生配置：已被在线删除的 seed 不应还能实例化。
    seed_root = runtime_seed_catalog_dir(settings.data_dir)
    source_seed_id = _resolve_source_seed_id(req.source_seed_id, seed_root=seed_root)
    template_id = _resolve_template_id(req.template_id) if source_seed_id is None else f"declared:{source_seed_id}"
    agent_id = (req.agent_id or "").strip() or f"biz-{uuid4().hex[:12]}"
    try:
        # 缺陷③：agent_id 直接作路径段，business_agent_layout 收敛了防穿越校验，非法 → 422。
        workspace_dir = business_agent_layout(settings.data_dir, agent_id).workspace
    except InvalidAgentId as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        plan = (
            prepare_declared_business_agent_workspace(
                source_agent_id=source_seed_id or agent_id,
                seed_root=seed_root,
            )
            if req.template_id is None
            else None
        )
        if source_seed_id is not None and plan is None:
            raise InvalidDeclaredBusinessAgentSeed(f"Declared workspace seed does not exist: {source_seed_id}")
        if plan is None:
            plan = prepare_business_agent_workspace(
                agent_id=agent_id,
                name=req.name,
                template_id=template_id,
            )
        record = provision_business_agent(
            store=store,
            agent_id=agent_id,
            name=req.name,
            workspace_dir=workspace_dir,
            template_id=template_id,
            plan=plan,
            validate_workspace=lambda workspace: raise_for_policy_violations(plan_workspace_policy(workspace=workspace, agent_id=agent_id).violations),
        )
        return _summary(record)
    except (InvalidDeclaredBusinessAgentSeed, WorkspaceSafetyError, ManagedAgentPolicyError) as exc:
        raise HTTPException(status_code=422, detail=f"Business agent template violates managed policy: {exc}") from exc


def _deletion_impact(
    agent_id: str,
    *,
    feedback_store: FeedbackStore,
    improvement_store: ImprovementStore,
    agent_governance: AgentGovernanceService,
) -> AgentDeletionImpact:
    """删除前的跨维度影响面提示，避免无声删除治理对象。

    这些治理记录是已发生事实，删除 Agent 不级联删除它们——只是把「你将失去对多少证据的入口」
    如实说出来。
    """

    return AgentDeletionImpact(
        runs=len(feedback_store.list_runs(agent_id=agent_id, limit=_IMPACT_COUNT_CAP)),
        feedback_signals=len(feedback_store.list_signals(agent_id=agent_id, limit=_IMPACT_COUNT_CAP)),
        improvements=len(improvement_store.list_improvements(agent_id=agent_id)),
        eval_runs=len(feedback_store.list_eval_runs(agent_id=agent_id, limit=_IMPACT_COUNT_CAP)),
        change_sets=len(agent_governance.list_change_sets(agent_id=agent_id, limit=_IMPACT_COUNT_CAP)),
        releases=len(agent_governance.list_releases(agent_id=agent_id, limit=_IMPACT_COUNT_CAP)),
    )


def _delete_agent_with_storage(
    agent_id: str,
    *,
    settings: AppSettings,
    agent_registry_store: AgentRegistryStore,
    feedback_store: FeedbackStore,
    improvement_store: ImprovementStore,
    agent_governance: AgentGovernanceService,
) -> AgentDeleteResponse:
    """删除注册身份并清理其运行态存储。

    事务与磁盘清理的先后是有意的：事务内只 tombstone，提交后才 rmtree。rmtree 不可回滚，
    放进事务块意味着事务回滚后磁盘已经回不来。
    """

    impact = _deletion_impact(
        agent_id,
        feedback_store=feedback_store,
        improvement_store=improvement_store,
        agent_governance=agent_governance,
    )
    # 与导入/导出/恢复共用同一把维护租约，因此删除与它们、与活跃 turn 天然互斥：租约获取本身
    # 就拒绝存在活跃 run 的 Agent，不会删掉正在被使用的 workspace。
    with agent_governance.version_maintenance.lease(agent_id=agent_id, kind="agent_delete", owner_id="api:agent-delete"):
        deleted = agent_registry_store.delete_business_agent(agent_id)  # 受保护→400，未知→404
    cleanup = purge_business_agent_storage(data_dir=settings.data_dir, agent_id=agent_id)
    # 缓存的版本 store 持有已被 rmtree 的 repository_dir；不失效会让同 id 重建命中悬空 store。
    agent_governance.evict_agent_store(agent_id)
    return AgentDeleteResponse(
        deleted=_summary(deleted),
        impact=impact,
        workspace_removed=cleanup.workspace_removed,
        seed_removed=cleanup.seed_removed,
        cleanup_complete=cleanup.cleanup_complete,
    )


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
        return BusinessAgentTemplatesResponse(
            templates=list_business_agent_templates(),
            seed_agent_ids=sorted(declared_business_agent_ids(seed_root=runtime_seed_catalog_dir(settings.data_dir))),
        )

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
        # 复用能力配置或修改配置后须评估通过才能激活，避免未验证配置直接上线。
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
        return _delete_agent_with_storage(
            agent_id,
            settings=settings,
            agent_registry_store=agent_registry_store,
            feedback_store=feedback_store,
            improvement_store=improvement_store,
            agent_governance=agent_governance,
        )

    return router
