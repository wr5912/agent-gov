from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.agent_testing.service import AgentTestingService, _TestSession  # noqa: E402

from app_test_utils import load_test_app  # noqa: E402


def test_main_exposes_only_agentscope_runtime_surfaces(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGENTGOV_RUNTIME_SHARED_SECRET", "test-runtime-shared-secret")
    module = load_test_app(monkeypatch, tmp_path)
    paths = {route.path for route in module.app.routes}

    assert {
        "/api/runtime/sessions/",
        "/api/runtime/chat/",
        "/api/runtime/sessions/{session_id}/stream",
        "/api/agent-runs/{run_id}",
        "/api/agent-runs/{run_id}/trace",
    } <= paths
    assert {
        "/api/chat",
        "/api/chat/stream",
        "/api/agent-runtime/sdk-events",
        "/api/debug/agent-runtime/raw-events",
        "/v1/chat/completions",
        "/v1/responses",
        "/v1/conversations",
    }.isdisjoint(paths)
    assert module.runtime_execution.client is module.runtime_client
    with TestClient(module.app) as client:
        response = client.get("/health/live")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


def test_api_main_does_not_load_runtime_provider_or_mcp_secrets() -> None:
    source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")

    assert "load_runtime_env" not in source
    assert "dict(os.environ)" not in source
    assert "runtime_env = MappingProxyType({})" in source


def test_candidate_test_session_cleanup_releases_runtime_before_local_checkout(tmp_path) -> None:
    store = Mock()
    store.reconcile_interrupted_runs.return_value = []
    release_candidate = AsyncMock()
    service = AgentTestingService(
        store=store,
        store_for=lambda _agent_id: Mock(),
        agent_exists=lambda _agent_id: True,
        get_change_set=lambda _change_set_id: None,
        run_candidate=AsyncMock(),
        release_candidate=release_candidate,
        artifacts_dir=tmp_path / "artifacts",
        api_base_url="http://agent-gov.test",
        api_key=None,
        run_timeout_seconds=10,
    )
    service.runner.remove_checkout = Mock()
    session = _TestSession(
        test_session_id="ats-test",
        agent_id="agent-a",
        commit_sha="a" * 40,
        change_set_id=None,
        checkout=tmp_path / "checkout",
        created_at="2026-09-09T00:00:00+00:00",
    )
    service._sessions[session.test_session_id] = session

    asyncio.run(service.delete_session_async(session.test_session_id))

    release_candidate.assert_awaited_once_with("agent-test-ats-test")
    service.runner.remove_checkout.assert_called_once()
    assert session.test_session_id not in service._sessions
    service.close()


def test_candidate_cleanup_removes_local_checkout_when_runtime_release_fails(tmp_path) -> None:
    store = Mock()
    store.reconcile_interrupted_runs.return_value = []
    release_candidate = AsyncMock(side_effect=RuntimeError("runtime unavailable"))
    service = AgentTestingService(
        store=store,
        store_for=lambda _agent_id: Mock(),
        agent_exists=lambda _agent_id: True,
        get_change_set=lambda _change_set_id: None,
        run_candidate=AsyncMock(),
        release_candidate=release_candidate,
        artifacts_dir=tmp_path / "artifacts",
        api_base_url="http://agent-gov.test",
        api_key=None,
        run_timeout_seconds=10,
    )
    service.runner.remove_checkout = Mock()
    session = _TestSession(
        test_session_id="ats-failed",
        agent_id="agent-a",
        commit_sha="b" * 40,
        change_set_id=None,
        checkout=tmp_path / "checkout",
        created_at="2026-09-09T00:00:00+00:00",
    )
    service._sessions[session.test_session_id] = session

    with pytest.raises(RuntimeError, match="runtime unavailable"):
        asyncio.run(service.delete_session_async(session.test_session_id))

    service.runner.remove_checkout.assert_called_once()
    assert session.test_session_id not in service._sessions
    service.close()
