from __future__ import annotations

import asyncio
import threading
import time
from urllib.error import URLError

from app.routers import core
from app.routers.agent_governance import create_agent_governance_router
from app.routers.core import create_core_router
from app.runtime import model_provider
from app.runtime.model_provider import VLLM_VERSION_PROBE_FAILED, ModelProviderRouter
from app.runtime.schemas import RuntimeDependencyVersions
from app.runtime.settings import AppSettings
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient


class _Response:
    status = 200

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def getcode(self) -> int:
        return self.status

    def read(self, _: int) -> bytes:
        return b'{"version":"0.14.0"}'


def _health_app(monkeypatch, router: ModelProviderRouter) -> TestClient:
    app = FastAPI()
    monkeypatch.setattr(core, "runtime_dependency_versions", lambda: RuntimeDependencyVersions())
    app.include_router(
        create_core_router(
            settings=router.settings,
            app=app,
            model_provider_router=router,
        )
    )
    return TestClient(app)


def _vllm_router() -> ModelProviderRouter:
    settings = AppSettings(
        _env_file=None,
        MODEL_PROVIDER_BACKEND="vllm",
        MODEL_PROVIDER_API_URL="http://user:secret@vllm:8000/private?token=hidden",
        MODEL_PROVIDER_PROBE_TIMEOUT_SECONDS=30,
    )
    return ModelProviderRouter(settings)


def test_health_views_have_no_provider_or_subprocess_side_effects(monkeypatch) -> None:
    router = _vllm_router()
    client = _health_app(monkeypatch, router)
    monkeypatch.setattr(model_provider, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network")))
    monkeypatch.setattr(core.subprocess, "check_output", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("subprocess")))

    live = client.get("/health/live")
    ready = client.get("/health/ready")
    health = client.get("/health")

    assert live.status_code == 200
    assert live.json()["status"] == "ok"
    assert ready.status_code == 503
    assert health.status_code == 200
    assert health.json()["agent_version_id"] is None


def test_health_endpoints_remain_fast_while_external_vllm_probe_is_stuck(monkeypatch) -> None:
    router = _vllm_router()
    client = _health_app(monkeypatch, router)
    entered = threading.Event()
    release = threading.Event()

    def slow_urlopen(*_args, **_kwargs):
        entered.set()
        assert release.wait(timeout=5)
        raise URLError(TimeoutError())

    monkeypatch.setattr(model_provider, "urlopen", slow_urlopen)
    probe = threading.Thread(target=router.refresh_readiness)
    probe.start()
    assert entered.wait(timeout=1)

    started = time.monotonic()
    live = client.get("/health/live")
    ready = client.get("/health/ready")
    diagnostic = client.get("/health")
    elapsed = time.monotonic() - started

    assert elapsed < 1
    assert live.status_code == 200
    assert ready.status_code == 503
    assert ready.json()["model_provider"]["error_code"] == "MODEL_PROVIDER_PROBE_IN_PROGRESS"
    assert diagnostic.status_code == 200
    assert diagnostic.json()["status"] == "ok"
    assert diagnostic.json()["model_provider_route"]["readiness"]["status"] == "checking"

    release.set()
    probe.join(timeout=2)
    assert not probe.is_alive()


def test_liveness_remains_fast_while_repository_status_is_blocked(monkeypatch) -> None:
    router = _vllm_router()
    app = FastAPI()
    monkeypatch.setattr(core, "runtime_dependency_versions", lambda: RuntimeDependencyVersions())
    app.include_router(create_core_router(settings=router.settings, app=app, model_provider_router=router))
    entered = threading.Event()
    release = threading.Event()

    class BlockingGovernance:
        def repository_status(self, _agent_id=None):
            entered.set()
            assert release.wait(timeout=5)
            return {
                "provider": "local",
                "repository_name": "test",
                "repository_dir": "/tmp/test",
                "worktrees_dir": "/tmp/test/worktrees",
                "releases_dir": "/tmp/test/releases",
                "status": "active",
            }

    app.include_router(create_agent_governance_router(agent_governance=BlockingGovernance(), require_api_key=lambda: None))

    async def scenario() -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            repository_request = asyncio.create_task(client.get("/api/agent-repository"))
            assert await asyncio.to_thread(entered.wait, 1)
            started = time.monotonic()
            live = await client.get("/health/live")
            elapsed = time.monotonic() - started
            release.set()
            assert (await repository_request).status_code == 200
            assert live.status_code == 200
            assert elapsed < 0.5

    asyncio.run(scenario())


def test_readiness_reports_sanitized_vllm_timeout_and_recovers(monkeypatch) -> None:
    router = _vllm_router()
    client = _health_app(monkeypatch, router)
    monkeypatch.setattr(model_provider, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError(TimeoutError())))

    degraded = router.refresh_readiness()
    response = client.get("/health/ready")

    assert degraded["status"] == "degraded"
    assert response.status_code == 503
    payload = response.json()["model_provider"]
    assert payload["error_code"] == VLLM_VERSION_PROBE_FAILED
    assert payload["reason"] == "timeout"
    assert payload["probe"] == "vllm_version"
    assert "external vLLM" in payload["action"]
    serialized = str(client.get("/health").json())
    assert "http://vllm:8000" in serialized
    assert "user:secret" not in serialized
    assert "token=hidden" not in serialized
    assert "/private" not in serialized

    monkeypatch.setattr(model_provider, "urlopen", lambda *_args, **_kwargs: _Response())
    recovered = router.refresh_readiness()

    assert recovered["status"] == "ready"
    assert client.get("/health/ready").status_code == 200
