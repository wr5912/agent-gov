"""#20：/api/chat 与 /api/chat/stream 两个原生入口都要求 agent_id 必填且有效（不静默跑 main）。

校验发生在进入 runtime 之前：缺失/空白 -> 422，未知业务 Agent -> 404。
main-agent / 已注册业务 Agent 的成功路径会真正驱动 SDK，属容器 e2e，不在单测覆盖。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from test_api_execution_optimizer import _load_app


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
