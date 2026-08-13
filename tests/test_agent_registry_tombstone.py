"""业务 Agent 注册表同步与生命周期投影；删除由 durable saga 专项覆盖。"""

from __future__ import annotations

from app.runtime.agent_profiles import build_business_agent_profile
from app.runtime.runtime_db import make_session_factory, runtime_db_path_from_data_dir
from app.runtime.stores.agent_registry_store import AgentRegistryStore
from tests.feedback_store_test_utils import _settings


def _store_and_profiles(tmp_path):
    settings = _settings(tmp_path)
    store = AgentRegistryStore(make_session_factory(runtime_db_path_from_data_dir(settings.data_dir)))
    profiles = {
        agent_id: build_business_agent_profile(settings, agent_id=agent_id, workspace_dir=settings.data_dir / "business-agents" / agent_id / "workspace")
        for agent_id in ("AAA", "BBB")
    }
    return store, profiles


def test_sync_does_not_persist_source_or_special_attributes(tmp_path) -> None:
    store, profiles = _store_and_profiles(tmp_path)
    store.sync_business_agents(profiles)
    agents = {a.agent_id: a for a in store.list_agents()}
    assert not hasattr(agents["AAA"], "origin")
    assert not hasattr(agents["AAA"], "builtin")
    assert not hasattr(agents["AAA"], "default")
    assert not hasattr(agents["AAA"], "protected")


def test_archived_status_not_reset_to_active_on_resync(tmp_path) -> None:
    """#26：删除前 archived 的治理意图不因 re-sync 被重置（sync 不动已存在行的 status）。"""
    store, profiles = _store_and_profiles(tmp_path)
    store.sync_business_agents(profiles)
    store.transition_business_agent("BBB", status="archived")
    store.sync_business_agents(profiles)  # 重启 re-sync
    assert store.get_agent("BBB").status == "archived"  # 不被重置为 active
