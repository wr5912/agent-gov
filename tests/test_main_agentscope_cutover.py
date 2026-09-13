from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app_test_utils import load_test_app  # noqa: E402


def test_main_exposes_only_agentscope_runtime_surfaces(process_environment, tmp_path) -> None:
    process_environment.set("AGENTGOV_RUNTIME_SHARED_SECRET", "test-runtime-shared-secret")
    module = load_test_app(process_environment, tmp_path)
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
        "/api/langfuse/traces/{trace_id}",
        "/api/agents",
        "/api/skills",
        "/api/config",
        "/api/agent-config-file",
        "/api/runtime/agents/{governance_agent_id}/provision",
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


def test_api_startup_reconciles_publication_evidence_before_serving_requests(
    process_environment,
    tmp_path,
    monkeypatch,
) -> None:
    process_environment.set("AGENTGOV_RUNTIME_SHARED_SECRET", "test-runtime-shared-secret")
    module = load_test_app(process_environment, tmp_path)
    calls: list[object] = []

    class EmptyReport:
        @staticmethod
        def to_payload() -> dict[str, int]:
            return {}

    def reconcile(service):
        calls.append(service)
        return EmptyReport()

    monkeypatch.setattr(module, "reconcile_legacy_publication_evidence", reconcile)
    with TestClient(module.app) as client:
        assert calls == [module.agent_governance]
        assert client.get("/health/live").status_code == 200


@pytest.mark.parametrize(
    ("method", "path"),
    (
        ("GET", "/api/agents"),
        ("GET", "/api/skills"),
        ("GET", "/api/config"),
        ("GET", "/api/agent-config-file"),
        ("PUT", "/api/agent-config-file"),
        ("POST", "/api/runtime/agents/retired/provision"),
    ),
)
def test_removed_runtime_management_tracks_are_not_routable(
    process_environment,
    tmp_path,
    method: str,
    path: str,
) -> None:
    process_environment.set("AGENTGOV_RUNTIME_SHARED_SECRET", "test-runtime-shared-secret")
    module = load_test_app(process_environment, tmp_path)

    with TestClient(module.app) as client:
        response = client.request(method, path)

    assert response.status_code == 404
