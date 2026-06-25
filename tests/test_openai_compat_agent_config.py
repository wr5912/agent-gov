"""C：/v1/chat/completions 出口 Agent 可配置（设置 API + /v1 接入）。

重点验证"从未配置（默认 main）"与"显式选 main-agent"是两个不同状态（configured 区分），
且 /v1 据配置解析 profile（未配置→main；配置失效→fail-loud 503）。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.runtime.runtime_db import make_session_factory, runtime_db_path_from_data_dir
from app.runtime.schemas import ChatResponse
from app.runtime.stores.runtime_settings_store import RuntimeSettingsStore
from test_api_execution_optimizer import _load_app


def _register_biz(client: TestClient, agent_id: str = "soc-ops", name: str = "客服助手") -> None:
    assert client.post("/api/agent-registry", json={"name": name, "agent_id": agent_id}).status_code == 201


def _fake_capturing_run(captured: dict):
    async def fake_run(req, *, profile=None, **kwargs):
        captured["profile"] = profile
        return ChatResponse(run_id="r", session_id="s", answer="ok")

    return fake_run


# ---------------------------------------------------------------- store unit

def test_store_distinguishes_unset_from_explicit_main(tmp_path: Path) -> None:
    store = RuntimeSettingsStore(make_session_factory(runtime_db_path_from_data_dir(tmp_path)))
    assert store.get_openai_compat_agent_id() is None  # 从未配置
    store.set_openai_compat_agent_id("main-agent")
    assert store.get_openai_compat_agent_id() == "main-agent"  # 显式 main（与未配置不同的状态）
    assert store.clear_openai_compat_agent_id() is True
    assert store.get_openai_compat_agent_id() is None  # 重置回未配置
    assert store.clear_openai_compat_agent_id() is False  # 已无行


# ---------------------------------------------------------------- settings API

def test_get_unconfigured_defaults_main(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        assert client.get("/api/settings/openai-compat-agent").json() == {
            "agent_id": None,
            "configured": False,
            "effective_agent_id": "main-agent",
        }


def test_explicit_main_is_configured_distinct_from_unset(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        # 显式选 main-agent -> configured=True（与"未配置"区分），effective 仍 main
        assert client.put("/api/settings/openai-compat-agent", json={"agent_id": "main-agent"}).json() == {
            "agent_id": "main-agent",
            "configured": True,
            "effective_agent_id": "main-agent",
        }
        # DELETE 重置回未配置
        assert client.delete("/api/settings/openai-compat-agent").json() == {
            "agent_id": None,
            "configured": False,
            "effective_agent_id": "main-agent",
        }


def test_put_business_agent_validates_and_persists(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        _register_biz(client)
        assert client.put("/api/settings/openai-compat-agent", json={"agent_id": "soc-ops"}).json() == {
            "agent_id": "soc-ops",
            "configured": True,
            "effective_agent_id": "soc-ops",
        }


def test_put_unknown_agent_404(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        assert client.put("/api/settings/openai-compat-agent", json={"agent_id": "ghost-agent"}).status_code == 404


def test_put_blank_agent_id_rejected_422(monkeypatch, tmp_path: Path) -> None:
    # 空白 agent_id 不入库（否则会出现 configured=True 但实际跑 main 的不一致态）
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        assert client.put("/api/settings/openai-compat-agent", json={"agent_id": ""}).status_code == 422
        assert client.put("/api/settings/openai-compat-agent", json={"agent_id": "   "}).status_code == 422
        # 仍未配置（未写入）
        assert client.get("/api/settings/openai-compat-agent").json()["configured"] is False


# ---------------------------------------------------------------- /v1 接入

def test_v1_runs_configured_business_agent(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    captured: dict = {}
    monkeypatch.setattr(module.runtime, "run", _fake_capturing_run(captured))
    with TestClient(module.app) as client:
        _register_biz(client)
        client.put("/api/settings/openai-compat-agent", json={"agent_id": "soc-ops"})
        resp = client.post("/v1/chat/completions", json={"model": "x", "messages": [{"role": "user", "content": "hi"}]})
        assert resp.status_code == 200
    assert captured["profile"] is not None
    assert str(captured["profile"].workspace_dir).endswith("/business-agents/soc-ops")


def test_v1_unconfigured_runs_main(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    captured: dict = {}
    monkeypatch.setattr(module.runtime, "run", _fake_capturing_run(captured))
    with TestClient(module.app) as client:
        resp = client.post("/v1/chat/completions", json={"model": "x", "messages": [{"role": "user", "content": "hi"}]})
        assert resp.status_code == 200
    assert captured["profile"] is None  # 未配置 -> main（profile=None）


def test_v1_fail_loud_when_configured_agent_deleted(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        _register_biz(client)
        client.put("/api/settings/openai-compat-agent", json={"agent_id": "soc-ops"})
        assert client.delete("/api/agent-registry/soc-ops").status_code == 200
        # 出口 Agent 悬空 -> /v1 fail-loud 503，不静默回 main
        resp = client.post("/v1/chat/completions", json={"model": "x", "messages": [{"role": "user", "content": "hi"}]})
        assert resp.status_code == 503
