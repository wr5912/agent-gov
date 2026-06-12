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


def test_scenario_pack_associate_and_copy(monkeypatch, tmp_path: Path) -> None:
    """AGV-026 criterion 2/3：资产可关联（Agent 装配能力）、可复制为模板，关联可审计。"""
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        pid = client.post("/api/scenario-packs", json={"name": "告警研判", "risk_level": "high"}).json()["scenario_pack_id"]
        # 关联资产与 Agent（Agent 据此装配场景包能力）。
        assoc = client.post(
            f"/api/scenario-packs/{pid}/assets",
            json={"agent_ids": ["soc-ops"], "eval_case_ids": ["ec-1"], "asset_refs": ["prompts/triage.md"]},
        )
        assert assoc.status_code == 200
        assert assoc.json()["agent_ids"] == ["soc-ops"]
        assert assoc.json()["eval_case_ids"] == ["ec-1"]
        # 再次关联去重并集。
        assert client.post(f"/api/scenario-packs/{pid}/assets", json={"agent_ids": ["soc-ops", "biz-2"]}).json()["agent_ids"] == [
            "soc-ops",
            "biz-2",
        ]
        # 复制为模板（资产可迁移/复制），不复制 agent_ids（各 Agent 另行装配、保留审计边界）。
        copied = client.post(f"/api/scenario-packs/{pid}/copy", json={"name": "告警研判副本"})
        assert copied.status_code == 201
        cbody = copied.json()
        assert cbody["scenario_pack_id"] != pid
        assert cbody["name"] == "告警研判副本"
        assert cbody["risk_level"] == "high"
        assert cbody["eval_case_ids"] == ["ec-1"]
        assert cbody["asset_refs"] == ["prompts/triage.md"]
        assert cbody["agent_ids"] == []
        # 未知 pack 关联/复制 -> 404。
        assert client.post("/api/scenario-packs/nope/assets", json={"agent_ids": ["x"]}).status_code == 404
        assert client.post("/api/scenario-packs/nope/copy", json={"name": "x"}).status_code == 404


def test_scenario_pack_dedup_detect_and_merge(monkeypatch, tmp_path: Path) -> None:
    """AGV-023：重复场景包可检测、可合并并入主资产、引用经 merged_into 保留、可审计。"""
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        p1 = client.post("/api/scenario-packs", json={"name": "告警研判"}).json()["scenario_pack_id"]
        p2 = client.post("/api/scenario-packs", json={"name": " 告警研判 "}).json()["scenario_pack_id"]  # 规范化后同名
        client.post(f"/api/scenario-packs/{p1}/assets", json={"eval_case_ids": ["ec-1"]})
        client.post(f"/api/scenario-packs/{p2}/assets", json={"eval_case_ids": ["ec-2"], "agent_ids": ["soc-ops"]})

        # 检测重复 + 治理建议（主资产）。
        dups = client.get("/api/scenario-packs/duplicates").json()
        assert len(dups) == 1
        assert set(dups[0]["scenario_pack_ids"]) == {p1, p2}
        primary = dups[0]["suggested_primary_id"]
        secondary = p2 if primary == p1 else p1

        # 合并：主资产并入重复包关联，引用不丢失。
        merged = client.post(f"/api/scenario-packs/{primary}/merge", json={"duplicate_ids": [secondary]})
        assert merged.status_code == 200
        assert set(merged.json()["eval_case_ids"]) == {"ec-1", "ec-2"}
        assert "soc-ops" in merged.json()["agent_ids"]
        # 重复包保留可审计且指向主资产（引用重定向，不物理删除）。
        sec = client.get(f"/api/scenario-packs/{secondary}").json()
        assert sec["merged_into"] == primary
        # 已合并不再被检测为重复。
        assert client.get("/api/scenario-packs/duplicates").json() == []
        # 非法合并（无 duplicate）400，未知主资产 404。
        assert client.post(f"/api/scenario-packs/{primary}/merge", json={"duplicate_ids": []}).status_code == 400
        assert client.post("/api/scenario-packs/nope/merge", json={"duplicate_ids": [secondary]}).status_code == 404


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
