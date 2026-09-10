from __future__ import annotations

import asyncio
import hashlib
import hmac

import httpx
import pytest
from app.routers.error_handlers import register_error_handlers
from app.runtime.runtime_db import make_session_factory
from app.runtime_gateway.client import AgentScopeRuntimeClient, RuntimeJsonResponse, RuntimeUpstreamError
from app.runtime_gateway.contracts import RuntimeChildSessionRegistration, RuntimeReceipt, RuntimeTeamInboxDelivery
from app.runtime_gateway.provisioning import RuntimeAgentBinding, RuntimeCurrentVersion
from app.runtime_gateway.router import create_agent_run_router, create_runtime_router
from app.runtime_gateway.store import RuntimeObjectNotFound, RuntimeRunStore, SessionCreationStatus
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _Provisioner:
    def __init__(self, store: RuntimeRunStore) -> None:
        self.calls: list[str] = []
        self.store = store

    def require_current_runtime(self, runtime_agent_id: str) -> RuntimeAgentBinding:
        self.calls.append(runtime_agent_id)
        if runtime_agent_id not in {"runtime-a", "runtime-b"}:
            raise RuntimeObjectNotFound(f"Runtime Agent is not provisioned: {runtime_agent_id}")
        agent_id = "agent-b" if runtime_agent_id == "runtime-b" else "agent-a"
        return RuntimeAgentBinding(
            agent_id=agent_id,
            agent_version_id="version-a",
            runtime_agent_id=runtime_agent_id,
            harness_digest="a" * 64,
            workspace_id=f"{agent_id}--v-{'a' * 64}",
            permission_mode="dont_ask",
            cwd="outputs",
            model_profile="default",
        )

    def inspect_current(self, agent_id: str) -> RuntimeCurrentVersion:
        return RuntimeCurrentVersion(agent_id, "version-a", "a" * 64, "runtime-a")

    async def ensure(self, agent_id: str) -> RuntimeAgentBinding:
        return self.require_current_runtime("runtime-a")

    def require_session(self, session_id: str, runtime_agent_id: str):
        return self.store.get_session(session_id, runtime_agent_id=runtime_agent_id)


