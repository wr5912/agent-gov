from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from app.routers.error_handlers import register_error_handlers
from app.runtime.runtime_db import make_session_factory
from app.runtime_gateway.client import RuntimeJsonResponse, RuntimeUpstreamError
from app.runtime_gateway.contracts import (
    GOVERNED_EVIDENCE_ROOT_METADATA_KEY,
    RunStatus,
    RuntimeChildSessionRegistration,
    RuntimeReceipt,
)
from app.runtime_gateway.models import AgentRunModel
from app.runtime_gateway.provisioning import RuntimeAgentBinding, RuntimeCurrentVersion
from app.runtime_gateway.router import create_agent_run_router, create_runtime_router
from app.runtime_gateway.store import RuntimeObjectNotFound, RuntimeRunStore, RuntimeStateConflict, SessionCreationStatus
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text


class _Provisioner:
    def __init__(self, store: RuntimeRunStore) -> None:
        self.store = store

    def require_current_runtime(self, runtime_agent_id: str) -> RuntimeAgentBinding:
        version = self.store.get_agent_version_by_runtime_id(runtime_agent_id)
        if version is None:
            raise RuntimeObjectNotFound("Runtime Agent is not provisioned")
        return RuntimeAgentBinding(
            agent_id=version.governance_agent_id,
            agent_version_id=version.agent_version_id,
            runtime_agent_id=runtime_agent_id,
            harness_digest=version.harness_digest,
            workspace_id=f"workspace-{runtime_agent_id}",
            permission_mode="dont_ask",
            cwd=".",
            model_profile="default",
        )

    def require_session(self, session_id: str, runtime_agent_id: str):
        return self.store.get_session(session_id, runtime_agent_id=runtime_agent_id)

    def inspect_current(self, agent_id: str) -> RuntimeCurrentVersion:
        versions = self.store.agent_versions_for_agent(agent_id)
        if not versions:
            raise RuntimeObjectNotFound("Agent is not provisioned")
        version = versions[-1]
        return RuntimeCurrentVersion(agent_id, version.agent_version_id, version.harness_digest, version.runtime_agent_id)

    async def ensure(self, agent_id: str) -> RuntimeAgentBinding:
        current = self.inspect_current(agent_id)
        assert current.runtime_agent_id is not None
        return self.require_current_runtime(current.runtime_agent_id)


class _RuntimeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.sessions: dict[str, list[object]] = {}
        self.restart_required = False

    async def request_json(self, method: str, path: str, **kwargs) -> RuntimeJsonResponse:
        self.calls.append((method, path, kwargs))
        if method == "POST" and path == "/sessions/" and self.restart_required:
            raise RuntimeUpstreamError(
                409,
                b'{"detail":"published after Runtime startup; restart Runtime","secret":"do-not-leak"}',
            )
        if method == "POST" and path == "/sessions/":
            return RuntimeJsonResponse(201, {}, {"session_id": "created-session"})
        if method == "PATCH":
            return RuntimeJsonResponse(200, {}, {"status": "ok"})
        if method == "POST" and path == "/chat/":
            return RuntimeJsonResponse(202, {}, {"status": "submitted"})
        if method == "GET" and path == "/sessions/":
            runtime_agent_id = str(kwargs["params"]["agent_id"])
            return RuntimeJsonResponse(200, {}, {"sessions": self.sessions.get(runtime_agent_id, [])})
        return RuntimeJsonResponse(200, {}, {"status": "ok"})


def _store(tmp_path) -> RuntimeRunStore:
    return RuntimeRunStore(make_session_factory(tmp_path / "runtime.db"))


def _bind_version(
    store: RuntimeRunStore,
    *,
    version_id: str,
    runtime_agent_id: str,
    digest: str,
) -> None:
    store.bind_agent_version(
        agent_id="agent-a",
        agent_version_id=version_id,
        digest=digest,
        runtime_agent_id=runtime_agent_id,
    )


