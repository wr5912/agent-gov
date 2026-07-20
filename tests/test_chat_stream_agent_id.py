"""#20：/api/chat 与 /api/chat/stream 两个原生入口都要求 agent_id 必填且有效（不静默跑 main）。

校验发生在进入 runtime 之前：缺失/空白 -> 422，未知业务 Agent -> 404；
成功路径只用 fake runtime 固定 SSE 投影，不在单测中调用真实模型。
"""

from __future__ import annotations

from pathlib import Path

from app.runtime.protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from fastapi.testclient import TestClient

from app_test_utils import load_test_app as _load_app


def test_chat_stream_requires_agent_id_422(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        assert client.post("/api/chat/stream", json={"message": "hi"}).status_code == 422
        assert client.post("/api/chat/stream", json={"message": "hi", "agent_id": "  "}).status_code == 422


def test_chat_stream_unknown_agent_id_404(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        assert client.post("/api/chat/stream", json={"message": "hi", "agent_id": "ghost-agent"}).status_code == 404


def test_chat_requires_agent_id_422(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        assert client.post("/api/chat", json={"message": "hi"}).status_code == 422
        assert client.post("/api/chat", json={"message": "hi", "agent_id": "  "}).status_code == 422


def test_chat_unknown_agent_id_404(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        assert client.post("/api/chat", json={"message": "hi", "agent_id": "ghost-agent"}).status_code == 404


def test_chat_rejects_removed_runtime_policy_fields(monkeypatch, tmp_path: Path) -> None:
    removed_fields: list[tuple[str, object]] = [
        ("agent", "legacy-subagent"),
        ("skills", ["legacy-skill"]),
        ("skills_mode", "all"),
        ("allowed_tools", ["Read"]),
        ("disallowed_tools", ["Bash"]),
        ("permission_mode", "bypassPermissions"),
    ]
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        for endpoint in ("/api/chat", "/api/chat/stream"):
            for field, value in removed_fields:
                response = client.post(endpoint, json={"message": "hi", "agent_id": DEFAULT_BUSINESS_AGENT_ID, field: value})

                assert response.status_code == 422
                assert any(error.get("type") == "extra_forbidden" for error in response.json()["detail"])


def test_chat_stream_projects_prompt_suggestion_event(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)

    async def fake_stream(req, *, profile=None):
        yield {
            "event": "prompt_suggestion",
            "data": {"suggestion": "继续检查边界条件", "run_id": "run-1", "session_id": "session-1"},
        }
        yield {"event": "done", "data": "[DONE]"}

    monkeypatch.setattr(module.runtime, "stream", fake_stream)
    with TestClient(module.app) as client:
        response = client.post("/api/chat/stream", json={"message": "hi", "agent_id": DEFAULT_BUSINESS_AGENT_ID})

    assert response.status_code == 200
    assert "event: prompt_suggestion" in response.text
    assert '"suggestion": "继续检查边界条件"' in response.text