class _RawResponse:
    status_code = 200
    headers = {
        "content-type": "text/event-stream",
        "cache-control": "no-cache",
        "x-accel-buffering": "no",
        "connection": "keep-alive",
    }

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False

    async def aiter_raw(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class _RuntimeClient:
    def __init__(self, *, fail_patch: bool = False) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.fail_patch = fail_patch
        self.session_counter = 0
        self.raw_response = _RawResponse(
            [
                b'data: {"id":"1","type":"REPLY_START"}\n\n',
                b": heartbeat\n\n",
                b'data: {"future":"unknown","type":"NEW_EVENT"}\n\n',
            ]
        )

    async def request_json(self, method: str, path: str, **kwargs):
        self.calls.append((method, path, kwargs))
        if method == "POST" and path == "/sessions/":
            self.session_counter += 1
            return RuntimeJsonResponse(
                201,
                {"content-type": "application/json", "connection": "close"},
                {"session_id": f"upstream-{self.session_counter}"},
            )
        if method == "PATCH" and path.startswith("/sessions/"):
            if self.fail_patch:
                raise RuntimeUpstreamError(502, b'{"detail":"patch failed"}')
            return RuntimeJsonResponse(200, {"content-type": "application/json"}, {"id": path.rsplit("/", 1)[-1]})
        if method == "DELETE" and path.startswith("/sessions/"):
            return RuntimeJsonResponse(204, {}, None)
        return RuntimeJsonResponse(200, {"content-type": "application/json"}, {"status": "ok"})

    async def start_stream(self, path: str, **kwargs):
        self.calls.append(("GET_STREAM", path, kwargs))
        return self.raw_response


def _setup(tmp_path, *, runtime_client: _RuntimeClient | None = None):
    store = RuntimeRunStore(make_session_factory(tmp_path / "runtime.db"))
    store.bind_agent_version(
        agent_id="agent-a",
        agent_version_id="version-a",
        digest="a" * 64,
        runtime_agent_id="runtime-a",
    )
    upstream = runtime_client or _RuntimeClient()
    provisioner = _Provisioner(store)
    router = create_runtime_router(
        client=upstream,  # type: ignore[arg-type]
        store=store,
        provisioner=provisioner,  # type: ignore[arg-type]
        model_type="openai_credential",
        credential_id="agentgov-runtime-provider",
        model_name="deepseek-chat",
        model_parameters={"temperature": 0},
        require_api_key=lambda: None,
    )
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(router)
    return app, router, store, upstream, provisioner


def test_session_creation_uses_only_governed_runtime_configuration_and_is_idempotent(tmp_path) -> None:
    app, _, store, upstream, provisioner = _setup(tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/api/runtime/sessions/",
            headers={"Idempotency-Key": "create-one"},
            json={"agent_id": "runtime-a", "name": "First"},
        )
        assert response.status_code == 201
        assert response.json() == {"session_id": "upstream-1"}
        assert response.headers["X-AgentGov-Session-Id"] == "upstream-1"

        repeated = client.post(
            "/api/runtime/sessions/",
            headers={"Idempotency-Key": "create-one"},
            json={"agent_id": "runtime-a", "name": "Ignored retry name"},
        )
        assert repeated.status_code == 200
        assert repeated.json() == {"session_id": "upstream-1"}

        injected = client.post(
            "/api/runtime/sessions/",
            json={"agent_id": "runtime-a", "model": "attacker-model"},
        )
        assert injected.status_code == 422

    assert provisioner.calls == ["runtime-a"]
    post = next(call for call in upstream.calls if call[:2] == ("POST", "/sessions/"))
    intent = store.session_creation_for_key("create-one")
    assert intent is not None
    assert post[2]["json"] == {
        "agent_id": "runtime-a",
        "workspace_id": intent.workspace_id,
        "name": "First",
        "chat_model_config": {
            "type": "openai_credential",
            "credential_id": "agentgov-runtime-provider",
            "model": "deepseek-chat",
            "parameters": {"temperature": 0},
        },
    }
    patch_call = next(call for call in upstream.calls if call[0] == "PATCH")
    assert patch_call[2]["json"] == {"permission_mode": "dont_ask", "cwd": "outputs"}
    assert intent.status == SessionCreationStatus.BOUND.value
    assert intent.session_id == "upstream-1"


def test_current_version_read_and_explicit_provision_expose_typed_tuple(tmp_path) -> None:
    app, _, _, _, _ = _setup(tmp_path)

    with TestClient(app) as client:
        current = client.get("/api/runtime/agents/agent-a/current")
        provisioned = client.post("/api/runtime/agents/agent-a/provision")

    expected = {
        "governance_agent_id": "agent-a",
        "agent_version_id": "version-a",
        "harness_digest": "a" * 64,
        "runtime_agent_id": "runtime-a",
        "provisioned": True,
    }
    assert current.status_code == 200 and current.json() == expected
    assert provisioned.status_code == 200 and provisioned.json() == expected


def test_idempotency_key_cannot_cross_agent_boundary(tmp_path) -> None:
    app, _, _, _, _ = _setup(tmp_path)
    with TestClient(app) as client:
        assert (
            client.post(
                "/api/runtime/sessions/",
                headers={"Idempotency-Key": "same-key"},
                json={"agent_id": "runtime-a"},
            ).status_code
            == 201
        )
        conflict = client.post(
            "/api/runtime/sessions/",
            headers={"Idempotency-Key": "same-key"},
            json={"agent_id": "runtime-b"},
        )
    assert conflict.status_code == 409
    assert "another Runtime Agent" in conflict.json()["detail"]


def test_session_creation_rejects_stable_agentgov_id_without_lazy_provision(tmp_path) -> None:
    app, _, _, upstream, provisioner = _setup(tmp_path)

    with TestClient(app) as client:
        response = client.post(
            "/api/runtime/sessions/",
            headers={"Idempotency-Key": "wrong-id-domain"},
            json={"agent_id": "agent-a"},
        )

    assert response.status_code == 404
    assert provisioner.calls == ["agent-a"]
    assert upstream.calls == []


def test_failed_governed_session_patch_compensates_upstream_session(tmp_path) -> None:
    upstream = _RuntimeClient(fail_patch=True)
    app, _, store, _, _ = _setup(tmp_path, runtime_client=upstream)
    with TestClient(app) as client:
        response = client.post(
            "/api/runtime/sessions/",
            headers={"Idempotency-Key": "failed-create"},
            json={"agent_id": "runtime-a"},
        )
    assert response.status_code == 502
    assert [(method, path) for method, path, _ in upstream.calls] == [
        ("POST", "/sessions/"),
        ("PATCH", "/sessions/upstream-1"),
        ("DELETE", "/sessions/upstream-1"),
    ]
    assert store.sessions_for_agent("agent-a") == []
    intent = store.session_creation_for_key("failed-create")
    assert intent is not None
    assert intent.status == SessionCreationStatus.FAILED_CLEANED.value
    assert intent.session_id == "upstream-1"
    assert intent.cleanup_attempts == 1
    assert intent.error_json["stage"] == "post_create_finalize"


def test_sse_proxy_preserves_raw_frames_and_unknown_events(tmp_path) -> None:
    _, router, store, upstream, _ = _setup(tmp_path)
    store.bind_session(
        session_id="session-a",
        agent_id="agent-a",
        agent_version_id="version-a",
        runtime_agent_id="runtime-a",
        digest="a" * 64,
        idempotency_key=None,
    )
    route = next(route for route in router.routes if getattr(route, "path", "") == "/api/runtime/sessions/{session_id}/stream")

    async def collect() -> bytes:
        response = await route.endpoint("session-a", "runtime-a")
        return b"".join([chunk async for chunk in response.body_iterator])

    raw = asyncio.run(collect())
    assert raw == b"".join(upstream.raw_response.chunks)
    assert upstream.raw_response.closed is True
    stream_call = upstream.calls[-1]
    assert stream_call == ("GET_STREAM", "/sessions/session-a/stream", {"params": {"agent_id": "runtime-a"}})


def test_session_data_plane_rejects_cross_agent_scope(tmp_path) -> None:
    app, _, store, upstream, _ = _setup(tmp_path)
    store.bind_session(
        session_id="session-a",
        agent_id="agent-a",
        agent_version_id="version-a",
        runtime_agent_id="runtime-a",
        digest="a" * 64,
        idempotency_key=None,
    )
    with TestClient(app) as client:
        for path in (
            "/api/runtime/sessions/session-a/messages?agent_id=runtime-b",
            "/api/runtime/sessions/session-a/status?agent_id=runtime-b",
        ):
            response = client.get(path)
            assert response.status_code == 404
        interrupt = client.post("/api/runtime/sessions/session-a/interrupt?agent_id=runtime-b")
        delete = client.delete("/api/runtime/sessions/session-a?agent_id=runtime-b")
    assert interrupt.status_code == 404
    assert delete.status_code == 404
    assert upstream.calls == []


def test_session_interrupt_cancels_root_and_every_team_child(tmp_path) -> None:
    app, _, store, upstream, _ = _setup(tmp_path)
    store.bind_session(
        session_id="leader-session",
        agent_id="agent-a",
        agent_version_id="version-a",
        runtime_agent_id="runtime-a",
        digest="a" * 64,
        idempotency_key=None,
    )
    run = store.begin_run(
        session_id="leader-session",
        runtime_agent_id="runtime-a",
        input_value={"role": "user", "content": []},
        alert_id=None,
        case_id=None,
        metadata={},
    )
    store.mark_trigger_started(run.run_id)
    store.bind_team_child(
        RuntimeChildSessionRegistration(
            run_id=run.run_id,
            parent_session_id="leader-session",
            child_session_id="worker-session",
            child_runtime_agent_id="worker-agent",
            team_id="team-1",
        ),
    )
    store.record_team_inbox_delivery(
        RuntimeTeamInboxDelivery(
            event_id="cancel-worker-delivery",
            run_id=run.run_id,
            source_session_id="leader-session",
            target_session_id="worker-session",
        ),
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/runtime/sessions/leader-session/interrupt?agent_id=runtime-a",
        )

    assert response.status_code == 200
    interrupt_paths = [path for method, path, _kwargs in upstream.calls if method == "POST" and path.endswith("/interrupt")]
    assert interrupt_paths == [
        "/sessions/leader-session/interrupt",
        "/sessions/worker-session/interrupt",
    ]
    assert store.get_run(run.run_id).metadata["cancellation_requested"] is True


def test_agent_run_cancel_partial_failure_keeps_all_team_fences_and_redacts(
    tmp_path,
) -> None:
    store = RuntimeRunStore(make_session_factory(tmp_path / "runtime.db"))
    store.bind_agent_version(
        agent_id="agent-a",
        agent_version_id="version-a",
        digest="a" * 64,
        runtime_agent_id="runtime-a",
    )
    store.bind_session(
        session_id="leader-session",
        agent_id="agent-a",
        agent_version_id="version-a",
        runtime_agent_id="runtime-a",
        digest="a" * 64,
    )
    run = store.begin_run(
        session_id="leader-session",
        runtime_agent_id="runtime-a",
        input_value={"role": "user", "content": []},
        alert_id=None,
        case_id=None,
        metadata={},
    )
    store.mark_trigger_started(run.run_id)
    store.bind_team_child(
        RuntimeChildSessionRegistration(
            run_id=run.run_id,
            parent_session_id="leader-session",
            child_session_id="worker-session",
            child_runtime_agent_id="worker-agent",
            team_id="team-1",
        ),
    )

    class _PartialFailureClient(_RuntimeClient):
        async def request_json(self, method: str, path: str, **kwargs):
            self.calls.append((method, path, kwargs))
            if "worker-session" in path:
                raise RuntimeUpstreamError(
                    503,
                    b'{"detail":"provider-test-secret"}',
                )
            return RuntimeJsonResponse(202, {}, {"status": "interrupting"})

    upstream = _PartialFailureClient()
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(
        create_agent_run_router(
            client=upstream,  # type: ignore[arg-type]
            store=store,
            trace_fetcher=lambda _: None,
            authorize_run=lambda _run: None,
            require_api_key=lambda: None,
        ),
    )

    with TestClient(app) as client:
        response = client.post(f"/api/agent-runs/{run.run_id}/cancel")

    assert response.status_code == 503
    assert response.json() == {
        "detail": "AgentScope Runtime request failed",
        "error_code": "RUNTIME_UPSTREAM_ERROR",
    }
    assert "provider-test-secret" not in response.text
    assert [path for method, path, _kwargs in upstream.calls if method == "POST"] == [
        "/sessions/leader-session/interrupt",
        "/sessions/worker-session/interrupt",
    ]
    active = store.get_run(run.run_id)
    assert active.metadata["recovery_required"] is True
    assert active.metadata["cancellation_uncertain"] is True
    assert active.error == {"type": "RuntimeUpstreamError"}
    assert store.get_session("leader-session").active_run_id == run.run_id
    assert store.get_session("worker-session").active_run_id == run.run_id


def test_confirmation_scope_run_is_rejected_for_non_confirmation_input(tmp_path) -> None:
    app, _, _, upstream, _ = _setup(tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/api/runtime/chat/",
            json={
                "agent_id": "runtime-a",
                "session_id": "session-a",
                "client_operation_id": "validation-scope",
                "input": {"role": "user", "content": []},
                "confirmation_scope": "run",
            },
        )
    assert response.status_code == 422
    assert "only applies to USER_CONFIRM_RESULT" in response.text
    assert upstream.calls == []


def test_hitl_continuation_requires_expected_run_id(tmp_path) -> None:
    app, _, _, upstream, _ = _setup(tmp_path)

    with TestClient(app) as client:
        response = client.post(
            "/api/runtime/chat/",
            json={
                "agent_id": "runtime-a",
                "session_id": "session-a",
                "client_operation_id": "validation-hitl",
                "input": {"type": "USER_CONFIRM_RESULT", "reply_id": "reply-a", "confirm_results": []},
            },
        )

    assert response.status_code == 422
    assert "expected_run_id is required" in response.text
    assert upstream.calls == []


def test_runtime_client_normalizes_transport_failure_without_leaking_target() -> None:
    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private-host.example refused secret=abc", request=request)

    async def call() -> None:
        async with httpx.AsyncClient(
            base_url="http://private-host.example",
            transport=httpx.MockTransport(unavailable),
        ) as transport_client:
            runtime_client = AgentScopeRuntimeClient(
                "http://private-host.example",
                shared_secret="shared-test-secret",
                client=transport_client,
            )
            with pytest.raises(RuntimeUpstreamError) as caught:
                await runtime_client.request_json("GET", "/health")
        assert caught.value.status_code == 503
        assert caught.value.body == b'{"detail":"AgentScope Runtime unavailable","error_code":"RUNTIME_UNAVAILABLE"}'

    asyncio.run(call())


def test_runtime_client_signature_binds_encoded_query_and_internal_user() -> None:
    observed: list[httpx.Request] = []

    def inspect_request(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(200, json={"status": "ok"})

    async def call() -> None:
        async with httpx.AsyncClient(
            base_url="http://runtime.test",
            transport=httpx.MockTransport(inspect_request),
        ) as transport_client:
            runtime_client = AgentScopeRuntimeClient(
                "http://runtime.test",
                shared_secret="shared-test-secret",
                client=transport_client,
            )
            await runtime_client.request_json(
                "GET",
                "/sessions/session-1/status",
                params={"agent_id": "agent id/one"},
            )

    asyncio.run(call())
    request = observed[0]
    timestamp = request.headers["X-AgentGov-Timestamp"]
    raw_target = request.url.raw_path
    expected = hmac.new(
        b"shared-test-secret",
        b"\n".join(
            (
                timestamp.encode("ascii"),
                b"agentgov-runtime",
                b"GET",
                raw_target,
                b"",
            ),
        ),
        hashlib.sha256,
    ).hexdigest()
    assert raw_target == b"/sessions/session-1/status?agent_id=agent+id%2Fone"
    assert request.headers["X-AgentGov-Signature"] == expected


def _terminal_run(store: RuntimeRunStore):
    store.bind_session(
        session_id="trace-session",
        agent_id="agent-a",
        agent_version_id="version-a",
        runtime_agent_id="runtime-a",
        digest="a" * 64,
    )
    run = store.begin_run(
        session_id="trace-session",
        runtime_agent_id="runtime-a",
        input_value={"role": "user", "content": []},
        alert_id=None,
        case_id=None,
        metadata={},
    )
    store.mark_trigger_started(run.run_id)
    for index, (event_type, payload) in enumerate(
        (
            ("REPLY_END", {"finished_reason": "completed"}),
            ("MESSAGE_PERSISTED", {"message_persisted": True, "finished_reason": "completed"}),
            (
                "SESSION_PERSISTED",
                {"reply_ids": ["trace-reply"], "message_count": 1, "team_generation": 0},
            ),
        ),
    ):
        store.apply_receipt(
            RuntimeReceipt(
                receipt_id=f"trace-receipt-{index}",
                event_id=f"trace-event-{index}",
                session_id=run.session_id,
                run_id=run.run_id,
                reply_id=None if event_type == "SESSION_PERSISTED" else "trace-reply",
                type=event_type,
                payload=payload,
                trace_id=run.trace_id,
            ),
        )
    return store.get_run(run.run_id)


def _complete_trace_for_run(run) -> dict[str, object]:
    ended = "2026-09-09T00:00:00Z"
    return {
        "id": run.trace_id,
        "url": "https://langfuse.example/run",
        "observations": [
            {
                "id": "root-span",
                "name": "agentgov.run",
                "trace_id": run.trace_id,
                "parent_observation_id": None,
                "end_time": ended,
                "attributes": {
                    "agentgov.run.id": run.run_id,
                    "agentgov.agent.id": run.agent_id,
                    "agentgov.agent.version_id": run.agent_version_id,
                    "agentgov.harness.digest": run.harness_digest,
                    "agentgov.runtime.version": "v1",
                    "agentscope.agent.id": run.runtime_agent_id,
                    "agentscope.runtime.version": "2.0.8",
                    "agentscope.session.id": run.session_id,
                    "agentgov.run.finished_reason": run.terminal_reason,
                },
            },
            {
                "id": "stage-span",
                "name": "agentgov.run.stage",
                "trace_id": run.trace_id,
                "parent_observation_id": "root-span",
                "end_time": ended,
                "attributes": {
                    "agentscope.agent.id": run.runtime_agent_id,
                    "agentscope.session.id": run.session_id,
                    "agentscope.agent.reply_id": run.reply_ids[0],
                },
            },
            {
                "id": "invoke-span",
                "name": "invoke_agent",
                "trace_id": run.trace_id,
                "parent_observation_id": "stage-span",
                "end_time": ended,
                "attributes": {
                    "gen_ai.conversation.id": run.session_id,
                    "agentscope.agent.reply_id": run.reply_ids[0],
                    "agentgov.content.input.length": 5,
                    "agentgov.content.input.sha256": "b" * 64,
                },
            },
            {
                "id": "chat-span",
                "name": "chat",
                "trace_id": run.trace_id,
                "parent_observation_id": "invoke-span",
                "end_time": ended,
                "attributes": {
                    "gen_ai.conversation.id": run.session_id,
                    "gen_ai.request.model": "model-1",
                    "gen_ai.provider.name": "provider-1",
                    "agentgov.content.output.length": 7,
                    "agentgov.content.output.sha256": "c" * 64,
                },
            },
        ],
    }


@pytest.mark.parametrize(
    "trace",
    [
        {"id": "TRACE_ID", "name": "agentgov.run"},
        {"fetch_status": "failed", "error": "not ready"},
        {
            "id": "TRACE_ID",
            "observations": [{"name": "chat", "end_time": "2026-09-09T00:00:00Z"}],
        },
        {
            "id": "TRACE_ID",
            "observations": [{"name": "agentgov.run", "end_time": None}],
        },
        {
            "id": "TRACE_ID",
            "observations": [
                {"name": "invoke_agent", "end_time": "2026-09-09T00:00:00Z"},
                {"name": "chat", "end_time": "2026-09-09T00:00:00Z"},
                {
                    "name": "agentgov.run",
                    "end_time": "2026-09-09T00:00:00Z",
                    "attributes": {
                        "agentgov.run.id": "wrong-run",
                        "agentgov.run.finished_reason": "completed",
                    },
                },
            ],
        },
        {
            "id": "TRACE_ID",
            "observations": [
                {"name": "invoke_agent", "end_time": "2026-09-09T00:00:00Z"},
                {"name": "chat", "end_time": "2026-09-09T00:00:00Z"},
                {
                    "name": "agentgov.run",
                    "end_time": "2026-09-09T00:00:00Z",
                    "attributes": {
                        "agentgov.run.id": "RUN_ID",
                        "agentgov.run.finished_reason": "interrupted",
                    },
                },
            ],
        },
    ],
    ids=["core-only", "fetch-failed", "no-root", "root-not-ended", "wrong-run-root", "old-stage-reason"],
)
def test_trace_stays_pending_until_finished_agentgov_run_root_is_observed(tmp_path, trace) -> None:
    store = RuntimeRunStore(make_session_factory(tmp_path / "runtime.db"))
    store.bind_agent_version(
        agent_id="agent-a",
        agent_version_id="version-a",
        digest="a" * 64,
        runtime_agent_id="runtime-a",
    )
    run = _terminal_run(store)
    resolved_trace = _replace_trace_id(trace, run.trace_id)
    for observation in resolved_trace.get("observations", []):
        if not isinstance(observation, dict):
            continue
        attributes = observation.get("attributes")
        if isinstance(attributes, dict) and attributes.get("agentgov.run.id") == "RUN_ID":
            attributes["agentgov.run.id"] = run.run_id
    app = FastAPI()
    app.include_router(
        create_agent_run_router(
            client=_RuntimeClient(),  # type: ignore[arg-type]
            store=store,
            trace_fetcher=lambda _trace_id: resolved_trace,
            authorize_run=lambda _run: None,
            require_api_key=lambda: None,
        ),
    )
    with TestClient(app) as client:
        response = client.get(f"/api/agent-runs/{run.run_id}/trace")
    assert response.status_code == 200
    assert response.json()["trace_status"] == "pending"
    assert store.get_run(run.run_id).trace_status == "pending"


def test_trace_completes_only_after_matching_finished_agentgov_run_root(tmp_path) -> None:
    store = RuntimeRunStore(make_session_factory(tmp_path / "runtime.db"))
    store.bind_agent_version(
        agent_id="agent-a",
        agent_version_id="version-a",
        digest="a" * 64,
        runtime_agent_id="runtime-a",
    )
    run = _terminal_run(store)
    trace = _complete_trace_for_run(run)
    app = FastAPI()
    app.include_router(
        create_agent_run_router(
            client=_RuntimeClient(),  # type: ignore[arg-type]
            store=store,
            trace_fetcher=lambda _trace_id: trace,
            authorize_run=lambda _run: None,
            require_api_key=lambda: None,
        ),
    )
    with TestClient(app) as client:
        response = client.get(f"/api/agent-runs/{run.run_id}/trace")
    assert response.status_code == 200
    assert response.json()["trace_status"] == "complete"
    assert response.json()["trace_url"] == "https://langfuse.example/run"


def test_old_run_cannot_interrupt_a_new_active_run_on_the_same_session(tmp_path) -> None:
    store = RuntimeRunStore(make_session_factory(tmp_path / "runtime.db"))
    store.bind_agent_version(
        agent_id="agent-a",
        agent_version_id="version-a",
        digest="a" * 64,
        runtime_agent_id="runtime-a",
    )
    old_run = _terminal_run(store)
    new_run = store.begin_run(
        session_id=old_run.session_id,
        runtime_agent_id="runtime-a",
        input_value={"role": "user", "content": []},
        alert_id=None,
        case_id=None,
        metadata={},
    )
    upstream = _RuntimeClient()
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(
        create_agent_run_router(
            client=upstream,  # type: ignore[arg-type]
            store=store,
            trace_fetcher=lambda _: None,
            authorize_run=lambda _run: None,
            require_api_key=lambda: None,
        ),
    )

    with TestClient(app) as client:
        response = client.post(f"/api/agent-runs/{old_run.run_id}/cancel")

    assert response.status_code == 409
    active = store.active_run_for_session(old_run.session_id)
    assert active is not None and active.run_id == new_run.run_id
    assert not any(path.endswith("/interrupt") for _, path, _ in upstream.calls)


def _replace_trace_id(value, trace_id: str | None):
    if isinstance(value, dict):
        return {key: _replace_trace_id(item, trace_id) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_trace_id(item, trace_id) for item in value]
    return trace_id if value == "TRACE_ID" else value