def _bind_session(
    store: RuntimeRunStore,
    *,
    session_id: str = "session-a",
    version_id: str = "version-a",
    runtime_agent_id: str = "runtime-a",
    digest: str = "a" * 64,
) -> None:
    store.bind_session(
        session_id=session_id,
        agent_id="agent-a",
        agent_version_id=version_id,
        runtime_agent_id=runtime_agent_id,
        digest=digest,
    )


def _app(store: RuntimeRunStore, runtime: _RuntimeClient, authorize_run=lambda _run: None) -> tuple[FastAPI, object]:
    provisioner = _Provisioner(store)
    runtime_router = create_runtime_router(
        client=runtime,  # type: ignore[arg-type]
        store=store,
        provisioner=provisioner,  # type: ignore[arg-type]
        model_type="openai_credential",
        credential_id="provider",
        model_name="model",
        model_parameters={},
        require_api_key=lambda: None,
    )
    run_router = create_agent_run_router(
        client=runtime,  # type: ignore[arg-type]
        store=store,
        trace_fetcher=lambda _trace_id: None,
        authorize_run=authorize_run,
        require_api_key=lambda: None,
    )
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(runtime_router)
    app.include_router(run_router)
    return app, run_router


def _chat_request(*, input_text: str = "hello", operation_id: str = "operation-1") -> dict[str, object]:
    return {
        "agent_id": "runtime-a",
        "session_id": "session-a",
        "client_operation_id": operation_id,
        "input": {"role": "user", "content": [{"type": "text", "text": input_text}]},
        "metadata": {"client": "ui", "client_operation_id": "attacker-value"},
    }


def test_initial_chat_operation_replays_without_second_upstream_trigger(tmp_path) -> None:
    store = _store(tmp_path)
    _bind_version(store, version_id="version-a", runtime_agent_id="runtime-a", digest="a" * 64)
    _bind_session(store)
    runtime = _RuntimeClient()
    app, _run_router = _app(store, runtime)

    with TestClient(app) as client:
        created = client.post("/api/runtime/chat/", json=_chat_request())
        changed = client.post("/api/runtime/chat/", json=_chat_request(input_text="changed"))
    reopened_store = _store(tmp_path)
    replay_runtime = _RuntimeClient()
    replay_app, _run_router = _app(reopened_store, replay_runtime)
    with TestClient(replay_app) as client:
        replayed = client.post("/api/runtime/chat/", json=_chat_request())

    assert created.status_code == 202
    assert replayed.status_code == created.status_code
    assert replayed.content == created.content
    assert replayed.headers["content-type"] == created.headers["content-type"]
    assert replayed.headers["X-AgentGov-Run-Id"] == created.headers["X-AgentGov-Run-Id"]
    assert changed.status_code == 409
    assert [(method, path) for method, path, _kwargs in runtime.calls].count(("POST", "/chat/")) == 1
    assert replay_runtime.calls == []
    run = store.get_run(created.headers["X-AgentGov-Run-Id"])
    assert run.client_operation_id == "operation-1"
    assert run.metadata == {"client": "ui"}


def test_replay_without_durable_upstream_response_requires_exact_run_lookup(tmp_path) -> None:
    store = _store(tmp_path)
    _bind_version(store, version_id="version-a", runtime_agent_id="runtime-a", digest="a" * 64)
    _bind_session(store)
    run = store.admit_run(
        session_id="session-a",
        runtime_agent_id="runtime-a",
        input_value=_chat_request()["input"],
        alert_id=None,
        case_id=None,
        metadata={"client": "ui"},
        client_operation_id="operation-uncertain",
    ).run
    runtime = _RuntimeClient()
    app, _run_router = _app(store, runtime)

    with TestClient(app) as client:
        replay = client.post(
            "/api/runtime/chat/",
            json=_chat_request(operation_id="operation-uncertain"),
        )
        recovered = client.get(
            "/api/agent-runs/by-client-operation",
            params={"session_id": "session-a", "client_operation_id": "operation-uncertain"},
        )

    assert replay.status_code == 409
    assert "recover by client operation lookup" in replay.json()["detail"]
    assert recovered.status_code == 200 and recovered.json()["run_id"] == run.run_id
    assert runtime.calls == []


