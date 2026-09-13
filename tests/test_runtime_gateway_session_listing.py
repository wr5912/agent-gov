from __future__ import annotations

from pathlib import Path

from app.routers.error_handlers import register_error_handlers
from app.runtime.runtime_db import make_session_factory
from app.runtime_gateway.client import RuntimeJsonResponse
from app.runtime_gateway.router import create_runtime_router
from app.runtime_gateway.store import RuntimeRunStore, RuntimeStateConflict
from fastapi import FastAPI
from fastapi.testclient import TestClient

from runtime_gateway_test_utils import store_with_agent_version


class _SessionListClient:
    def __init__(self, sessions_by_runtime: dict[str, list[dict[str, object]]]) -> None:
        self.sessions_by_runtime = sessions_by_runtime
        self.calls: list[str] = []

    async def request_json(self, method: str, path: str, **kwargs) -> RuntimeJsonResponse:
        assert (method, path) == ("GET", "/sessions/")
        runtime_agent_id = kwargs["params"]["agent_id"]
        self.calls.append(runtime_agent_id)
        return RuntimeJsonResponse(
            200,
            {"content-type": "application/json"},
            {"sessions": self.sessions_by_runtime[runtime_agent_id]},
        )


class _SessionListProvisioner:
    def __init__(self, store: RuntimeRunStore, *, missing_runtime_id: str | None = None) -> None:
        self.store = store
        self.missing_runtime_id = missing_runtime_id
        self.calls: list[str] = []

    def require_session(self, session_id: str, runtime_agent_id: str):
        self.calls.append(runtime_agent_id)
        if runtime_agent_id == self.missing_runtime_id:
            raise RuntimeStateConflict("Published Harness snapshot is missing")
        return self.store.get_session(session_id, runtime_agent_id=runtime_agent_id)


def _list_sessions(store: RuntimeRunStore, upstream: _SessionListClient, provisioner: _SessionListProvisioner):
    api = FastAPI()
    register_error_handlers(api)
    api.include_router(
        create_runtime_router(
            client=upstream,  # type: ignore[arg-type]
            store=store,
            provisioner=provisioner,  # type: ignore[arg-type]
            model_type="openai_credential",
            credential_id="provider",
            model_name="model",
            model_parameters={},
            require_api_key=lambda: None,
        ),
    )
    with TestClient(api) as client:
        return client.get("/api/runtime/sessions/", params={"governance_agent_id": "agent-a"})


def _bind_session(store: RuntimeRunStore, *, version_id: str, runtime_id: str, session_id: str, digest: str) -> None:
    store.bind_session(
        session_id=session_id,
        agent_id="agent-a",
        agent_version_id=version_id,
        runtime_agent_id=runtime_id,
        digest=digest,
    )


def _bind_candidate(store: RuntimeRunStore) -> None:
    store.bind_agent_version(
        agent_id="candidate-owner",
        governance_agent_id="agent-a",
        agent_version_id="candidate-version",
        digest="c" * 64,
        runtime_agent_id="runtime-candidate",
        source_kind="candidate_snapshot",
        source_id="candidate-source",
    )
    _bind_session(
        store,
        version_id="candidate-version",
        runtime_id="runtime-candidate",
        session_id="session-candidate",
        digest="c" * 64,
    )


def _view(session_id: str) -> dict[str, object]:
    return {"session": {"id": session_id}}


def test_published_and_candidate_sessions_list_only_published(tmp_path: Path) -> None:
    store = store_with_agent_version(tmp_path)
    _bind_session(store, version_id="version-a", runtime_id="runtime-a", session_id="session-published", digest="a" * 64)
    _bind_candidate(store)
    upstream = _SessionListClient({"runtime-a": [_view("session-published")]})
    provisioner = _SessionListProvisioner(store)

    response = _list_sessions(store, upstream, provisioner)

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["sessions"][0]["session"]["id"] == "session-published"
    assert upstream.calls == ["runtime-a"]
    assert provisioner.calls == ["runtime-a"]


def test_only_candidate_session_returns_empty_published_list(tmp_path: Path) -> None:
    store = RuntimeRunStore(make_session_factory(tmp_path / "runtime.db"))
    _bind_candidate(store)
    upstream = _SessionListClient({})
    provisioner = _SessionListProvisioner(store)

    response = _list_sessions(store, upstream, provisioner)

    assert response.status_code == 200
    assert response.json() == {"sessions": [], "total": 0}
    assert upstream.calls == []
    assert provisioner.calls == []


def test_missing_published_snapshot_still_fails_closed(tmp_path: Path) -> None:
    store = store_with_agent_version(tmp_path)
    _bind_session(store, version_id="version-a", runtime_id="runtime-a", session_id="session-published", digest="a" * 64)
    upstream = _SessionListClient({"runtime-a": [_view("session-published")]})
    provisioner = _SessionListProvisioner(store, missing_runtime_id="runtime-a")

    response = _list_sessions(store, upstream, provisioner)

    assert response.status_code == 409
    assert response.json()["error_code"] == "RUNTIMESTATECONFLICT"
    assert upstream.calls == []
    assert provisioner.calls == ["runtime-a"]


def test_two_published_versions_remain_visible(tmp_path: Path) -> None:
    store = store_with_agent_version(tmp_path)
    store.bind_agent_version(
        agent_id="agent-a",
        agent_version_id="version-b",
        digest="b" * 64,
        runtime_agent_id="runtime-b",
    )
    _bind_session(store, version_id="version-a", runtime_id="runtime-a", session_id="session-a", digest="a" * 64)
    _bind_session(store, version_id="version-b", runtime_id="runtime-b", session_id="session-b", digest="b" * 64)
    upstream = _SessionListClient({"runtime-a": [_view("session-a")], "runtime-b": [_view("session-b")]})
    provisioner = _SessionListProvisioner(store)

    response = _list_sessions(store, upstream, provisioner)

    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert {item["session"]["id"] for item in response.json()["sessions"]} == {"session-a", "session-b"}
    assert set(upstream.calls) == {"runtime-a", "runtime-b"}
    assert set(provisioner.calls) == {"runtime-a", "runtime-b"}
