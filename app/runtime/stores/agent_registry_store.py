from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import sessionmaker

from ..agent_profiles import MAIN_AGENT_PROFILE, AgentRuntimeProfile
from ..agent_registry_db import AgentRegistryModel
from ..errors import BusinessRuleViolation, ConflictError, NotFoundError
from ..runtime_db import utc_now
from ..state_machines import validate_transition


@dataclass(frozen=True)
class AgentRegistryRecord:
    """业务 Agent 的稳定身份记录（被治理对象的归属锚点）。"""

    agent_id: str
    name: str
    category: str
    workspace_dir: str
    created_at: str
    status: str = "active"
    origin: str = "user"  # #26：seed（声明式基线，禁删）vs user（用户创建，可 tombstone 删除）
    requires_web_hitl: bool = False  # 部署契约：其执行能力依赖 ENABLE_CLAUDE_WEB_HITL（agent.yaml 声明）


class AgentRegistryStore:
    """业务 Agent 身份注册表存储（AGV-004/022 基座）。

    只登记业务 Agent（被治理对象）；治理 Agent（闭环执行者）不入注册表。
    `sync_business_agents` 幂等，可重复调用而不重复登记。
    """

    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def sync_business_agents(self, profiles: dict[str, AgentRuntimeProfile], *, seed_agent_ids: frozenset[str] = frozenset()) -> None:
        with self._session_factory.begin() as db:
            for profile in profiles.values():
                if profile.category != "business":
                    continue
                # 业务 Agent（含预制 main-agent）以 profile.name 为身份；origin 以 seed 目录为准
                # 区分声明式基线 vs 用户创建（#26）。
                origin = "seed" if profile.name in seed_agent_ids else "user"
                existing = db.get(AgentRegistryModel, profile.name)
                if existing is not None:
                    # #26：用户已删除（tombstone）的 Agent 不因磁盘 workspace 仍在而被复活。
                    if existing.deleted_at:
                        continue
                    # ⑤：已存在记录若 workspace_dir 漂移（升级后路径迁移）同步更新；origin 以 seed 目录校正。
                    if existing.workspace_dir != str(profile.workspace_dir):
                        existing.workspace_dir = str(profile.workspace_dir)
                    existing.origin = origin
                    existing.requires_web_hitl = profile.requires_web_hitl  # 部署契约随 agent.yaml 校正
                    continue
                db.add(
                    AgentRegistryModel(
                        agent_id=profile.name,
                        name=profile.name,
                        category=profile.category,
                        workspace_dir=str(profile.workspace_dir),
                        created_at=utc_now(),
                        origin=origin,
                        requires_web_hitl=profile.requires_web_hitl,
                    )
                )

    def list_agents(self) -> list[AgentRegistryRecord]:
        with self._session_factory.begin() as db:
            rows = (
                db.query(AgentRegistryModel)
                .filter(AgentRegistryModel.deleted_at.is_(None))  # #26：过滤 tombstone（已删除）
                .order_by(AgentRegistryModel.created_at, AgentRegistryModel.agent_id)
                .all()
            )
            return [_record(row) for row in rows]

    def get_agent(self, agent_id: str) -> AgentRegistryRecord | None:
        with self._session_factory.begin() as db:
            row = db.get(AgentRegistryModel, agent_id)
            return _record(row) if row is not None and not row.deleted_at else None

    def create_business_agent(self, *, name: str, agent_id: str, workspace_dir: str) -> AgentRegistryRecord:
        """注册一个业务 Agent 身份（被治理对象）。活跃 agent_id 重复拒绝，空 name 拒绝。

        #26：若该 agent_id 是被 tombstone 删除的旧行（deleted_at 非空），允许复用——清 tombstone
        重置为新建的 user Agent，使删除后的 id 可重新创建（否则 id 永久不可用）。
        """
        clean_name = name.strip()
        if not clean_name:
            raise BusinessRuleViolation("Business agent name cannot be empty")
        created_at = utc_now()
        with self._session_factory.begin() as db:
            existing = db.get(AgentRegistryModel, agent_id)
            if existing is not None:
                if not existing.deleted_at:
                    raise ConflictError(f"Business agent already exists: {agent_id}")
                existing.deleted_at = None
                existing.name = clean_name
                existing.category = "business"
                existing.workspace_dir = workspace_dir
                existing.origin = "user"
                existing.status = "active"
                existing.created_at = created_at
                return _record(existing)
            db.add(
                AgentRegistryModel(
                    agent_id=agent_id,
                    name=clean_name,
                    category="business",
                    workspace_dir=workspace_dir,
                    created_at=created_at,
                    origin="user",  # #26：API 创建 = 用户来源（可 tombstone 删除）
                )
            )
        return AgentRegistryRecord(
            agent_id=agent_id,
            name=clean_name,
            category="business",
            workspace_dir=workspace_dir,
            created_at=created_at,
            origin="user",
        )

    def transition_business_agent(self, agent_id: str, *, status: str) -> AgentRegistryRecord:
        """业务 Agent 生命周期状态转移（AGV-020）。

        合法转移由 `agent_lifecycle` 状态机判定，非法转移抛 StateTransitionError（可理解错误）。
        main-agent 是样板基线，其生命周期固定为 active，不接受转移。
        """
        if agent_id == MAIN_AGENT_PROFILE:
            raise BusinessRuleViolation("Main agent lifecycle is fixed (sample baseline)")
        with self._session_factory.begin() as db:
            row = db.get(AgentRegistryModel, agent_id)
            if row is None:
                raise NotFoundError(f"Business agent not found: {agent_id}")
            validate_transition("agent_lifecycle", row.status or "active", status)
            row.status = status
            return _record(row)

    def delete_business_agent(self, agent_id: str) -> AgentRegistryRecord:
        """删除业务 Agent。main-agent 与 seed 声明式基线不可删（去 seed 源移除）；用户创建的 Agent 逻辑删除
        （tombstone：置 deleted_at），重启 discover_seeded 不复活。未知 / 已删除 agent_id 报 404。

        删除前的影响面提示由路由层基于 agent_id 归属计数给出，避免无声删除治理对象。
        """
        if agent_id == MAIN_AGENT_PROFILE:
            raise BusinessRuleViolation("Main agent is the sample baseline and cannot be deleted")
        with self._session_factory.begin() as db:
            row = db.get(AgentRegistryModel, agent_id)
            if row is None or row.deleted_at:
                raise NotFoundError(f"Business agent not found: {agent_id}")
            if (row.origin or "user") == "seed":
                raise BusinessRuleViolation(
                    f"Seed business agent '{agent_id}' is a declarative baseline and cannot be deleted; remove it from docker/runtime-volume-seeds instead"
                )
            record = _record(row)
            row.deleted_at = utc_now()  # #26：tombstone 逻辑删除，重启 discover 不复活
        return record


def _record(row: AgentRegistryModel) -> AgentRegistryRecord:
    return AgentRegistryRecord(
        agent_id=row.agent_id,
        name=row.name,
        category=row.category,
        workspace_dir=row.workspace_dir,
        created_at=row.created_at,
        status=row.status or "active",
        origin=row.origin or "user",
        requires_web_hitl=bool(row.requires_web_hitl),
    )