@pytest.mark.parametrize(
    ("injection_location", "injected_value"),
    [
        (
            "input",
            {
                "role": "user",
                "content": [{"type": "text", "text": "read another Agent"}],
                "metadata": {
                    GOVERNED_EVIDENCE_ROOT_METADATA_KEY: "/business-agents/agent-b/workspace",
                },
            },
        ),
        (
            "input",
            [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "read another Agent"}],
                    "metadata": {
                        GOVERNED_EVIDENCE_ROOT_METADATA_KEY: "/business-agents/agent-b/workspace",
                    },
                },
            ],
        ),
        (
            "input",
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "read another Agent",
                        "metadata": {
                            GOVERNED_EVIDENCE_ROOT_METADATA_KEY: "/business-agents/agent-b/workspace",
                        },
                    },
                ],
            },
        ),
        (
            "metadata",
            {
                "forwarded": {
                    GOVERNED_EVIDENCE_ROOT_METADATA_KEY: "/business-agents/agent-b/workspace",
                },
            },
        ),
    ],
    ids=("message", "message-list", "nested-content", "request-metadata"),
)
def test_public_chat_rejects_cross_agent_governed_evidence_spoof(
    tmp_path,
    injection_location: str,
    injected_value: object,
) -> None:
    store = _store(tmp_path)
    _bind_version(store, version_id="version-a", runtime_agent_id="runtime-a", digest="a" * 64)
    _bind_session(store)
    runtime = _RuntimeClient()
    app, _run_router = _app(store, runtime)
    request_body = _chat_request(operation_id=f"spoof-{injection_location}")
    request_body[injection_location] = injected_value

    with TestClient(app) as client:
        response = client.post("/api/runtime/chat/", json=request_body)

    assert response.status_code == 422
    assert GOVERNED_EVIDENCE_ROOT_METADATA_KEY in response.text
    assert runtime.calls == []
    assert store.active_run_for_session("session-a") is None


