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
