from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import sessionmaker

from ..agent_profiles import MAIN_AGENT_PROFILE, AgentRuntimeProfile
from ..agent_registry_db import AgentRegistryModel
from ..errors import BusinessRuleViolation, ConflictError, NotFoundError
from ..runtime_db import utc_now


@dataclass(frozen=True)
class AgentRegistryRecord:
    """业务 Agent 的稳定身份记录（被治理对象的归属锚点）。"""

    agent_id: str
    name: str
    category: str
    workspace_dir: str
    created_at: str


class AgentRegistryStore:
    """业务 Agent 身份注册表存储（AGV-004/022 基座）。

    只登记业务 Agent（被治理对象）；治理 Agent（闭环执行者）不入注册表。
    `sync_business_agents` 幂等，可重复调用而不重复登记。
    """

    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def sync_business_agents(self, profiles: dict[str, AgentRuntimeProfile]) -> None:
        with self._session_factory.begin() as db:
            for profile in profiles.values():
                if profile.category != "business":
                    continue
                if db.get(AgentRegistryModel, profile.role) is not None:
                    continue
                db.add(
                    AgentRegistryModel(
                        agent_id=profile.role,
                        name=profile.name,
                        category=profile.category,
                        workspace_dir=str(profile.workspace_dir),
                        created_at=utc_now(),
                    )
                )

    def list_agents(self) -> list[AgentRegistryRecord]:
        with self._session_factory.begin() as db:
            rows = db.query(AgentRegistryModel).order_by(AgentRegistryModel.created_at, AgentRegistryModel.agent_id).all()
            return [_record(row) for row in rows]

    def get_agent(self, agent_id: str) -> AgentRegistryRecord | None:
        with self._session_factory.begin() as db:
            row = db.get(AgentRegistryModel, agent_id)
            return _record(row) if row is not None else None

    def create_business_agent(self, *, name: str, agent_id: str, workspace_dir: str) -> AgentRegistryRecord:
        """注册一个业务 Agent 身份（被治理对象）。重复 agent_id 拒绝，空 name 拒绝。"""
        clean_name = name.strip()
        if not clean_name:
            raise BusinessRuleViolation("Business agent name cannot be empty")
        created_at = utc_now()
        with self._session_factory.begin() as db:
            if db.get(AgentRegistryModel, agent_id) is not None:
                raise ConflictError(f"Business agent already exists: {agent_id}")
            db.add(
                AgentRegistryModel(
                    agent_id=agent_id,
                    name=clean_name,
                    category="business",
                    workspace_dir=workspace_dir,
                    created_at=created_at,
                )
            )
        return AgentRegistryRecord(
            agent_id=agent_id,
            name=clean_name,
            category="business",
            workspace_dir=workspace_dir,
            created_at=created_at,
        )

    def delete_business_agent(self, agent_id: str) -> AgentRegistryRecord:
        """删除一个注册业务 Agent；main-agent 样板不可删，未知 agent_id 报 404。

        删除前的影响面提示由路由层基于 agent_id 归属计数给出，避免无声删除治理对象。
        """
        if agent_id == MAIN_AGENT_PROFILE:
            raise BusinessRuleViolation("Main agent is the sample baseline and cannot be deleted")
        with self._session_factory.begin() as db:
            row = db.get(AgentRegistryModel, agent_id)
            if row is None:
                raise NotFoundError(f"Business agent not found: {agent_id}")
            record = _record(row)
            db.delete(row)
        return record


def _record(row: AgentRegistryModel) -> AgentRegistryRecord:
    return AgentRegistryRecord(
        agent_id=row.agent_id,
        name=row.name,
        category=row.category,
        workspace_dir=row.workspace_dir,
        created_at=row.created_at,
    )
