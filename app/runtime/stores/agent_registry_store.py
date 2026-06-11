from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import sessionmaker

from ..agent_profiles import AgentRuntimeProfile
from ..agent_registry_db import AgentRegistryModel
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


def _record(row: AgentRegistryModel) -> AgentRegistryRecord:
    return AgentRegistryRecord(
        agent_id=row.agent_id,
        name=row.name,
        category=row.category,
        workspace_dir=row.workspace_dir,
        created_at=row.created_at,
    )