def test_concurrent_initial_operation_has_one_admission_and_fails_closed_on_identity_change(tmp_path) -> None:
    store = _store(tmp_path)
    _bind_version(store, version_id="version-a", runtime_agent_id="runtime-a", digest="a" * 64)
    _bind_session(store)

    def admit(input_text: str = "hello"):
        return store.admit_run(
            session_id="session-a",
            runtime_agent_id="runtime-a",
            input_value={"role": "user", "content": [{"type": "text", "text": input_text}]},
            alert_id="alert-a",
            case_id="case-a",
            metadata={"client_operation_id": "untrusted"},
            client_operation_id="operation-concurrent",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        admissions = list(pool.map(lambda _index: admit(), range(2)))

    assert {item.run.run_id for item in admissions} == {admissions[0].run.run_id}
    assert sorted(item.should_trigger_upstream for item in admissions) == [False, True]
    assert admissions[0].run.client_operation_id == "operation-concurrent"
    assert "client_operation_id" not in admissions[0].run.metadata
    with pytest.raises(RuntimeStateConflict):
        admit("changed")
    _bind_version(store, version_id="version-b", runtime_agent_id="runtime-b", digest="b" * 64)
    _bind_session(
        store,
        session_id="session-b",
        version_id="version-b",
        runtime_agent_id="runtime-b",
        digest="b" * 64,
    )
    with pytest.raises(RuntimeStateConflict, match="another immutable chat request"):
        store.admit_run(
            session_id="session-b",
            runtime_agent_id="runtime-b",
            input_value={"role": "user", "content": [{"type": "text", "text": "hello"}]},
            alert_id="alert-a",
            case_id="case-a",
            metadata={},
            client_operation_id="operation-concurrent",
        )
    with pytest.raises(RuntimeObjectNotFound):
        store.admit_run(
            session_id="session-a",
            runtime_agent_id="runtime-b",
            input_value={
                "role": "user",
                "content": [],
            },
            alert_id=None,
            case_id=None,
            metadata={},
            client_operation_id="operation-other-runtime",
        )


def test_hitl_continuation_uses_expected_run_without_rebinding_initial_operation(tmp_path) -> None:
    store = _store(tmp_path)
    _bind_version(store, version_id="version-a", runtime_agent_id="runtime-a", digest="a" * 64)
    _bind_session(store)
    run = store.admit_run(
        session_id="session-a",
        runtime_agent_id="runtime-a",
        input_value={"role": "user", "content": []},
        alert_id=None,
        case_id=None,
        metadata={},
        client_operation_id="initial-operation",
    ).run
    store.mark_trigger_started(run.run_id)
    tool_call = {"type": "tool_call", "id": "tool-a", "name": "Read", "input": "{}", "state": "asking"}
    store.apply_receipt(
        RuntimeReceipt(
            receipt_id="receipt-hitl",
            event_id="event-hitl",
            session_id="session-a",
            run_id=run.run_id,
            reply_id="reply-hitl",
            type="REQUIRE_USER_CONFIRM",
            payload={"tool_calls": [tool_call]},
            trace_id=run.trace_id,
        ),
    )

    resumed = store.admit_run(
        session_id="session-a",
        runtime_agent_id="runtime-a",
        input_value={
            "type": "USER_CONFIRM_RESULT",
            "reply_id": "reply-hitl",
            "confirm_results": [{"confirmed": True, "tool_call": tool_call}],
        },
        alert_id=None,
        case_id=None,
        metadata={},
        client_operation_id=f"detached:session-a:{run.run_id}",
        expected_run_id=run.run_id,
    )

    assert resumed.should_trigger_upstream is True
    assert resumed.run.run_id == run.run_id
    assert resumed.run.client_operation_id == "initial-operation"
    assert "client_operation_id" not in resumed.run.metadata
    assert (
        store.run_for_client_operation(
            session_id="session-a",
            client_operation_id="initial-operation",
        ).run_id
        == run.run_id
    )
    with pytest.raises(RuntimeObjectNotFound):
        store.run_for_client_operation(
            session_id="session-a",
            client_operation_id=f"detached:session-a:{run.run_id}",
        )


def test_recovery_quiescent_reset_breaks_the_consecutive_idle_sequence(tmp_path) -> None:
    store = _store(tmp_path)
    _bind_version(store, version_id="version-a", runtime_agent_id="runtime-a", digest="a" * 64)
    _bind_session(store)
    run = store.admit_run(
        session_id="session-a",
        runtime_agent_id="runtime-a",
        input_value={"role": "user", "content": []},
        alert_id=None,
        case_id=None,
        metadata={},
        client_operation_id="recovery-sequence",
    ).run
    store.reconcile_after_restart()

    assert store.note_recovery_quiescent(run.run_id) == 1
    assert store.reset_recovery_quiescent(run.run_id).metadata["recovery_quiescent_observations"] == 0
    assert store.note_recovery_quiescent(run.run_id) == 1


def test_session_list_uses_governance_id_and_aggregates_every_pinned_version(tmp_path) -> None:
    store = _store(tmp_path)
    _bind_version(store, version_id="version-a", runtime_agent_id="runtime-a", digest="a" * 64)
    _bind_version(store, version_id="version-b", runtime_agent_id="runtime-b", digest="b" * 64)
    _bind_session(store)
    _bind_session(
        store,
        session_id="session-b",
        version_id="version-b",
        runtime_agent_id="runtime-b",
        digest="b" * 64,
    )
    runtime = _RuntimeClient()
    runtime.sessions = {
        "runtime-a": [{"session": {"id": "session-a", "agent_id": "runtime-a"}}],
        "runtime-b": [{"session": {"id": "session-b", "agent_id": "runtime-b"}}],
    }
    app, _run_router = _app(store, runtime)

    with TestClient(app) as client:
        listed = client.get("/api/runtime/sessions/?governance_agent_id=agent-a")
        legacy = client.get("/api/runtime/sessions/?agent_id=runtime-a")
        empty = client.get("/api/runtime/sessions/?governance_agent_id=unknown")

    assert listed.status_code == 200 and listed.json()["total"] == 2
    assert {item["session"]["agent_id"] for item in listed.json()["sessions"]} == {"runtime-a", "runtime-b"}
    assert legacy.status_code == 422
    assert empty.status_code == 200 and empty.json() == {"sessions": [], "total": 0}
    queried = [kwargs["params"]["agent_id"] for method, path, kwargs in runtime.calls if (method, path) == ("GET", "/sessions/")]
    assert queried == ["runtime-a", "runtime-b"]


def test_exact_operation_lookup_precedes_dynamic_route_and_enforces_authorization(tmp_path) -> None:
    store = _store(tmp_path)
    _bind_version(store, version_id="version-a", runtime_agent_id="runtime-a", digest="a" * 64)
    _bind_session(store)
    admission = store.admit_run(
        session_id="session-a",
        runtime_agent_id="runtime-a",
        input_value={"role": "user", "content": []},
        alert_id=None,
        case_id=None,
        metadata={},
        client_operation_id="operation-lookup",
    )
    authorized: list[str] = []
    denied = False

    def authorize(run) -> None:
        if denied:
            raise RuntimeObjectNotFound("Run is outside the active Agent generation")
        authorized.append(run.run_id)

    app, run_router = _app(store, _RuntimeClient(), authorize)
    paths = [route.path for route in run_router.routes]
    assert paths.index("/api/agent-runs/by-client-operation") < paths.index("/api/agent-runs/{run_id}")

    with TestClient(app) as client:
        found = client.get(
            "/api/agent-runs/by-client-operation",
            params={"session_id": "session-a", "client_operation_id": "operation-lookup"},
        )
        missing = client.get(
            "/api/agent-runs/by-client-operation",
            params={"session_id": "session-a", "client_operation_id": "operation-missing"},
        )
        denied = True
        forbidden = client.get(
            "/api/agent-runs/by-client-operation",
            params={"session_id": "session-a", "client_operation_id": "operation-lookup"},
        )

    assert found.status_code == 200 and found.json()["run_id"] == admission.run.run_id
    assert found.json()["client_operation_id"] == "operation-lookup"
    assert "client_operation_id" not in found.json()["metadata"]
    assert authorized == [admission.run.run_id]
    assert missing.status_code == 404
    assert forbidden.status_code == 404


def test_exact_operation_lookup_rejects_corrupt_duplicate_rows(tmp_path) -> None:
    store = _store(tmp_path)
    _bind_version(store, version_id="version-a", runtime_agent_id="runtime-a", digest="a" * 64)
    _bind_session(store)
    first = store.admit_run(
        session_id="session-a",
        runtime_agent_id="runtime-a",
        input_value={"role": "user", "content": []},
        alert_id=None,
        case_id=None,
        metadata={},
        client_operation_id="operation-duplicate",
    ).run
    store.fail_trigger(first.run_id, error={"type": "test"})
    with store.Session.begin() as db:
        db.execute(text("DROP INDEX ux_agent_runs_client_operation"))
        db.add(
            AgentRunModel(
                run_id="run-corrupt-duplicate",
                session_id="session-a",
                agent_id="agent-a",
                agent_version_id="version-a",
                runtime_agent_id="runtime-a",
                harness_digest="a" * 64,
                client_operation_id="operation-duplicate",
                input_fingerprint="f" * 64,
                status=RunStatus.FAILED.value,
                metadata_json={},
            ),
        )

    app, _run_router = _app(store, _RuntimeClient())
    with TestClient(app) as client:
        response = client.get(
            "/api/agent-runs/by-client-operation",
            params={"session_id": "session-a", "client_operation_id": "operation-duplicate"},
        )
    assert response.status_code == 409


def test_pending_actions_project_root_and_worker_then_disappear_at_terminal(tmp_path) -> None:
    store = _store(tmp_path)
    _bind_version(store, version_id="version-a", runtime_agent_id="runtime-a", digest="a" * 64)
    _bind_session(store)
    run = store.admit_run(
        session_id="session-a",
        runtime_agent_id="runtime-a",
        input_value={"role": "user", "content": []},
        alert_id=None,
        case_id=None,
        metadata={},
        client_operation_id="operation-pending-actions",
    ).run
    store.mark_trigger_started(run.run_id)
    store.bind_team_child(
        RuntimeChildSessionRegistration(
            run_id=run.run_id,
            parent_session_id="session-a",
            child_session_id="worker-session",
            child_runtime_agent_id="worker-agent",
            team_id="team-1",
        ),
    )

    for suffix, session_id in (("root", "session-a"), ("worker", "worker-session")):
        tool_call = {
            "type": "tool_call",
            "id": f"tool-{suffix}",
            "name": "Read",
            "input": f'{{"file_path":"{suffix}.txt"}}',
            "state": "asking",
            "suggested_rules": [{"tool_name": "Read", "rule_content": "**", "behavior": "allow"}],
            "metadata": {"api_key": "must-not-project"},
        }
        store.apply_receipt(
            RuntimeReceipt(
                receipt_id=f"receipt-{suffix}",
                event_id=f"event-{suffix}",
                session_id=session_id,
                run_id=run.run_id,
                reply_id=f"reply-{suffix}",
                type="REQUIRE_USER_CONFIRM",
                payload={"tool_calls": [tool_call]},
                trace_id=run.trace_id,
            ),
        )

    deny = False

    def authorize(candidate) -> None:
        if deny:
            raise RuntimeObjectNotFound("Run is outside the authorized Agent boundary")
        assert candidate.run_id == run.run_id

    app, _run_router = _app(store, _RuntimeClient(), authorize)
    with TestClient(app) as client:
        first = client.get(f"/api/agent-runs/{run.run_id}/pending-actions")
        repeated = client.get(f"/api/agent-runs/{run.run_id}/pending-actions")
        deny = True
        forbidden = client.get(f"/api/agent-runs/{run.run_id}/pending-actions")
        deny = False
        store.fail_trigger(run.run_id, error={"type": "test"})
        terminal = client.get(f"/api/agent-runs/{run.run_id}/pending-actions")

    assert first.status_code == 200 and first.json() == repeated.json()
    assert {item["session_id"] for item in first.json()} == {"session-a", "worker-session"}
    assert all(set(item) == {"action_id", "session_id", "run_id", "reply_id", "kind", "tool_call", "status", "created_at"} for item in first.json())
    assert all(set(item["tool_call"]) == {"type", "id", "name", "input", "state"} for item in first.json())
    assert forbidden.status_code == 404
    assert terminal.status_code == 200 and terminal.json() == []


def test_session_template_restart_failure_has_specific_safe_error_code(tmp_path) -> None:
    store = _store(tmp_path)
    _bind_version(store, version_id="version-a", runtime_agent_id="runtime-a", digest="a" * 64)
    runtime = _RuntimeClient()
    runtime.restart_required = True
    app, _run_router = _app(store, runtime)

    with TestClient(app) as client:
        response = client.post(
            "/api/runtime/sessions/",
            headers={"Idempotency-Key": "restart-required"},
            json={"agent_id": "runtime-a"},
        )

    assert response.status_code == 503
    assert response.json()["error_code"] == "RUNTIMERESTARTREQUIRED"
    assert "do-not-leak" not in response.text
    intent = store.session_creation_for_key("restart-required")
    assert intent is not None and intent.status == SessionCreationStatus.FAILED_CLEANED.value


def test_session_delete_treats_authorized_upstream_404_as_absent(tmp_path) -> None:
    store = _store(tmp_path)
    _bind_version(store, version_id="version-a", runtime_agent_id="runtime-a", digest="a" * 64)
    _bind_session(store)

    class _MissingSessionClient(_RuntimeClient):
        async def request_json(self, method: str, path: str, **kwargs) -> RuntimeJsonResponse:
            if method == "DELETE" and path == "/sessions/session-a":
                self.calls.append((method, path, kwargs))
                raise RuntimeUpstreamError(404, b'{"detail":"already absent"}')
            return await super().request_json(method, path, **kwargs)

    runtime = _MissingSessionClient()
    app, _run_router = _app(store, runtime)
    with TestClient(app) as client:
        response = client.delete(
            "/api/runtime/sessions/session-a",
            params={"agent_id": "runtime-a"},
        )

    assert response.status_code == 204
    assert response.headers["X-AgentGov-Session-Id"] == "session-a"
    assert store.sessions_for_agent("agent-a") == []
