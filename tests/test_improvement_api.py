"""四阶段改进治理 跨代重建：改进事项 ImprovementItem 的 /api/improvements API 验收。

覆盖：事项级单一领域实体端到端（创建→列表 scoping→详情→阶段转移）、非法转移 409、
未知 404、空字段 400、以及 backend-owned 字段所有权（hostile 输入不得越权覆盖）。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from test_api_execution_optimizer import _load_app


def test_improvement_item_single_source_lifecycle(monkeypatch, tmp_path: Path) -> None:
    """业务产物负责前推阶段，公开 lifecycle 只允许返工。"""
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

        # 通用 lifecycle 即使目标是相邻状态也不得前推。
        forward = client.post(f"/api/improvements/{improvement_id}/lifecycle", json={"stage": "triage"})
        assert forward.status_code == 409
        assert client.get(f"/api/improvements/{improvement_id}").json()["improvement_stage"] == "feedback_intake"

        # 业务产物成功后由后端推进：系统理解 -> triage，归因 -> attribution。
        assert (
            client.put(
                f"/api/improvements/{improvement_id}/normalized-feedback",
                json={"problem": "告警误报"},
            ).status_code
            == 200
        )
        assert client.get(f"/api/improvements/{improvement_id}").json()["improvement_stage"] == "triage"
        assert client.post(f"/api/improvements/{improvement_id}/normalized-feedback/confirm").status_code == 200
        assert (
            client.put(
                f"/api/improvements/{improvement_id}/attribution",
                json={"summary": "数据时间不一致", "responsibility_boundary": [], "evidence": []},
            ).status_code
            == 200
        )
        assert client.get(f"/api/improvements/{improvement_id}").json()["improvement_stage"] == "attribution"

        # lifecycle 保留合法返工 attribution -> triage。
        refined = client.post(f"/api/improvements/{improvement_id}/lifecycle", json={"stage": "triage"})
        assert refined.status_code == 200 and refined.json()["improvement_stage"] == "triage"

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
        assert client.post(
            "/api/improvements",
            json={"agent_id": "soc-ops", "title": "伪造归属", "source_feedback_refs": ["fbc-forged"]},
        ).status_code == 400
        assert client.get("/api/improvements/imp-unknown").status_code == 404
        assert client.post("/api/improvements/imp-unknown/lifecycle", json={"stage": "triage"}).status_code == 404


def test_archive_is_terminal_status_and_blocks_lifecycle(monkeypatch, tmp_path: Path) -> None:
    """归档为终态：事项关系与内容都不可再写，且失败写入不留下部分副作用。"""
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        created = client.post(
            "/api/improvements",
            json={"agent_id": "soc-ops", "title": "待归档事项", "source_feedback_refs": ["feedback-1"]},
        )
        improvement_id = created.json()["improvement_id"]
        assert client.put(
            f"/api/improvements/{improvement_id}/normalized-feedback",
            json={"problem": "归档前问题"},
        ).status_code == 200
        archived = client.post(f"/api/improvements/{improvement_id}/archive")
        assert archived.status_code == 200 and archived.json()["improvement_status"] == "archived"
        assert client.post(
            f"/api/improvements/{improvement_id}/lifecycle",
            json={"stage": "feedback_intake"},
        ).status_code == 409
        assert client.put(
            f"/api/improvements/{improvement_id}/normalized-feedback",
            json={"problem": "归档后污染"},
        ).status_code == 409
        assert client.post(f"/api/improvements/{improvement_id}/normalized-feedback/confirm").status_code == 409
        assert client.post(
            f"/api/improvements/{improvement_id}/feedbacks",
            json={"summary": "归档后反馈"},
        ).status_code == 409
        assert client.post(
            f"/api/improvements/{improvement_id}/split",
            json={"feedback_ref": "feedback-1"},
        ).status_code == 409
        normalized = client.get(f"/api/improvements/{improvement_id}/normalized-feedback").json()
        unchanged = client.get(f"/api/improvements/{improvement_id}").json()
        assert normalized["problem"] == "归档前问题" and normalized["status"] == "draft"
        assert unchanged["source_feedback_refs"] == ["feedback-1"]
        # 归档项仍可列出（审计）。
        assert improvement_id in {i["improvement_id"] for i in client.get("/api/improvements").json()}
        # 未知 id 归档 404。
        assert client.post("/api/improvements/imp-unknown/archive").status_code == 404


def test_merge_split_and_similar_api(monkeypatch, tmp_path: Path) -> None:
    """W2-b：相似 → 归并(同 Agent)→ 拆分；跨 Agent 归并 400、未知 404。"""
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        a = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "告警时间窗口不一致误报", "source_feedback_refs": ["f1"]}).json()
        b = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "告警时间窗口不一致重复反馈", "source_feedback_refs": ["f2"]}).json()
        # 相似列表（同 Agent，含 b）。
        similar = client.get(f"/api/improvements/{a['improvement_id']}/similar").json()
        assert any(s["improvement"]["improvement_id"] == b["improvement_id"] for s in similar)
        # 归并 b 进 a：a 拿到 f1+f2，b 归档。
        merged = client.post(f"/api/improvements/{a['improvement_id']}/merge", json={"source_improvement_id": b["improvement_id"]})
        assert merged.status_code == 200 and set(merged.json()["source_feedback_refs"]) == {"f1", "f2"}
        assert client.get(f"/api/improvements/{b['improvement_id']}").json()["improvement_status"] == "archived"
        # 拆分 f2 出来为新事项。
        split = client.post(f"/api/improvements/{a['improvement_id']}/split", json={"feedback_ref": "f2"})
        assert split.status_code == 201 and split.json()["source_feedback_refs"] == ["f2"]
        # 跨 Agent 归并被拒（400）。
        other = client.post("/api/improvements", json={"agent_id": "shop-bot", "title": "无关"}).json()
        assert client.post(f"/api/improvements/{a['improvement_id']}/merge", json={"source_improvement_id": other["improvement_id"]}).status_code == 400
        # 未知 merge 源 404。
        assert client.post(f"/api/improvements/{a['improvement_id']}/merge", json={"source_improvement_id": "imp-nope"}).status_code == 404


def test_auto_merge_on_create(monkeypatch, tmp_path: Path) -> None:
    """W2-b：auto_merge 创建时把来源反馈并入相似开放事项，而非新建。"""
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        base = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "数据时间窗口不可靠导致误判", "source_feedback_refs": ["fa"]}).json()
        merged = client.post(
            "/api/improvements",
            json={"agent_id": "soc-ops", "title": "数据时间窗口不可靠导致误判", "source_feedback_refs": ["fb"], "auto_merge": True},
        ).json()
        # 并入既有事项（同一 improvement_id），refs 合并，未新建。
        assert merged["improvement_id"] == base["improvement_id"]
        assert set(merged["source_feedback_refs"]) == {"fa", "fb"}
        assert len(client.get("/api/improvements", params={"agent_id": "soc-ops"}).json()) == 1


def test_closed_loop_links_api_is_read_only(monkeypatch, tmp_path: Path) -> None:
    """闭环链接由权威业务动作写入；公开 API 只读，不能注入任意或跨 Agent 引用。"""
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        item = client.post("/api/improvements", json={"agent_id": "soc-ops", "title": "关联闭环"}).json()
        iid = item["improvement_id"]
        module.improvement_store.add_link(iid, kind="attribution", ref_id="attr-authoritative")
        injected = client.post(
            f"/api/improvements/{iid}/links",
            json={"kind": "change_set", "ref_id": "foreign-or-missing-change-set"},
        )
        links = client.get(f"/api/improvements/{iid}/links").json()
        assert injected.status_code == 405
        assert {(link["kind"], link["ref_id"]) for link in links} == {("attribution", "attr-authoritative")}
        assert client.get("/api/improvements/imp-nope/links").status_code == 404


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
