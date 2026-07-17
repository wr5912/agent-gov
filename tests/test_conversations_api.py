"""/v1/conversations：create/list/get/delete + items（投影自 SDK transcript）、owning-agent 解析、
cursor 分页、保留 metadata 剥离、not-found 404。"""

from __future__ import annotations

from pathlib import Path

import app.routers.conversations as conv_module
from app.runtime.runtime_db import SessionRecordModel
from app.runtime.session_store import LocalSession
from fastapi.testclient import TestClient

from test_api_execution_optimizer import _load_app


def _register_biz(client: TestClient, agent_id: str = "soc-ops") -> None:
    assert client.post("/api/agent-registry", json={"name": "客服", "agent_id": agent_id}).status_code == 201


def _fake_history(captured: dict):
    async def fn(*, sdk_store, sdk_session_id, workspace_dir, scrub, limit, offset):
        captured["limit"] = limit
        captured["offset"] = offset
        return {
            "sdk_session_id": sdk_session_id,
            "title": "T",
            "messages": [
                {"role": "user", "blocks": [{"text": "hi"}], "parent_tool_use_id": None},
                {"role": "assistant", "blocks": [{"text": "yo"}]},
            ],
            "subagents": [],
        }

    return fn


def _skip_migration(monkeypatch) -> None:
    async def ready(session_store, session, **kwargs):
        return session, object()

    monkeypatch.setattr(conv_module, "committed_sdk_history_store", ready)


# ---------------------------------------------------------------- CRUD


