"""v2.7 W3：治理资产 Registry 复利中心 store 单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.runtime.errors import BusinessRuleViolation, NotFoundError
from app.runtime.runtime_db import make_session_factory
from app.runtime.stores.asset_store import AssetStore


def _store(tmp_path: Path) -> AssetStore:
    return AssetStore(make_session_factory(tmp_path / "runtime.sqlite3"))


def test_create_list_get(tmp_path: Path) -> None:
    store = _store(tmp_path)
    a = store.create_asset(agent_id="soc", asset_type="methodology", title="误报归因法", body="步骤...", source_improvement_id="imp-1")
    store.create_asset(agent_id="soc", asset_type="regression", title="时间窗口回归集")
    store.create_asset(agent_id="shop", asset_type="methodology", title="退款方法论")
    assert {x.asset_id for x in store.list_assets(agent_id="soc")} == {a.asset_id, *[x.asset_id for x in store.list_assets(agent_id="soc", asset_type="regression")]}
    methods = store.list_assets(agent_id="soc", asset_type="methodology")
    assert [x.asset_id for x in methods] == [a.asset_id]
    assert store.get_asset(a.asset_id).source_improvement_id == "imp-1"


def test_create_rejects_bad_input(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(BusinessRuleViolation):
        store.create_asset(agent_id="", asset_type="methodology", title="x")
    with pytest.raises(BusinessRuleViolation):
        store.create_asset(agent_id="soc", asset_type="bogus", title="x")
    with pytest.raises(BusinessRuleViolation):
        store.create_asset(agent_id="soc", asset_type="methodology", title="  ")


def test_inherit_compounds_to_target_agent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    src = store.create_asset(agent_id="soc", asset_type="methodology", title="误报归因法", body="正文")
    inherited = store.inherit_asset(src.asset_id, target_agent_id="shop")
    assert inherited.agent_id == "shop"
    assert inherited.asset_id != src.asset_id
    assert inherited.inherited_from == src.asset_id
    assert inherited.title == src.title and inherited.body == src.body and inherited.asset_type == src.asset_type
    # 同 Agent 继承被拒；未知资产 404。
    with pytest.raises(BusinessRuleViolation):
        store.inherit_asset(src.asset_id, target_agent_id="soc")
    with pytest.raises(NotFoundError):
        store.inherit_asset("ast-nope", target_agent_id="shop")


def test_list_by_source_improvement_id(tmp_path: Path) -> None:
    """v2.7 §11.2：按沉淀来源改进事项过滤资产。"""
    store = _store(tmp_path)
    store.create_asset(agent_id="soc", asset_type="regression", title="回归A", source_improvement_id="imp-1")
    store.create_asset(agent_id="soc", asset_type="methodology", title="方法B", source_improvement_id="imp-1")
    store.create_asset(agent_id="soc", asset_type="regression", title="回归C", source_improvement_id="imp-2")
    only1 = store.list_assets(source_improvement_id="imp-1")
    assert {a.title for a in only1} == {"回归A", "方法B"}
    assert [a.title for a in store.list_assets(source_improvement_id="imp-2")] == ["回归C"]
