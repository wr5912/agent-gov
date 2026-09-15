from __future__ import annotations

import time
from pathlib import Path

import pytest
from app.api_mode import ACCEPTANCE_IDENTITY_HEADER, ApiModeGateMiddleware
from app.routers.error_handlers import register_error_handlers
from app.runtime.runtime_db import make_session_factory
from app.runtime_gateway.contracts import RunStatus, RuntimeReceipt
from app.runtime_gateway.router import create_internal_runtime_router
from app.runtime_gateway.security import sign_internal_request
from app.runtime_gateway.store import RuntimeRunStore
from fastapi import FastAPI
from fastapi.testclient import TestClient

RUNTIME_SECRET = "runtime-shared-secret-for-mode-gate"
ACCEPTANCE_IDENTITY = "agentscope-cutover-one"
ACCEPTANCE_KEY = "one-time-acceptance-key"
MUTATIONS = (
    ("POST", "/api/runtime/sessions/"),
    ("POST", "/api/runtime/chat/"),
    ("POST", "/api/feedback-signals"),
    ("POST", "/api/agent-change-sets/change-set/publish"),
    ("POST", "/api/agent-registry/security/workspace/restore"),
    ("DELETE", "/api/agent-registry/security"),
)


def _app(
    mode: str,
    state_file: Path | None = None,
    *,
    store: RuntimeRunStore | None = None,
) -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        ApiModeGateMiddleware,
        mode=mode,
        acceptance_identity=ACCEPTANCE_IDENTITY,
        acceptance_api_key=ACCEPTANCE_KEY,
        state_file=state_file,
    )
    if store is not None:
        register_error_handlers(app)
        app.include_router(
            create_internal_runtime_router(
                store=store,
                shared_secret=RUNTIME_SECRET,
            ),
        )
    return app


def _runtime_store(tmp_path: Path):
    store = RuntimeRunStore(make_session_factory(tmp_path / "api-mode.db"))
    store.bind_agent_version(
        agent_id="agent-mode-gate",
        agent_version_id="version-mode-gate",
        digest="a" * 64,
        runtime_agent_id="runtime-agent-mode-gate",
    )
    store.bind_session(
        session_id="session-mode-gate",
        agent_id="agent-mode-gate",
        agent_version_id="version-mode-gate",
        runtime_agent_id="runtime-agent-mode-gate",
        digest="a" * 64,
    )
    run = store.begin_run(
        session_id="session-mode-gate",
        runtime_agent_id="runtime-agent-mode-gate",
        input_value={"role": "user", "content": []},
        entities={},
        metadata={},
    )
    store.mark_trigger_started(run.run_id)
    return store, store.get_run(run.run_id)


@pytest.mark.parametrize(("method", "path"), MUTATIONS)
def test_drain_rejects_every_external_mutation(method: str, path: str) -> None:
    response = TestClient(_app("drain")).request(method, path)

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert response.json() == {"detail": "API mutation gate is drain"}


@pytest.mark.parametrize("path", ("/api/agent-registry", "/health/ready"))
def test_drain_allows_reads_and_health(path: str) -> None:
    response = TestClient(_app("drain")).get(path)

    # 404 来自未伪造业务端点的真实 FastAPI 路由边界，证明 gate 已放行读取。
    assert response.status_code == 404


def test_drain_allows_only_router_verified_runtime_receipt_write(tmp_path: Path) -> None:
    store, run = _runtime_store(tmp_path)
    receipt = RuntimeReceipt(
        receipt_id="receipt-mode-gate",
        event_id="event-mode-gate",
        session_id=run.session_id,
        run_id=run.run_id,
        reply_id="reply-mode-gate",
        type="REPLY_START",
        payload={},
        trace_id=run.trace_id,
    )
    body = receipt.model_dump_json().encode()
    timestamp = str(time.time())
    signature = sign_internal_request(
        secret=RUNTIME_SECRET,
        timestamp=timestamp,
        method="POST",
        path="/internal/runtime-receipts",
        body=body,
    )
    response = TestClient(_app("drain", store=store)).post(
        "/internal/runtime-receipts",
        content=body,
        headers={"X-AgentGov-Timestamp": timestamp, "X-AgentGov-Signature": signature},
    )

    assert response.status_code == 200
    assert response.json()["run_id"] == run.run_id
    assert response.json()["status"] == RunStatus.RUNNING
    assert store.get_run(run.run_id).status is RunStatus.RUNNING