def test_create_get_list_delete(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        created = client.post("/v1/conversations", json={"metadata": {"source": "playground"}}).json()
        cid = created["id"]
        assert cid.startswith("conv_") and created["object"] == "conversation"
        assert created["metadata"] == {"source": "playground"}

        got = client.get(f"/v1/conversations/{cid}").json()
        assert got["id"] == cid

        listing = client.get("/v1/conversations").json()
        assert listing["object"] == "list" and any(c["id"] == cid for c in listing["data"])

        deleted = client.delete(f"/v1/conversations/{cid}").json()
        assert deleted["deleted"] is True and deleted["object"] == "conversation.deleted"
        assert client.get(f"/v1/conversations/{cid}").status_code == 404


def test_active_turn_blocks_both_session_delete_surfaces(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    module.session_store.get_or_create_owned("sess-active-delete", agent_id="main-agent")
    with module.session_store.Session.begin() as db:
        session = db.get(SessionRecordModel, "sess-active-delete")
        assert session is not None
        session.active_run_id = "run-active-delete"
        session.active_run_expires_at = "2999-01-01T00:00:00+00:00"
        session.active_run_generation = 1

    with TestClient(module.app) as client:
        active = next(item for item in client.get("/v1/conversations").json()["data"] if item["id"] == "conv_sess-active-delete")
        assert active["agentgov"]["active_run_id"] == "run-active-delete"
        assert client.delete("/api/sessions/sess-active-delete").status_code == 409
        assert client.delete("/v1/conversations/conv_sess-active-delete").status_code == 409
        with module.session_store.Session.begin() as db:
            session = db.get(SessionRecordModel, "sess-active-delete")
            assert session is not None
            session.active_run_id = None
            session.active_run_expires_at = None
            session.active_run_generation = 0
        deleted = client.delete("/v1/conversations/conv_sess-active-delete")

    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True


def test_create_strips_reserved_metadata(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        body = client.post("/v1/conversations", json={"metadata": {"__agentgov_x__": "y", "source": "s"}}).json()
    assert body["metadata"] == {"source": "s"}


def test_single_api_key_authorizes_all_conversation_operations(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path, api_key="general-secret")
    general_headers = {"Authorization": "Bearer general-secret"}
    invalid_headers = {"Authorization": "Bearer retired-secret"}

    with TestClient(module.app) as client:
        assert client.post("/v1/conversations", json={}, headers=invalid_headers).status_code == 401
        created = client.post("/v1/conversations", json={}, headers=general_headers)
        conversation_id = created.json()["id"]
        assert created.status_code == 200
        assert client.get("/v1/conversations", headers=general_headers).status_code == 200
        assert client.get(f"/v1/conversations/{conversation_id}", headers=general_headers).status_code == 200
        assert client.get(f"/v1/conversations/{conversation_id}/items", headers=general_headers).status_code == 200
        assert client.delete(f"/v1/conversations/{conversation_id}", headers=general_headers).status_code == 200


def test_get_unknown_conversation_404(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        assert client.get("/v1/conversations/conv_ghost").status_code == 404
        assert client.get("/v1/conversations/conv_ghost/items").status_code == 404


# ---------------------------------------------------------------- items 投影


def test_items_empty_when_no_transcript(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        cid = client.post("/v1/conversations", json={}).json()["id"]  # 无 sdk_session_id
        body = client.get(f"/v1/conversations/{cid}/items").json()
    assert body["object"] == "list" and body["data"] == [] and body["has_more"] is False


def test_items_ownerless_transcript_is_session_conflict_409(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    module.session_store.save(LocalSession(session_id="sess-ownerless", sdk_session_id="sdk-ownerless", turns=1))
    with TestClient(module.app) as client:
        response = client.get("/v1/conversations/conv_sess-ownerless/items")

    assert response.status_code == 409
    assert response.json()["error_code"] == "SESSION_CONFLICT"


def test_items_project_transcript_via_owning_agent(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    captured: dict = {}
    _skip_migration(monkeypatch)
    monkeypatch.setattr(conv_module, "read_session_history", _fake_history(captured))
    module.session_store.save(LocalSession(session_id="sess-x", agent_id="soc-ops", sdk_session_id="sdk-1"))
    with TestClient(module.app) as client:
        _register_biz(client)  # owning agent 必须在 registry（否则 _resolve_owning_profile 404）
        body = client.get("/v1/conversations/conv_sess-x/items").json()
    items = body["data"]
    assert [i["id"] for i in items] == ["msg_0", "msg_1"]
    assert items[0]["role"] == "user" and items[0]["content"] == [{"text": "hi"}]
    assert items[1]["role"] == "assistant"
    assert body["first_id"] == "msg_0" and body["last_id"] == "msg_1"


def test_items_use_persisted_candidate_project_binding_after_worktree_cleanup(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    captured: dict = {}

    async def fake_history(*, sdk_store, sdk_session_id, workspace_dir, scrub, limit, offset):
        captured["project_key"] = sdk_store.binding.project_key
        captured["sdk_session_id"] = sdk_store.binding.sdk_session_id
        captured["workspace_dir"] = str(workspace_dir)
        return {"sdk_session_id": sdk_session_id, "title": "candidate", "messages": [], "subagents": []}

    monkeypatch.setattr(conv_module, "read_session_history", fake_history)
    module.session_store.save(
        LocalSession(
            session_id="eval-candidate",
            agent_id="main-agent",
            sdk_session_id="00000000-0000-4000-8000-000000000001",
            sdk_project_key="candidate-worktree-project-key",
            sdk_store_ready_at="2026-07-14T00:00:00+00:00",
        )
    )

    with TestClient(module.app) as client:
        response = client.get("/v1/conversations/conv_eval-candidate/items")

    assert response.status_code == 200
    assert captured == {
        "project_key": "candidate-worktree-project-key",
        "sdk_session_id": "00000000-0000-4000-8000-000000000001",
        "workspace_dir": str(module.settings.main_workspace_dir),
    }


def test_items_has_more_when_page_full(monkeypatch, tmp_path: Path) -> None:
    # 后端请求 limit+1 判定 has_more；mock 回满（received limit = client_limit+1 条）-> has_more=True、只返回 client_limit 项
    module = _load_app(monkeypatch, tmp_path)

    _skip_migration(monkeypatch)

    async def fake(*, sdk_store, sdk_session_id, workspace_dir, scrub, limit, offset):
        msgs = [{"role": "user", "blocks": [{"text": f"m{i}"}]} for i in range(limit)]
        return {"sdk_session_id": sdk_session_id, "title": "T", "messages": msgs, "subagents": []}

    monkeypatch.setattr(conv_module, "read_session_history", fake)
    module.session_store.save(LocalSession(session_id="sess-p", agent_id="soc-ops", sdk_session_id="sdk-p"))
    with TestClient(module.app) as client:
        _register_biz(client)
        body = client.get("/v1/conversations/conv_sess-p/items?limit=2").json()
    assert len(body["data"]) == 2 and body["has_more"] is True and body["last_id"] == "msg_1"


def test_items_cursor_maps_to_offset(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    captured: dict = {}
    _skip_migration(monkeypatch)
    monkeypatch.setattr(conv_module, "read_session_history", _fake_history(captured))
    module.session_store.save(LocalSession(session_id="sess-y", agent_id="soc-ops", sdk_session_id="sdk-2"))
    with TestClient(module.app) as client:
        _register_biz(client)
        client.get("/v1/conversations/conv_sess-y/items?after=msg_4&limit=10")
    # cursor msg_4 -> 下一页 offset 5；后端向 read_session_history 多取一条（limit+1=11）判定 has_more
    assert captured["offset"] == 5 and captured["limit"] == 11


def test_items_reject_invalid_cursor_and_unsupported_order(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    module.session_store.save(LocalSession(session_id="sess-invalid-page", agent_id="soc-ops", sdk_session_id="sdk-page"))
    with TestClient(module.app) as client:
        _register_biz(client)
        invalid_cursor = client.get("/v1/conversations/conv_sess-invalid-page/items?after=not-a-cursor")
        unsupported_order = client.get("/v1/conversations/conv_sess-invalid-page/items?order=desc")

    assert invalid_cursor.status_code == 422
    assert unsupported_order.status_code == 422


def test_items_missing_owning_agent_404(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(conv_module, "read_session_history", _fake_history({}))
    # 有 transcript 但 owning agent 未注册 -> fail-loud 404（不静默）
    module.session_store.save(LocalSession(session_id="sess-z", agent_id="ghost-agent", sdk_session_id="sdk-3"))
    with TestClient(module.app) as client:
        assert client.get("/v1/conversations/conv_sess-z/items").status_code == 404
