"""/v1/conversations：create/list/get/delete + items（投影自 SDK transcript）、owning-agent 解析、
cursor 分页、保留 metadata 剥离、not-found 404。"""

from __future__ import annotations

from pathlib import Path

import app.routers.conversations as conv_module
from app.runtime.session_store import LocalSession
from fastapi.testclient import TestClient

from test_api_execution_optimizer import _load_app


def _register_biz(client: TestClient, agent_id: str = "soc-ops") -> None:
    assert client.post("/api/agent-registry", json={"name": "客服", "agent_id": agent_id}).status_code == 201


def _fake_history(captured: dict):
    def fn(*, sdk_session_id, workspace_dir, claude_config_dir, scrub, limit, offset):
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


def test_create_strips_reserved_metadata(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        body = client.post("/v1/conversations", json={"metadata": {"__agentgov_x__": "y", "source": "s"}}).json()
    assert body["metadata"] == {"source": "s"}


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


def test_items_project_transcript_via_owning_agent(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    captured: dict = {}
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


def test_items_cursor_maps_to_offset(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    captured: dict = {}
    monkeypatch.setattr(conv_module, "read_session_history", _fake_history(captured))
    module.session_store.save(LocalSession(session_id="sess-y", agent_id="soc-ops", sdk_session_id="sdk-2"))
    with TestClient(module.app) as client:
        _register_biz(client)
        client.get("/v1/conversations/conv_sess-y/items?after=msg_4&limit=10")
    assert captured["offset"] == 5 and captured["limit"] == 10  # cursor msg_4 -> 下一页 offset 5


def test_items_missing_owning_agent_404(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(conv_module, "read_session_history", _fake_history({}))
    # 有 transcript 但 owning agent 未注册 -> fail-loud 404（不静默）
    module.session_store.save(LocalSession(session_id="sess-z", agent_id="ghost-agent", sdk_session_id="sdk-3"))
    with TestClient(module.app) as client:
        assert client.get("/v1/conversations/conv_sess-z/items").status_code == 404
