"""GET /api/sessions/{id}/messages：SDK session 投影端点 + session_history 适配器单测。

适配器把 Claude Code agent 自己的 session transcript（经 claude-agent-sdk session API 读取）投影成
API 契约，不另存副本。单测覆盖：role 来自 SessionMessage.type、blocks 原样透传、脱敏开关、hostile
输入不崩不污染 role、config-dir 还原；端点覆盖 404 / 空态 / 投影 / 401 越权。
"""

from __future__ import annotations

import sys
from pathlib import Path

import app.routers.sessions as sessions_mod
import pytest
from app.runtime.errors import NotFoundError, SessionConflictError
from app.runtime.session_history import _scrub_message, normalize_message, read_session_history
from app.runtime.session_store import LocalSession
from fastapi.testclient import TestClient

from test_api_execution_optimizer import _load_app


class _FakeMsg:
    def __init__(self, type_: str, content, uuid: str = "u", parent=None) -> None:
        self.type = type_
        self.uuid = uuid
        self.parent_tool_use_id = parent
        self.message = {"role": None, "content": content}


class _FakeInfo:
    def __init__(self, custom_title=None, summary=None) -> None:
        self.custom_title = custom_title
        self.summary = summary


# ---------------------------------------------------------------- adapter unit

def test_normalize_message_role_from_type_and_blocks_passthrough() -> None:
    msg = _FakeMsg(
        "assistant",
        [
            {"type": "text", "text": "hi"},
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"p": 1}},
            {"type": "tool_result", "tool_use_id": "t1", "content": "r", "is_error": False},
        ],
        uuid="u1",
        parent="pt",
    )
    out = normalize_message(msg)
    assert out["role"] == "assistant" and out["uuid"] == "u1" and out["parent_tool_use_id"] == "pt"
    assert [b["type"] for b in out["blocks"]] == ["text", "tool_use", "tool_result"]
    # tool_use.id <-> tool_result.tool_use_id 配对保留
    blocks = {b["type"]: b for b in out["blocks"]}
    assert blocks["tool_use"]["id"] == blocks["tool_result"]["tool_use_id"]


def test_normalize_message_hostile_inputs_do_not_crash_or_pollute_role() -> None:
    class _NoneMessage:
        type, uuid, parent_tool_use_id, message = "user", "w", None, None

    assert normalize_message(_NoneMessage())["blocks"] == []

    class _IntContent:
        type, uuid, parent_tool_use_id, message = "assistant", "i", None, {"content": 123}

    assert normalize_message(_IntContent())["blocks"] == []

    # backend-owned 字段污染：块内伪造 role/session_id 不得改写投影的 role（role 只来自 SessionMessage.type）
    polluted = _FakeMsg("user", [{"type": "text", "text": "x", "role": "system", "session_id": "evil"}])
    assert normalize_message(polluted)["role"] == "user"


def test_scrub_toggle_redacts_content_keeps_structure() -> None:
    norm = normalize_message(
        _FakeMsg(
            "assistant",
            [
                {"type": "text", "text": "secret"},
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {"k": "v"}},
                {"type": "tool_result", "tool_use_id": "t1", "content": "leak", "is_error": False},
            ],
        )
    )
    scrubbed = {b["type"]: b for b in _scrub_message(norm)["blocks"]}
    assert scrubbed["text"]["text"] == "[redacted]"
    assert scrubbed["tool_use"]["input"] == "[redacted]" and scrubbed["tool_use"]["id"] == "t1"
    assert scrubbed["tool_result"]["content"] == "[redacted]"
    assert scrubbed["tool_result"]["tool_use_id"] == "t1" and scrubbed["tool_result"]["is_error"] is False


