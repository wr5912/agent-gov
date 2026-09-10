from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends

from app.agent_testing.schedule import AgentTestScheduleService
from app.agent_testing.store import AgentTestingStore
from app.runtime.agent_git_store import AgentGitError
from app.runtime.agent_governance_schemas import AgentPresentationResponse
from app.runtime.agent_governance_schemas import agent_summary_response as _summary
from app.runtime.errors import ConflictError, NotFoundError
from app.runtime.schemas import (
    AgentDeleteResponse,
    AgentDeletionImpact,
    AgentLifecycleTransitionRequest,
    AgentSummaryResponse,
)
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_registry_store import AgentRegistryRecord, AgentRegistryStore
from app.runtime.stores.feedback_store import FeedbackStore
from app.runtime.stores.improvement_store import ImprovementStore
from app.runtime_gateway.provisioning import RuntimeAgentProvisioner
from app.runtime_gateway.store import RuntimeStoreError
from app.services.agent_governance import AgentGovernanceService
from app.services.business_agent_presentation import business_agent_presentation
from app.services.runtime_agent_deletion import RuntimeAgentDeletionService

_IMPACT_COUNT_CAP = 1000


def _has_passed_test(agent_id: str, store: AgentTestingStore) -> bool:
    return any(str(run.get("status")) == "passed" for run in store.list_runs(agent_id=agent_id, limit=_IMPACT_COUNT_CAP))


def _runtime_summary(
    record: AgentRegistryRecord,
    provisioner: RuntimeAgentProvisioner | None,
) -> AgentSummaryResponse:
    if provisioner is None:
        return _summary(record)
    try:
        current = provisioner.inspect_current(record.agent_id)
    except (AgentGitError, RuntimeStoreError):
        # Registry list remains readable before explicit provision creates the
        # immutable Runtime binding.
        return _summary(record)
    return _summary(
        record,
        agent_version_id=current.agent_version_id,
        harness_digest=current.harness_digest,
        runtime_agent_id=current.runtime_agent_id,
    )


def _deletion_impact(
    agent_id: str,
    *,
    feedback_store: FeedbackStore,
    improvement_store: ImprovementStore,
    agent_governance: AgentGovernanceService,
    agent_testing_store: AgentTestingStore,
) -> AgentDeletionImpact:
    """删除前的跨维度影响面提示，避免无声删除治理对象。

    这些治理记录是已发生事实，删除 Agent 不级联删除它们——只是把「你将失去对多少证据的入口」
    如实说出来。
    """

    return AgentDeletionImpact(
        runs=len(feedback_store.list_runs(agent_id=agent_id, limit=_IMPACT_COUNT_CAP)),
        feedback_signals=len(feedback_store.list_signals(agent_id=agent_id, limit=_IMPACT_COUNT_CAP)),
        improvements=len(improvement_store.list_improvements(agent_id=agent_id)),
        test_runs=len(agent_testing_store.list_runs(agent_id=agent_id, limit=_IMPACT_COUNT_CAP)),
        change_sets=len(agent_governance.list_change_sets(agent_id=agent_id, limit=_IMPACT_COUNT_CAP)),
        releases=len(agent_governance.list_releases(agent_id=agent_id, limit=_IMPACT_COUNT_CAP)),
    )


async def _delete_agent_with_storage(
    agent_id: str,
    *,
    settings: AppSettings,
    agent_registry_store: AgentRegistryStore,
    feedback_store: FeedbackStore,
    improvement_store: ImprovementStore,
    agent_governance: AgentGovernanceService,
    agent_testing_store: AgentTestingStore,
    runtime_deletion: RuntimeAgentDeletionService,
    agent_test_schedule_service: AgentTestScheduleService | None = None,
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
        agent_testing_store=agent_testing_store,
    )
    del settings
    deleted = agent_registry_store.get_agent(agent_id)
    if deleted is None:
        from app.runtime.errors import NotFoundError

        raise NotFoundError(f"Business agent not found: {agent_id}")
    # 与导入/导出/恢复共用同一把维护租约；intent 在任何远端副作用前落库，部分成功可由
    # startup/periodic recovery 用同一组定位信息继续，而不会先丢 Session/版本证据。
    with agent_governance.version_maintenance.lease(
        agent_id=agent_id,
        kind="agent_delete",
        owner_id="api:agent-delete",
    ) as lease:
        intent = runtime_deletion.start(deleted)
        cleanup = await runtime_deletion.resume(
            intent.intent_id,
            assert_maintenance_active=lease.assert_active,
        )
        if agent_registry_store.get_agent(agent_id) is None and agent_test_schedule_service is not None:
            agent_test_schedule_service.disable_agent_schedule(agent_id)
    return AgentDeleteResponse(
        deleted=_summary(deleted),
        impact=impact,
        workspace_removed=cleanup.workspace_removed,
        cleanup_complete=cleanup.cleanup_complete,
    )


def create_agents_router(
    *,
    settings: AppSettings,
    agent_registry_store: AgentRegistryStore,
    feedback_store: FeedbackStore,
    improvement_store: ImprovementStore,
    agent_governance: AgentGovernanceService,
    agent_testing_store: AgentTestingStore,
    runtime_deletion: RuntimeAgentDeletionService,
    agent_test_schedule_service: AgentTestScheduleService | None = None,
    require_api_key: Callable,
    runtime_provisioner: RuntimeAgentProvisioner | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["agents"], dependencies=[Depends(require_api_key)])

    @router.get(
        "/agent-registry",
        response_model=list[AgentSummaryResponse],
        summary="List registered business agents (governance objects)",
    )
    def list_agents() -> list[AgentSummaryResponse]:
        return [_runtime_summary(record, runtime_provisioner) for record in agent_registry_store.list_agents()]

    @router.get(
        "/agent-registry/{agent_id}/presentation",
        response_model=AgentPresentationResponse,
        summary="Read structured Welcome Card content for a registered business agent",
    )
    async def get_agent_presentation(agent_id: str) -> AgentPresentationResponse:
        record = agent_registry_store.get_agent(agent_id)
        if record is None:
            raise NotFoundError(f"Business agent not found: {agent_id}")
        return business_agent_presentation(record)

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
            if (
                current is not None
                and current.status == "evaluating"
                and not _has_passed_test(
                    agent_id,
                    agent_testing_store,
                )
            ):
                raise ConflictError(f"Agent {agent_id} cannot enter active from evaluating without a passed platform test run")
        transitioned = agent_registry_store.transition_business_agent(agent_id, status=req.status)
        if transitioned.status == "archived" and agent_test_schedule_service is not None:
            agent_test_schedule_service.disable_agent_schedule(agent_id)
        return _runtime_summary(transitioned, runtime_provisioner)

    @router.delete(
        "/agent-registry/{agent_id}",
        response_model=AgentDeleteResponse,
        summary="Delete a business agent and report its governance impact",
    )
    async def delete_agent(agent_id: str) -> AgentDeleteResponse:
        return await _delete_agent_with_storage(
            agent_id,
            settings=settings,
            agent_registry_store=agent_registry_store,
            feedback_store=feedback_store,
            improvement_store=improvement_store,
            agent_governance=agent_governance,
            agent_testing_store=agent_testing_store,
            runtime_deletion=runtime_deletion,
            agent_test_schedule_service=agent_test_schedule_service,
        )

    return router
