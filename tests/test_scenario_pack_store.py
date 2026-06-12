"""AGV-026/027 场景包：按业务场景组织治理资产的一等对象（slice1 数据层）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from fastapi.testclient import TestClient

from app.runtime.errors import BusinessRuleViolation, NotFoundError
from app.runtime.runtime_db import make_session_factory
from app.runtime.stores.scenario_pack_store import ScenarioPackStore

from test_api_execution_optimizer import _load_app


def _store(tmp_path: Path) -> ScenarioPackStore:
    return ScenarioPackStore(make_session_factory(tmp_path / "runtime.sqlite3"))


def test_scenario_pack_api_create_list_get(monkeypatch, tmp_path: Path) -> None:
    """AGV-026：场景包可经 API 创建并查询（业务目标/适用范围/风险等级）。"""
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        created = client.post(
            "/api/scenario-packs",
            json={"name": "告警研判", "business_goal": "提升研判准确率", "scope": "SOC 告警", "risk_level": "high"},
        )
        assert created.status_code == 201
        pid = created.json()["scenario_pack_id"]
        assert created.json()["risk_level"] == "high"
        assert client.get(f"/api/scenario-packs/{pid}").json()["business_goal"] == "提升研判准确率"
        assert pid in {p["scenario_pack_id"] for p in client.get("/api/scenario-packs").json()}
        # 未知 404、非法风险等级 400。
        assert client.get("/api/scenario-packs/nope").status_code == 404
        assert client.post("/api/scenario-packs", json={"name": "x", "risk_level": "extreme"}).status_code == 400


def test_create_and_query_scenario_pack(tmp_path: Path) -> None:
    """AGV-026 criterion 1：场景包表达业务目标、适用范围和风险等级，并可查询。"""
    store = _store(tmp_path)
    pack = store.create_scenario_pack(
        name="告警研判", business_goal="提升研判准确率", scope="SOC 告警", risk_level="high"
    )
    assert pack.scenario_pack_id.startswith("pack-")
    assert pack.name == "告警研判"
    assert pack.business_goal == "提升研判准确率"
    assert pack.scope == "SOC 告警"
    assert pack.risk_level == "high"
    # 可查询详情与列表。
    got = store.get_scenario_pack(pack.scenario_pack_id)
    assert got.scenario_pack_id == pack.scenario_pack_id
    assert [p.scenario_pack_id for p in store.list_scenario_packs()] == [pack.scenario_pack_id]
    # 关联（agent/eval/asset）默认空，由 slice2 装配。
    assert got.agent_ids == [] and got.eval_case_ids == [] and got.asset_refs == []


def test_scenario_pack_validation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(BusinessRuleViolation):
        store.create_scenario_pack(name="  ")  # 空名拒绝
    with pytest.raises(BusinessRuleViolation):
        store.create_scenario_pack(name="x", risk_level="extreme")  # 非法风险等级拒绝
    with pytest.raises(NotFoundError):
        store.get_scenario_pack("nope")  # 未知 404
