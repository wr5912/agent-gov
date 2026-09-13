"""健康端点使用真实生产 Runtime client；可达路径由真实容器验收覆盖。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.routers.core import create_core_router
from app.runtime.settings import AppSettings
from app.runtime_gateway.client import AgentScopeRuntimeClient
from fastapi import FastAPI
from fastapi.testclient import TestClient


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
        AGENTSCOPE_RUNTIME_URL="http://127.0.0.1:1",
        AGENTGOV_RUNTIME_SHARED_SECRET="health-test-shared-secret",
    )


def _app_with_unreachable_runtime(tmp_path: Path) -> tuple[FastAPI, AgentScopeRuntimeClient]:
    settings = _settings(tmp_path)
    runtime = AgentScopeRuntimeClient(
        settings.agentscope_runtime_url,
        shared_secret=settings.runtime_shared_secret,
        timeout_seconds=0.2,
    )
    app = FastAPI()
    app.include_router(create_core_router(settings=settings, app=app, runtime_client=runtime))
    return app, runtime


def test_liveness_succeeds_when_real_runtime_client_target_is_unreachable(tmp_path: Path) -> None:
    app, runtime = _app_with_unreachable_runtime(tmp_path)
    try:
        with TestClient(app) as client:
            response = client.get("/health/live")
    finally:
        asyncio.run(runtime.close())

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_readiness_reports_real_connection_failure_without_credentials(tmp_path: Path) -> None:
    app, runtime = _app_with_unreachable_runtime(tmp_path)
    try:
        with TestClient(app) as client:
            response = client.get("/health/ready")
    finally:
        asyncio.run(runtime.close())

    assert response.status_code == 503
    payload = response.json()["runtime_service"]
    assert payload["status"] == "not_ready"
    assert payload["retryable"] is True
    assert "health-test-shared-secret" not in str(payload)


def test_health_exposes_agentscope_only_dependency_versions(tmp_path: Path) -> None:
    app, runtime = _app_with_unreachable_runtime(tmp_path)
    try:
        with TestClient(app) as client:
            response = client.get("/health")
    finally:
        asyncio.run(runtime.close())

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "degraded"
    assert payload["runtime_kind"] == "agentscope"
    assert payload["runtime_dependency_versions"]["agentscope"]
    serialized = str(payload).lower()
    assert "litellm" not in serialized
    assert "sdk_session" not in serialized