@pytest.mark.parametrize(
    "headers",
    (
        {},
        {ACCEPTANCE_IDENTITY_HEADER: ACCEPTANCE_IDENTITY},
        {"Authorization": f"Bearer {ACCEPTANCE_KEY}"},
        {
            "Authorization": f"Bearer {ACCEPTANCE_KEY}",
            ACCEPTANCE_IDENTITY_HEADER: "spoofed-identity",
        },
    ),
)
def test_acceptance_mutation_requires_identity_bound_to_one_time_bearer(headers: dict[str, str]) -> None:
    response = TestClient(_app("acceptance")).post("/api/runtime/chat/", headers=headers)

    assert response.status_code == 503


def test_acceptance_identity_and_one_time_bearer_allow_mutation() -> None:
    response = TestClient(_app("acceptance")).post(
        "/api/runtime/chat/",
        headers={
            "Authorization": f"Bearer {ACCEPTANCE_KEY}",
            ACCEPTANCE_IDENTITY_HEADER: ACCEPTANCE_IDENTITY,
        },
    )

    assert response.status_code == 404


def test_one_mounted_state_file_is_the_atomic_drain_to_open_latch(tmp_path) -> None:
    state_file = tmp_path / "api-gate-state.json"
    state_file.write_text(
        '{"schema_version":1,"state":"drain","cutover_id":"agentscope-cutover-one"}\n',
        encoding="utf-8",
    )
    client = TestClient(_app("open", state_file))
    assert client.post("/api/runtime/chat/").status_code == 503

    replacement = tmp_path / ".api-gate-state.json.next"
    replacement.write_text(
        '{"schema_version":1,"state":"open","cutover_id":"agentscope-cutover-one","irreversible_at":"now"}\n',
        encoding="utf-8",
    )
    replacement.replace(state_file)

    assert client.post("/api/runtime/chat/").status_code == 404


def test_invalid_mounted_gate_file_fails_closed_for_mutations_but_keeps_health_readable(tmp_path) -> None:
    state_file = tmp_path / "api-gate-state.json"
    state_file.write_text('{"schema_version":1,"state":"unknown"}\n', encoding="utf-8")
    client = TestClient(_app("open", state_file))

    assert client.post("/api/runtime/chat/").status_code == 503
    assert client.get("/health/ready").status_code == 404


@pytest.mark.parametrize(
    "payload",
    (
        '{"schema_version":1,"state":"drain","cutover_id":"wrong-cutover"}\n',
        '{"schema_version":1,"state":"open","cutover_id":"agentscope-cutover-one"}\n',
    ),
)
def test_mounted_gate_is_bound_to_cutover_and_open_requires_irreversible_marker(
    tmp_path,
    payload: str,
) -> None:
    state_file = tmp_path / "api-gate-state.json"
    state_file.write_text(payload, encoding="utf-8")
    client = TestClient(_app("open", state_file))

    assert client.post("/api/runtime/chat/").status_code == 503
    assert client.post("/internal/runtime-receipts", content=b"{}").status_code == 503


def test_unknown_mode_and_incomplete_acceptance_configuration_fail_closed() -> None:
    with pytest.raises(ValueError, match="Unsupported AGENTGOV_API_MODE"):
        _app("unknown").build_middleware_stack()
    app = FastAPI()
    app.add_middleware(
        ApiModeGateMiddleware,
        mode="acceptance",
        acceptance_identity=None,
        acceptance_api_key=None,
    )
    with pytest.raises(ValueError, match="one-time identity"):
        app.build_middleware_stack()