def test_read_session_history_projects_via_sdk_and_restores_env(monkeypatch) -> None:
    import os

    seen: dict[str, object] = {}

    def fake_messages(sid, directory=None, limit=None, offset=0):
        seen["cfg"] = os.environ.get("CLAUDE_CONFIG_DIR")
        seen["dir"] = directory
        seen["limit"] = limit
        return [_FakeMsg("user", [{"type": "text", "text": "q"}]), _FakeMsg("assistant", [{"type": "text", "text": "a"}])]

    fake_sdk = type(
        "FakeSdk",
        (),
        {
            "get_session_info": staticmethod(lambda sid, directory=None: _FakeInfo(summary="标题")),
            "get_session_messages": staticmethod(fake_messages),
            "list_subagents": staticmethod(lambda sid, directory=None: ["agent-x"]),
            "get_subagent_messages": staticmethod(lambda sid, aid, directory=None: [_FakeMsg("assistant", [{"type": "text", "text": "sub"}])]),
        },
    )
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    out = read_session_history(sdk_session_id="sid", workspace_dir="/main-workspace", claude_config_dir="/cfg/.claude", limit=10)
    assert out["sdk_session_id"] == "sid" and out["title"] == "标题"
    assert [m["role"] for m in out["messages"]] == ["user", "assistant"]
    assert out["subagents"][0]["agent_id"] == "agent-x"
    assert out["subagents"][0]["messages"][0]["blocks"][0]["text"] == "sub"
    # 读取期临时把 CLAUDE_CONFIG_DIR 指向该 profile，读完还原（不泄漏给并发子进程）
    assert seen["cfg"] == "/cfg/.claude" and seen["dir"] == "/main-workspace" and seen["limit"] == 10
    assert os.environ.get("CLAUDE_CONFIG_DIR") is None


def test_read_session_history_scrub_applies_to_subagents(monkeypatch) -> None:
    fake_sdk = type(
        "FakeSdk",
        (),
        {
            "get_session_info": staticmethod(lambda sid, directory=None: None),
            "get_session_messages": staticmethod(lambda sid, directory=None, limit=None, offset=0: [_FakeMsg("user", [{"type": "text", "text": "main"}])]),
            "list_subagents": staticmethod(lambda sid, directory=None: ["agent-x"]),
            "get_subagent_messages": staticmethod(lambda sid, aid, directory=None: [_FakeMsg("assistant", [{"type": "text", "text": "subsecret"}])]),
        },
    )
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)
    out = read_session_history(sdk_session_id="sid", workspace_dir="/w", claude_config_dir="/c", scrub=True)
    assert out["messages"][0]["blocks"][0]["text"] == "[redacted]"
    assert out["subagents"][0]["messages"][0]["blocks"][0]["text"] == "[redacted]"


# ------------------------------------------------------------ endpoint wiring

