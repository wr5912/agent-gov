"""v2.7 P3：系统理解 NormalizedFeedback + 归因 Attribution 内容子资源（store + API）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.runtime.errors import BusinessRuleViolation
from app.runtime.runtime_db import make_session_factory
from app.runtime.stores.improvement_content_store import ImprovementContentStore
from test_api_execution_optimizer import _load_app


def _store(tmp_path: Path) -> ImprovementContentStore:
    return ImprovementContentStore(make_session_factory(tmp_path / "runtime.sqlite3"))


def test_normalized_feedback_upsert_is_1to1_and_confirmable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    a = store.upsert_normalized_feedback("imp-1", problem="告警误报", user_quote="这是误报")
    b = store.upsert_normalized_feedback("imp-1", problem="告警误报(改)", possible_reason="时间不一致")
    # 1:1：同一 improvement 复用同一行（id 不变）。
    assert a.normalized_feedback_id == b.normalized_feedback_id
    assert store.get_normalized_feedback("imp-1").problem == "告警误报(改)"
    assert store.get_normalized_feedback("imp-1").status == "draft"
    confirmed = store.set_normalized_feedback_status("imp-1", status="confirmed")
    assert confirmed.status == "confirmed"
    with pytest.raises(BusinessRuleViolation):
        store.set_normalized_feedback_status("imp-1", status="bogus")
    with pytest.raises(BusinessRuleViolation):
        store.set_normalized_feedback_status("imp-none", status="confirmed")


def test_attribution_upsert_and_confirm(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert_attribution("imp-1", summary="MCP 数据时间不一致", responsibility_boundary=["不是主 Agent 推理错误"], evidence=["list_events 时间窗口不一致"])
    got = store.get_attribution("imp-1")
    assert got.summary == "MCP 数据时间不一致" and got.responsibility_boundary == ["不是主 Agent 推理错误"]
    assert store.set_attribution_status("imp-1", status="confirmed").status == "confirmed"


def test_content_api_lifecycle(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        iid = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "告警误报治理"}).json()["improvement_id"]
        # 系统理解 upsert → get → confirm
        assert client.put(f"/api/improvements/{iid}/normalized-feedback", json={"problem": "告警误报", "possible_reason": "时间不一致"}).status_code == 200
        assert client.get(f"/api/improvements/{iid}/normalized-feedback").json()["problem"] == "告警误报"
        assert client.post(f"/api/improvements/{iid}/normalized-feedback/confirm").json()["status"] == "confirmed"
        # 归因 upsert → get
        attr = client.put(f"/api/improvements/{iid}/attribution", json={"summary": "MCP 数据问题", "responsibility_boundary": ["不是主 Agent"], "evidence": ["e1"]})
        assert attr.status_code == 200 and attr.json()["responsibility_boundary"] == ["不是主 Agent"]
        # 未知改进事项 404；无内容 get 404
        assert client.put("/api/improvements/imp-none/normalized-feedback", json={"problem": "x"}).status_code == 404
        other = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "无内容"}).json()["improvement_id"]
        assert client.get(f"/api/improvements/{other}/attribution").status_code == 404


def test_feedback_table_create_and_list(monkeypatch, tmp_path: Path) -> None:
    """v2.7 §8.4：来源反馈一等内容（摘要/来源/状态），1:多，未知事项 404。"""
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        iid = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "告警误报治理"}).json()["improvement_id"]
        a = client.post(f"/api/improvements/{iid}/feedbacks", json={"summary": "这是误报", "source": "playground_run", "raw_text": "原文", "run_id": "run-1"})
        assert a.status_code == 201 and a.json()["status"] == "merged" and a.json()["run_id"] == "run-1"
        client.post(f"/api/improvements/{iid}/feedbacks", json={"summary": "MCP 数据像模拟", "source": "trace"})
        rows = client.get(f"/api/improvements/{iid}/feedbacks").json()
        assert {r["summary"] for r in rows} == {"这是误报", "MCP 数据像模拟"}
        assert {r["source"] for r in rows} == {"playground_run", "trace"}
        assert client.post("/api/improvements/imp-none/feedbacks", json={"summary": "x"}).status_code == 404


def test_optimization_plan_and_execution(monkeypatch, tmp_path: Path) -> None:
    """v2.7 §106/§107：优化方案 + 执行记录 1:1 子资源，upsert→get→confirm，未知事项/无内容 404。"""
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        iid = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "误报治理"}).json()["improvement_id"]
        # 优化方案
        op = client.put(f"/api/improvements/{iid}/optimization-plan", json={"summary": "收紧时间一致性校验", "changes": [{"target": "prompt", "change": "新增时间校验指令"}]})
        assert op.status_code == 200 and op.json()["changes"][0]["target"] == "prompt" and op.json()["status"] == "draft"
        assert client.post(f"/api/improvements/{iid}/optimization-plan/confirm").json()["status"] == "confirmed"
        # 执行记录
        ex = client.put(f"/api/improvements/{iid}/execution", json={"summary": "已应用并生成版本", "changes_applied": ["prompt 更新"], "agent_version": "v1.2.0"})
        assert ex.status_code == 200 and ex.json()["agent_version"] == "v1.2.0"
        assert client.post(f"/api/improvements/{iid}/execution/confirm").json()["status"] == "confirmed"
        # 未知事项 / 无内容 404
        assert client.put("/api/improvements/imp-none/optimization-plan", json={"summary": "x"}).status_code == 404
        other = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "空"}).json()["improvement_id"]
        assert client.get(f"/api/improvements/{other}/execution").status_code == 404
