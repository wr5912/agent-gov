"""AGV-004/022 基座：业务 Agent 身份注册表。

注册表只登记业务 Agent（被治理对象），治理 Agent（闭环执行者）不入表；
sync 幂等，作为运行/反馈/评估/版本治理的归属锚点。
"""

from __future__ import annotations

from pathlib import Path

from app.runtime.agent_profiles import build_profiles
from app.runtime.runtime_db import make_session_factory
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_registry_store import AgentRegistryStore


def _store(tmp_path: Path) -> tuple[AgentRegistryStore, dict]:
    factory = make_session_factory(tmp_path / "runtime.sqlite3")
    return AgentRegistryStore(factory), build_profiles(AppSettings())


def test_sync_registers_only_business_agents(tmp_path: Path) -> None:
    store, profiles = _store(tmp_path)
    store.sync_business_agents(profiles)

    agents = store.list_agents()
    assert [agent.agent_id for agent in agents] == ["main-agent"]
    assert agents[0].category == "business"
    # 治理 Agent 是闭环执行者，不作为被治理对象入注册表。
    assert store.get_agent("attribution-analyzer") is None


def test_sync_is_idempotent(tmp_path: Path) -> None:
    store, profiles = _store(tmp_path)
    store.sync_business_agents(profiles)
    store.sync_business_agents(profiles)  # 重复执行不得重复登记
    assert len(store.list_agents()) == 1


def test_get_agent_returns_stable_identity(tmp_path: Path) -> None:
    store, profiles = _store(tmp_path)
    store.sync_business_agents(profiles)

    record = store.get_agent("main-agent")
    assert record is not None
    assert record.name == "main-agent"
    assert record.workspace_dir  # 非空 workspace，作为归属锚点
    assert record.created_at