def test_endpoint_unknown_session_404(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        assert client.get("/api/sessions/does-not-exist/messages").status_code == 404


def test_endpoint_session_without_transcript_returns_empty(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    module.session_store.save(LocalSession(session_id="s-empty", sdk_session_id=None, title="新会话"))
    with TestClient(module.app) as client:
        resp = client.get("/api/sessions/s-empty/messages")
        assert resp.status_code == 200
        body = resp.json()
        assert body["session_id"] == "s-empty" and body["sdk_session_id"] is None
        assert body["messages"] == [] and body["subagents"] == []


def test_endpoint_projects_history(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    module.session_store.save(LocalSession(session_id="s1", sdk_session_id="sdk-1", agent_id="main-agent", title="t"))

    def fake_read(*, sdk_session_id, workspace_dir, claude_config_dir, scrub, limit, offset):
        return {
            "sdk_session_id": sdk_session_id,
            "title": "t",
            "messages": [{"uuid": "u", "role": "user", "parent_tool_use_id": None, "blocks": [{"type": "text", "text": "hi"}]}],
            "subagents": [{"agent_id": "agent-x", "messages": []}],
        }

    monkeypatch.setattr(sessions_mod, "read_session_history", fake_read)
    with TestClient(module.app) as client:
        resp = client.get("/api/sessions/s1/messages")
        assert resp.status_code == 200
        body = resp.json()
        assert body["sdk_session_id"] == "sdk-1"
        assert body["messages"][0]["role"] == "user"
        assert body["subagents"][0]["agent_id"] == "agent-x"


def test_endpoint_requires_auth_401(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path, api_key="secret")
    module.session_store.save(LocalSession(session_id="s2", sdk_session_id=None))
    with TestClient(module.app) as client:
        assert client.get("/api/sessions/s2/messages").status_code == 401
        assert client.get("/api/sessions/s2/messages", headers={"Authorization": "Bearer secret"}).status_code == 200


# -------------------------------------------------- owning-agent 强校验（无静默兜底）

class _StubSettings:
    data_dir = Path("/data")
    main_workspace_dir = Path("/main-workspace")
    main_claude_root = Path("/claude-roots/main")


class _FakeRecord:
    def __init__(self, workspace_dir: str) -> None:
        self.workspace_dir = workspace_dir


class _FakeRegistry:
    def __init__(self, known: dict[str, str]) -> None:
        self._known = dict(known)

    def get_agent(self, agent_id: str):
        ws = self._known.get(agent_id)
        return _FakeRecord(ws) if ws is not None else None


def test_resolve_owning_profile_main_and_registered_business() -> None:
    # main-agent 已归一为预制业务 Agent：与其它业务 Agent 一样在注册表中（lifespan sync 登记），
    # 走完全相同的解析路径，无特判。
    settings = _StubSettings()
    reg = _FakeRegistry(
        {"main-agent": "/data/business-agents/main-agent/workspace", "biz-1": "/custom/biz-1-ws"}
    )
    ws, cfg = sessions_mod._resolve_owning_profile(settings, reg, LocalSession(session_id="x", agent_id="main-agent"))
    assert ws == Path("/data/business-agents/main-agent/workspace")
    assert cfg == Path("/data/business-agents/main-agent/claude-root/.claude")
    ws, cfg = sessions_mod._resolve_owning_profile(settings, reg, LocalSession(session_id="x", agent_id="biz-1"))
    assert ws == Path("/custom/biz-1-ws")  # #22：cwd 来自注册表 workspace_dir，不硬推导
    assert cfg == Path("/data/business-agents/biz-1/claude-root/.claude")


def test_resolve_owning_profile_unknown_business_agent_raises_404() -> None:
    with pytest.raises(NotFoundError):
        sessions_mod._resolve_owning_profile(_StubSettings(), _FakeRegistry({}), LocalSession(session_id="x", agent_id="ghost"))


def test_resolve_owning_profile_missing_agent_id_is_session_conflict() -> None:
    with pytest.raises(SessionConflictError):
        sessions_mod._resolve_owning_profile(_StubSettings(), _FakeRegistry({}), LocalSession(session_id="x", agent_id=None))


def test_resolve_owning_profile_ignores_client_metadata_agent_id() -> None:
    # backend-owned：session.agent_id 缺失即整改性报错；客户端 metadata.agent_id 不被采信、不能改写归属
    session = LocalSession(session_id="x", agent_id=None, metadata={"agent_id": "attacker"})
    with pytest.raises(SessionConflictError):
        sessions_mod._resolve_owning_profile(_StubSettings(), _FakeRegistry({"attacker": "/x"}), session)


def test_endpoint_unknown_owning_agent_404(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    module.session_store.save(LocalSession(session_id="sg", sdk_session_id="sdk-g", agent_id="ghost-agent"))
    with TestClient(module.app) as client:
        assert client.get("/api/sessions/sg/messages").status_code == 404


def test_endpoint_missing_owning_agent_is_session_conflict_409(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    # transcript 存在（sdk_session_id 有值）但没记 owning agent —— 数据完整性异常，必须报错不静默兜底
    module.session_store.save(LocalSession(session_id="sx", sdk_session_id="sdk-x", agent_id=None))
    with TestClient(module.app) as client:
        response = client.get("/api/sessions/sx/messages")
    assert response.status_code == 409
    assert response.json()["error_code"] == "SESSION_CONFLICT"


def test_endpoint_routes_registered_business_agent(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    module.agent_registry_store.create_business_agent(name="Biz", agent_id="biz-9", workspace_dir="/custom/ws-biz-9")
    module.session_store.save(LocalSession(session_id="sb", sdk_session_id="sdk-b", agent_id="biz-9"))
    captured: dict[str, str] = {}

    def fake_read(*, sdk_session_id, workspace_dir, claude_config_dir, scrub, limit, offset):
        captured["ws"] = str(workspace_dir)
        captured["cfg"] = str(claude_config_dir)
        return {"sdk_session_id": sdk_session_id, "title": None, "messages": [], "subagents": []}

    monkeypatch.setattr(sessions_mod, "read_session_history", fake_read)
    with TestClient(module.app) as client:
        assert client.get("/api/sessions/sb/messages").status_code == 200
    assert captured["ws"] == "/custom/ws-biz-9"  # #22：cwd = 注册表 workspace_dir，不硬推导
    assert captured["cfg"].endswith("/business-agents/biz-9/claude-root/.claude")


def test_endpoint_rejects_invalid_limit_offset(monkeypatch, tmp_path: Path) -> None:
    # #23：limit/offset 越界由 FastAPI Query 约束在进入 handler 前 422，不透传给 SDK
    module = _load_app(monkeypatch, tmp_path)
    module.session_store.save(LocalSession(session_id="s1", sdk_session_id="sdk-1", agent_id="main-agent"))
    with TestClient(module.app) as client:
        assert client.get("/api/sessions/s1/messages", params={"offset": -1}).status_code == 422
        assert client.get("/api/sessions/s1/messages", params={"limit": 0}).status_code == 422
        assert client.get("/api/sessions/s1/messages", params={"limit": 99999}).status_code == 422
