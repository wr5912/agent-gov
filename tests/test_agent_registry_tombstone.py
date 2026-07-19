"""业务 Agent 删除：tombstone、独立保护名单与重启不复活。"""

from __future__ import annotations

import pytest
from app.runtime.agent_profiles import build_business_agent_profile
from app.runtime.errors import BusinessRuleViolation, NotFoundError
from app.runtime.protected_business_agents import SECURITY_OPERATIONS_EXPERT_AGENT_ID
from app.runtime.runtime_db import make_session_factory, runtime_db_path_from_data_dir
from app.runtime.stores.agent_registry_store import AgentRegistryStore
from tests.feedback_store_test_utils import _settings


def _store_and_profiles(tmp_path, *, extra_agent_ids: tuple[str, ...] = ()):
    settings = _settings(tmp_path)
    store = AgentRegistryStore(make_session_factory(runtime_db_path_from_data_dir(settings.data_dir)))
    profiles = {
        agent_id: build_business_agent_profile(settings, agent_id=agent_id, workspace_dir=settings.data_dir / "business-agents" / agent_id / "workspace")
        for agent_id in ("AAA", "BBB", *extra_agent_ids)
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


def test_ordinary_discovered_agent_can_be_deleted(tmp_path) -> None:
    store, profiles = _store_and_profiles(tmp_path)
    store.sync_business_agents(profiles)

    store.delete_business_agent("AAA")

    assert store.get_agent("AAA") is None


def test_protected_agent_cannot_be_deleted(tmp_path) -> None:
    """受保护 Agent 只能经仓库评审变更移除。"""

    store, profiles = _store_and_profiles(tmp_path, extra_agent_ids=(SECURITY_OPERATIONS_EXPERT_AGENT_ID,))
    store.sync_business_agents(profiles)

    with pytest.raises(BusinessRuleViolation):
        store.delete_business_agent(SECURITY_OPERATIONS_EXPERT_AGENT_ID)

    assert store.get_agent(SECURITY_OPERATIONS_EXPERT_AGENT_ID) is not None


def test_user_agent_tombstone_delete_and_no_resurrect_on_restart(tmp_path) -> None:
    store, profiles = _store_and_profiles(tmp_path)
    store.sync_business_agents(profiles)
    # 普通 Agent BBB：逻辑删除（tombstone）
    store.delete_business_agent("BBB")
    assert store.get_agent("BBB") is None  # get 过滤 tombstone
    assert "BBB" not in {a.agent_id for a in store.list_agents()}  # list 过滤 tombstone
    # 重启模拟：BBB 磁盘 workspace 仍在 → 再次 sync（discover 会再发现）→ tombstone 优先，不复活
    store.sync_business_agents(profiles)
    assert store.get_agent("BBB") is None
    # 重复删除已 tombstone 的 → 404
    with pytest.raises(NotFoundError):
        store.delete_business_agent("BBB")


def test_create_reuses_tombstoned_agent_id(tmp_path) -> None:
    """被 tombstone 删除的普通 Agent id 可重新创建，id 不永久不可用。"""
    store, profiles = _store_and_profiles(tmp_path)
    store.sync_business_agents(profiles)
    store.delete_business_agent("BBB")
    assert store.get_agent("BBB") is None  # tombstone
    rec = store.create_business_agent(name="BBB新建", agent_id="BBB", workspace_dir="/ws/bbb-new")
    assert rec.agent_id == "BBB" and rec.status == "active"
    again = store.get_agent("BBB")
    assert again is not None and again.name == "BBB新建"  # 不再是 tombstone
    # 活跃 id 重复仍拒绝
    with pytest.raises(__import__("app.runtime.errors", fromlist=["ConflictError"]).ConflictError):
        store.create_business_agent(name="重复", agent_id="BBB", workspace_dir="/ws/dup")


def test_archived_status_not_reset_to_active_on_resync(tmp_path) -> None:
    """#26：删除前 archived 的治理意图不因 re-sync 被重置（sync 不动已存在行的 status）。"""
    store, profiles = _store_and_profiles(tmp_path)
    store.sync_business_agents(profiles)
    store.transition_business_agent("BBB", status="archived")
    store.sync_business_agents(profiles)  # 重启 re-sync
    assert store.get_agent("BBB").status == "archived"  # 不被重置为 active
