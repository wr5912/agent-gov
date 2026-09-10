from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.routers import core
from app.routers.core import create_core_router
from app.runtime.schemas import RuntimeDependencyVersions
from app.runtime.settings import AppSettings
from app.runtime_gateway.client import RuntimeJsonResponse, RuntimeUpstreamError
from fastapi import FastAPI
from fastapi.testclient import TestClient


@dataclass
class StubRuntimeClient:
    response: RuntimeJsonResponse | None = None
    error: Exception | None = None
    calls: int = 0
    base_url: str = "http://agentscope-runtime:8090"

    async def request_json(self, method: str, path: str, *, timeout: float):
        self.calls += 1
        assert (method, path, timeout) == ("GET", "/health", 5.0)
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


def _settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        _env_file=None,
        RUNTIME_CONTAINER="0",
        DATA_DIR=tmp_path / "data",
        GOVERNOR_WORKSPACE_DIR=tmp_path / "governor",
        AGENT_GIT_REPOSITORY_DIR=tmp_path / "repository",
        AGENT_GIT_WORKTREES_DIR=tmp_path / "worktrees",
        AGENT_RELEASE_ARCHIVES_DIR=tmp_path / "releases",
        RUNTIME_CANDIDATES_DIR=tmp_path / "candidates",
    )


def _client(monkeypatch, tmp_path: Path, runtime: StubRuntimeClient) -> TestClient:
    app = FastAPI()
    monkeypatch.setattr(
        core,
        "runtime_dependency_versions",
        lambda: RuntimeDependencyVersions(agentscope="2.0.8"),
    )
    app.include_router(create_core_router(settings=_settings(tmp_path), app=app, runtime_client=runtime))
    return TestClient(app)


def test_liveness_never_calls_runtime(monkeypatch, tmp_path) -> None:
    runtime = StubRuntimeClient(error=AssertionError("runtime must not be called"))
    client = _client(monkeypatch, tmp_path, runtime)

    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert runtime.calls == 0


def test_readiness_reports_agentscope_reachable(monkeypatch, tmp_path) -> None:
    runtime = StubRuntimeClient(response=RuntimeJsonResponse(200, {}, {"status": "ok"}))
    client = _client(monkeypatch, tmp_path, runtime)

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["runtime_service"]["status"] == "ready"
    assert response.json()["runtime_service"]["route"] == "http://agentscope-runtime:8090"


def test_readiness_reports_agentscope_unreachable_without_leaking_body(monkeypatch, tmp_path) -> None:
    runtime = StubRuntimeClient(error=RuntimeUpstreamError(503, b'{"secret":"hidden"}'))
    client = _client(monkeypatch, tmp_path, runtime)

    response = client.get("/health/ready")

    assert response.status_code == 503
    payload = response.json()["runtime_service"]
    assert payload["status"] == "not_ready"
    assert payload["retryable"] is True
    assert "hidden" not in str(payload)


def test_health_exposes_agentscope_only_dependency_versions(monkeypatch, tmp_path) -> None:
    runtime = StubRuntimeClient(response=RuntimeJsonResponse(200, {}, {"status": "ok"}))
    client = _client(monkeypatch, tmp_path, runtime)

    payload = client.get("/health").json()

    assert payload["runtime_kind"] == "agentscope"
    assert payload["runtime_dependency_versions"]["agentscope"] == "2.0.8"
    serialized = str(payload).lower()
    assert "litellm" not in serialized
    assert "sdk_session" not in serialized
