"""v2.7 P3：系统理解 NormalizedFeedback + 归因 Attribution 内容子资源（store + API）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.runtime.errors import BusinessRuleViolation
from app.runtime.runtime_db import make_session_factory
from app.runtime.stores.improvement_content_store import ImprovementContentStore
from app.runtime.stores.improvement_store import ImprovementStore
from test_api_execution_optimizer import _load_app


def _store(tmp_path: Path) -> ImprovementContentStore:
    return ImprovementContentStore(make_session_factory(tmp_path / "runtime.sqlite3"))


def test_reassign_feedback_and_delete_improvement_cascade(tmp_path: Path) -> None:
    """Part B：跨事项调整（reassign）移动反馈；删除事项级联删反馈/内容，A 不受影响。"""
    factory = make_session_factory(tmp_path / "runtime.sqlite3")
    items = ImprovementStore(factory)
    content = ImprovementContentStore(factory)
    a = items.create_improvement(agent_id="main-agent", title="事项A")
    b = items.create_improvement(agent_id="main-agent", title="事项B")
    fb = content.create_feedback(a.improvement_id, agent_id="main-agent", summary="反馈一")

    # reassign：把 A 的反馈移到 B（跨事项调整）。
    moved = content.reassign_feedback(fb.feedback_id, target_improvement_id=b.improvement_id)
    assert moved.improvement_id == b.improvement_id
    assert content.count_feedbacks(a.improvement_id) == 0  # A 被清空
    assert content.count_feedbacks(b.improvement_id) == 1
    # attachable：从 A 视角能看到 B 的反馈作为可调整来源。
    attachable = content.list_attachable_feedbacks(agent_id="main-agent", exclude_improvement_id=a.improvement_id)
    assert any(f.feedback_id == fb.feedback_id for f in attachable)

    # deletion_impact + 硬删除：删 B，其反馈随删；A 仍在。
    impact = items.deletion_impact(b.improvement_id)
    assert impact.feedbacks == 1
    items.delete_improvement(b.improvement_id)
    assert items.get_improvement(b.improvement_id) is None
    assert content.count_feedbacks(b.improvement_id) == 0
    assert items.get_improvement(a.improvement_id) is not None


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
        a = client.post(f"/api/improvements/{iid}/feedbacks", json={
            "summary": "这是误报",
            "source": "playground_run",
            "raw_text": "原文",
            "run_id": "run-1",
            "session_id": "session-1",
            "agent_version_id": "agent-v1",
            "scenario": "alert-triage",
            "task_id": "task-1",
            "alert_id": "alert-1",
            "case_id": "case-1",
        })
        assert a.status_code == 201 and a.json()["status"] == "merged" and a.json()["run_id"] == "run-1"
        assert a.json()["agent_version_id"] == "agent-v1"
        assert a.json()["scenario"] == "alert-triage"
        assert a.json()["task_id"] == "task-1"
        assert a.json()["alert_id"] == "alert-1"
        assert a.json()["case_id"] == "case-1"
        client.post(f"/api/improvements/{iid}/feedbacks", json={"summary": "MCP 数据像模拟", "source": "trace"})
        rows = client.get(f"/api/improvements/{iid}/feedbacks").json()
        assert {r["summary"] for r in rows} == {"这是误报", "MCP 数据像模拟"}
        assert {r["source"] for r in rows} == {"playground_run", "trace"}
        assert {r["agent_version_id"] for r in rows} == {"agent-v1", ""}
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


def test_backend_generates_initial_attribution_and_plan(monkeypatch, tmp_path: Path) -> None:
    """P2：归因/方案生成走后端治理端点，不由浏览器拼接后直接 upsert。"""
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        iid = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "告警误报治理"}).json()["improvement_id"]
        client.put(f"/api/improvements/{iid}/normalized-feedback", json={
            "problem": "告警误报",
            "possible_reason": "事件时间与告警时间窗口不一致",
            "possible_object": "sec-ops-data MCP 数据",
            "suggestion": "进入归因和回归保障",
            "user_quote": "这个横向移动告警其实是误报",
        })

        attr = client.post(f"/api/improvements/{iid}/attribution/generate")
        assert attr.status_code == 200
        assert "sec-ops-data MCP 数据" in attr.json()["summary"]
        assert attr.json()["evidence"] == ["用户反馈：这个横向移动告警其实是误报"]

        plan = client.post(f"/api/improvements/{iid}/optimization-plan/generate")
        assert plan.status_code == 200
        assert plan.json()["changes"][0]["target"] == "prompt"
        assert "告警误报治理" in plan.json()["summary"]


def test_regression_assessment_generate_get_confirm(monkeypatch, tmp_path: Path) -> None:
    """v2.7 §11/§17.5：回归保障评估 generate(治理 Agent，测试环境 heuristic 兜底)→get→confirm，未知事项 404。"""
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        iid = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "误报治理"}).json()["improvement_id"]
        client.put(f"/api/improvements/{iid}/normalized-feedback", json={"problem": "告警误报"})
        gen = client.post(f"/api/improvements/{iid}/regression-assessment/generate")
        assert gen.status_code == 200 and gen.json()["generated_by"] in {"governor", "heuristic"} and gen.json()["cases"]
        assert client.get(f"/api/improvements/{iid}/regression-assessment").json()["status"] == "draft"
        assert client.post(f"/api/improvements/{iid}/regression-assessment/confirm").json()["status"] == "confirmed"
        assert client.post("/api/improvements/imp-none/regression-assessment/generate").status_code == 404
        other = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "空"}).json()["improvement_id"]
        assert client.get(f"/api/improvements/{other}/regression-assessment").status_code == 404
