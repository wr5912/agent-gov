"""v2.7 跨代重建：改进事项 ImprovementItem 的 /api/improvements API 验收。

覆盖：事项级单一领域实体端到端（创建→列表 scoping→详情→阶段转移）、非法转移 409、
未知 404、空字段 400、以及 backend-owned 字段所有权（hostile 输入不得越权覆盖）。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from test_api_execution_optimizer import _load_app


def test_improvement_item_single_source_lifecycle(monkeypatch, tmp_path: Path) -> None:
    """主流程：改进事项作为事项级单一领域实体，创建后按 agent scoping 可列、可读、可推进阶段；非法转移被拒。"""
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        created = client.post(
            "/api/improvements",
            json={"agent_id": "soc-ops", "title": "告警误报治理", "summary": "事件时间不一致", "source_feedback_refs": ["fbs-1"]},
        )
        assert created.status_code == 201
        body = created.json()
        improvement_id = body["improvement_id"]
        assert improvement_id.startswith("imp-")
        assert body["agent_id"] == "soc-ops"
        assert body["improvement_stage"] == "feedback_intake"
        assert body["improvement_status"] == "active"
        assert body["source_feedback_refs"] == ["fbs-1"]

        # 列表按业务 Agent scoping。
        scoped = client.get("/api/improvements", params={"agent_id": "soc-ops"})
        assert scoped.status_code == 200
        assert improvement_id in {item["improvement_id"] for item in scoped.json()}

        # 详情可读。
        detail = client.get(f"/api/improvements/{improvement_id}")
        assert detail.status_code == 200 and detail.json()["improvement_id"] == improvement_id

        # 合法阶段推进 feedback_intake -> triage -> attribution。
        assert client.post(f"/api/improvements/{improvement_id}/lifecycle", json={"stage": "triage"}).status_code == 200
        advanced = client.post(f"/api/improvements/{improvement_id}/lifecycle", json={"stage": "attribution"})
        assert advanced.status_code == 200 and advanced.json()["improvement_stage"] == "attribution"

        # 非法跨段转移被状态机拒绝（409）。
        rejected = client.post(f"/api/improvements/{improvement_id}/lifecycle", json={"stage": "release"})
        assert rejected.status_code == 409
        assert "transition" in rejected.json()["detail"].lower()


def test_list_scoped_by_agent_and_global(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        a = client.post("/api/improvements", json={"agent_id": "agent-a", "title": "a"}).json()["improvement_id"]
        b = client.post("/api/improvements", json={"agent_id": "agent-b", "title": "b"}).json()["improvement_id"]
        only_a = {i["improvement_id"] for i in client.get("/api/improvements", params={"agent_id": "agent-a"}).json()}
        allitems = {i["improvement_id"] for i in client.get("/api/improvements").json()}
    assert only_a == {a}
    assert {a, b}.issubset(allitems)


def test_create_rejects_empty_and_unknown_is_404(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        assert client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "  "}).status_code == 400
        assert client.post("/api/improvements", json={"agent_id": "  ", "title": "x"}).status_code == 400
        assert client.get("/api/improvements/imp-unknown").status_code == 404
        assert client.post("/api/improvements/imp-unknown/lifecycle", json={"stage": "triage"}).status_code == 404


def test_archive_is_terminal_status_and_blocks_lifecycle(monkeypatch, tmp_path: Path) -> None:
    """归档为终态状态：improvement_status=archived；归档后阶段转移 409；未知 id 归档 404。"""
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        created = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "待归档事项"})
        improvement_id = created.json()["improvement_id"]
        archived = client.post(f"/api/improvements/{improvement_id}/archive")
        assert archived.status_code == 200 and archived.json()["improvement_status"] == "archived"
        # 归档后阶段推进被拒。
        assert client.post(f"/api/improvements/{improvement_id}/lifecycle", json={"stage": "triage"}).status_code == 409
        # 归档项仍可列出（审计）。
        assert improvement_id in {i["improvement_id"] for i in client.get("/api/improvements").json()}
        # 未知 id 归档 404。
        assert client.post("/api/improvements/imp-unknown/archive").status_code == 404


def test_create_ignores_hostile_backend_owned_fields(monkeypatch, tmp_path: Path) -> None:
    """字段所有权：请求体里夹带 backend-owned 字段不得越权——后端权威生成 id/stage/status。"""
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        created = client.post(
            "/api/improvements",
            json={
                "agent_id": "soc-ops",
                "title": "正常标题",
                "improvement_id": "hacked-id",
                "improvement_stage": "release",
                "improvement_status": "done",
                "created_at": "1999-01-01T00:00:00Z",
            },
        )
    assert created.status_code == 201
    body = created.json()
    # 后端权威字段未被污染。
    assert body["improvement_id"] != "hacked-id" and body["improvement_id"].startswith("imp-")
    assert body["improvement_stage"] == "feedback_intake"
    assert body["improvement_status"] == "active"
    assert body["created_at"] != "1999-01-01T00:00:00Z"
