from __future__ import annotations

import asyncio
import itertools

import httpx
import pytest
from app.routers.error_handlers import register_error_handlers
from app.runtime.runtime_db import make_session_factory
from app.runtime_gateway.client import RuntimeJsonResponse, RuntimeUpstreamError
from app.runtime_gateway.contracts import (
    RuntimeChildSessionRegistration,
    RuntimeReceipt,
    RuntimeTeamInboxDelivery,
)
from app.runtime_gateway.models import RuntimePendingActionModel
from app.runtime_gateway.provisioning import RuntimeAgentBinding
from app.runtime_gateway.router import create_runtime_router, reconcile_runtime_gateway
from app.runtime_gateway.store import RuntimeRunStore, RuntimeStateConflict, SessionCreationStatus
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select


class _Provisioner:
    calls: list[str]

    def __init__(self, store: RuntimeRunStore) -> None:
        self.calls = []
        self.store = store

    def require_current_runtime(self, runtime_agent_id: str) -> RuntimeAgentBinding:
        self.calls.append(runtime_agent_id)
        return RuntimeAgentBinding(
            agent_id="agent-a",
            agent_version_id="version-a",
            runtime_agent_id=runtime_agent_id,
            harness_digest="a" * 64,
            workspace_id=f"agent-a--v-{'a' * 64}",
            permission_mode="dont_ask",
            cwd="outputs",
            model_profile="default",
        )

    def require_session(self, session_id: str, runtime_agent_id: str):
        return self.store.get_session(session_id, runtime_agent_id=runtime_agent_id)


class _CreationClient:
    def __init__(self, store: RuntimeRunStore) -> None:
        self.store = store
        self.calls: list[tuple[str, str]] = []
        self.session_creations = 0

    async def request_json(self, method: str, path: str, **kwargs) -> RuntimeJsonResponse:
        self.calls.append((method, path))
        intent = self.store.session_creation_for_key("concurrent-create")
        if method == "POST":
            assert intent is not None
            assert intent.status == SessionCreationStatus.PENDING.value
            assert intent.session_id is None
            self.session_creations += 1
            await asyncio.sleep(0.05)
            return RuntimeJsonResponse(201, {"content-type": "application/json"}, {"session_id": "runtime-session-a"})
        if method == "PATCH":
            assert intent is not None
            assert intent.status == SessionCreationStatus.UPSTREAM_CREATED.value
            assert intent.session_id == "runtime-session-a"
            return RuntimeJsonResponse(200, {}, {"session_id": "runtime-session-a"})
        if method == "DELETE":
            return RuntimeJsonResponse(204, {}, None)
        raise AssertionError(f"unexpected request: {method} {path} {kwargs}")


class _RecoveryClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.sessions: list[object] = []
        self.messages: list[object] = []
        self.delete_returns_404 = False
        self.messages_return_404 = False
        self.interrupt_returns_404 = False
        self.status = "idle"

    async def request_json(self, method: str, path: str, **kwargs) -> RuntimeJsonResponse:
        self.calls.append((method, path))
        if method == "GET" and path == "/sessions/":
            return RuntimeJsonResponse(200, {}, {"sessions": self.sessions, "total": len(self.sessions)})
        if method == "GET" and path.endswith("/messages"):
            if self.messages_return_404:
                raise RuntimeUpstreamError(404, b'{"detail":"not found"}')
            return RuntimeJsonResponse(200, {}, {"messages": self.messages, "is_running": False, "has_more": False})
        if method == "GET" and path.endswith("/status"):
            return RuntimeJsonResponse(200, {}, {"session_id": path.split("/")[-2], "status": self.status})
        if method == "POST" and path.endswith("/interrupt"):
            if self.interrupt_returns_404:
                raise RuntimeUpstreamError(404, b'{"detail":"not found"}')
            return RuntimeJsonResponse(202, {}, {"session_id": path.split("/")[-2]})
        if method == "DELETE":
            if self.delete_returns_404:
                raise RuntimeUpstreamError(404, b'{"detail":"not found"}')
            return RuntimeJsonResponse(204, {}, None)
        raise AssertionError(f"unexpected request: {method} {path} {kwargs}")


class _AcceptedButResponseLostClient(_RecoveryClient):
    """模拟 Runtime 已受理 chat，但网关只观察到 5xx/响应丢失。"""

    def __init__(self, *, status_code: int = 503) -> None:
        super().__init__()
        self.chat_attempts = 0
        self.status_code = status_code

    async def request_json(self, method: str, path: str, **kwargs) -> RuntimeJsonResponse:
        if method == "POST" and path == "/chat/":
            self.calls.append((method, path))
            self.chat_attempts += 1
            raise RuntimeUpstreamError(
                self.status_code,
                b'{"detail":"accepted, but response lost: provider-test-secret"}',
            )
        return await super().request_json(method, path, **kwargs)


def _store(tmp_path) -> RuntimeRunStore:
    store = RuntimeRunStore(make_session_factory(tmp_path / "runtime.db"))
    store.bind_agent_version(
        agent_id="agent-a",
        agent_version_id="version-a",
        digest="a" * 64,
        runtime_agent_id="runtime-a",
    )
    return store


def _creation_app(store: RuntimeRunStore, runtime_client: _CreationClient, provisioner: _Provisioner) -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(
        create_runtime_router(
            client=runtime_client,  # type: ignore[arg-type]
            store=store,
            provisioner=provisioner,  # type: ignore[arg-type]
            model_type="openai_credential",
            credential_id="agentgov-runtime-provider",
            model_name="deepseek-chat",
            model_parameters={"temperature": 0},
            require_api_key=lambda: None,
        ),
    )
    return app


def test_concurrent_idempotent_creation_persists_intent_before_upstream(tmp_path) -> None:
    store = _store(tmp_path)
    runtime_client = _CreationClient(store)
    provisioner = _Provisioner(store)
    app = _creation_app(store, runtime_client, provisioner)

    async def invoke() -> list[httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:

            async def request() -> httpx.Response:
                return await client.post(
                    "/api/runtime/sessions/",
                    headers={"Idempotency-Key": "concurrent-create"},
                    json={"agent_id": "runtime-a", "name": "Concurrent"},
                )

            return list(await asyncio.gather(request(), request()))

    responses = asyncio.run(invoke())
    assert sorted(response.status_code for response in responses) == [200, 201]
    assert {response.json()["session_id"] for response in responses} == {"runtime-session-a"}
    assert runtime_client.session_creations == 1
    assert provisioner.calls == ["runtime-a"]
    intent = store.session_creation_for_key("concurrent-create")
    assert intent is not None
    assert intent.status == SessionCreationStatus.BOUND.value
    assert store.get_session("runtime-session-a").runtime_agent_id == "runtime-a"


def test_startup_recovery_discovers_and_cleans_unbound_session_with_404_idempotency(tmp_path) -> None:
    store = _store(tmp_path)
    intent, owned = store.start_session_creation(
        idempotency_key="orphan-create",
        agent_id="agent-a",
        agent_version_id="version-a",
        runtime_agent_id="runtime-a",
        digest="a" * 64,
        workspace_id=f"agent-a--v-{'a' * 64}",
        session_name="Orphan",
    )
    assert owned is True
    duplicate, duplicate_owned = store.start_session_creation(
        idempotency_key="orphan-create",
        agent_id="agent-a",
        agent_version_id="version-a",
        runtime_agent_id="runtime-a",
        digest="a" * 64,
        workspace_id=f"agent-a--v-{'a' * 64}",
        session_name="Ignored",
    )
    assert duplicate.intent_id == intent.intent_id
    assert duplicate_owned is False

    runtime_client = _RecoveryClient()
    runtime_client.sessions = [
        {
            "session": {
                "id": "runtime-orphan",
                "config": {"workspace_id": intent.workspace_id},
            },
        },
    ]
    runtime_client.delete_returns_404 = True
    periodic = asyncio.run(reconcile_runtime_gateway(client=runtime_client, store=store))  # type: ignore[arg-type]
    assert periodic.session_intents_cleaned == 0
    assert runtime_client.calls == []

    recovered = asyncio.run(
        reconcile_runtime_gateway(client=runtime_client, store=store, include_fresh_intents=True),  # type: ignore[arg-type]
    )
    assert recovered.session_intents_cleaned == 1
    assert runtime_client.calls == [("GET", "/sessions/"), ("DELETE", "/sessions/runtime-orphan")]
    audited = store.session_creation_for_key("orphan-create")
    assert audited is not None
    assert audited.status == SessionCreationStatus.FAILED_CLEANED.value
    assert audited.session_id == "runtime-orphan"
    assert audited.cleanup_attempts == 1


_RECEIPT_IDS = itertools.count()


def _receipt(run, event_type: str, payload: dict[str, object]) -> RuntimeReceipt:
    suffix = next(_RECEIPT_IDS)
    return RuntimeReceipt(
        receipt_id=f"receipt-recovery-{suffix}",
        event_id=f"event-recovery-{suffix}",
        session_id=run.session_id,
        run_id=run.run_id,
        reply_id="reply-recovery",
        type=event_type,
        payload=payload,
        trace_id=run.trace_id,
    )


def _finalizing_run(store: RuntimeRunStore):
    store.bind_session(
        session_id="session-a",
        agent_id="agent-a",
        agent_version_id="version-a",
        runtime_agent_id="runtime-a",
        digest="a" * 64,
    )
    run = store.begin_run(
        session_id="session-a",
        runtime_agent_id="runtime-a",
        input_value={"role": "user", "content": []},
        alert_id=None,
        case_id=None,
        metadata={},
    )
    store.mark_trigger_started(run.run_id)
    store.apply_receipt(_receipt(run, "REPLY_START", {}))
    return store.apply_receipt(_receipt(run, "REPLY_END", {"finished_reason": "completed"}))


def test_periodic_recovery_finalizes_from_canonical_message_and_fails_closed_on_404(tmp_path) -> None:
    store = _store(tmp_path)
    run = _finalizing_run(store)
    runtime_client = _RecoveryClient()
    runtime_client.messages = [
        {
            "id": "reply-recovery",
            "name": "assistant",
            "role": "assistant",
            "content": [],
            "finished_reason": "completed",
            "error": None,
        },
    ]
    report = asyncio.run(reconcile_runtime_gateway(client=runtime_client, store=store))  # type: ignore[arg-type]
    assert report.runs_finalized == 1
    assert store.get_run(run.run_id).status.value == "succeeded"

    second_store = _store(tmp_path / "missing")
    missing = _finalizing_run(second_store)
    missing_client = _RecoveryClient()
    missing_client.messages_return_404 = True
    report = asyncio.run(reconcile_runtime_gateway(client=missing_client, store=second_store))  # type: ignore[arg-type]
    assert report.runs_finalized == 1
    terminal = second_store.get_run(missing.run_id)
    assert terminal.status.value == "interrupted"
    assert terminal.terminal_reason == "observation_incomplete"
    assert second_store.active_run_for_session("session-a") is None


def test_restart_recovery_fails_closed_even_when_canonical_batch_is_readable(tmp_path) -> None:
    store = _store(tmp_path)
    run = _finalizing_run(store)
    assert store.reconcile_after_restart() == [run.run_id]
    runtime_client = _RecoveryClient()
    runtime_client.messages = [
        {
            "id": "reply-recovery",
            "name": "assistant",
            "role": "assistant",
            "content": [],
            "finished_reason": "completed",
            "error": None,
        },
    ]

    first = asyncio.run(reconcile_runtime_gateway(client=runtime_client, store=store))  # type: ignore[arg-type]
    assert first.runs_finalized == 0
    assert store.get_run(run.run_id).status.value == "finalizing"
    assert runtime_client.calls == [("POST", "/sessions/session-a/interrupt")]

    second = asyncio.run(reconcile_runtime_gateway(client=runtime_client, store=store))  # type: ignore[arg-type]
    assert second.runs_finalized == 0
    assert store.get_run(run.run_id).status.value == "finalizing"

    third = asyncio.run(reconcile_runtime_gateway(client=runtime_client, store=store))  # type: ignore[arg-type]
    assert third.runs_finalized == 1
    terminal = store.get_run(run.run_id)
    assert terminal.status.value == "interrupted"
    assert terminal.persisted_reply_ids == []
    assert terminal.persistence_batch_reply_ids == []
    assert runtime_client.calls[-2:] == [
        ("GET", "/sessions/session-a/status"),
        ("GET", "/sessions/session-a/status"),
    ]
    assert not any(path.endswith("/messages") for _method, path in runtime_client.calls)


def test_restart_recovery_releases_fence_only_after_two_idle_incomplete_probes(tmp_path) -> None:
    store = _store(tmp_path)
    store.bind_session(
        session_id="session-a",
        agent_id="agent-a",
        agent_version_id="version-a",
        runtime_agent_id="runtime-a",
        digest="a" * 64,
    )
    run = store.begin_run(
        session_id="session-a",
        runtime_agent_id="runtime-a",
        input_value={"role": "user", "content": []},
        alert_id=None,
        case_id=None,
        metadata={},
    )
    store.mark_trigger_started(run.run_id)
    store.apply_receipt(_receipt(run, "REPLY_START", {}))
    store.reconcile_after_restart()
    runtime_client = _RecoveryClient()

    asyncio.run(reconcile_runtime_gateway(client=runtime_client, store=store))  # interrupt only
    asyncio.run(reconcile_runtime_gateway(client=runtime_client, store=store))  # first idle observation
    assert store.active_run_for_session("session-a") is not None
    final = asyncio.run(reconcile_runtime_gateway(client=runtime_client, store=store))

    assert final.runs_finalized == 1
    terminal = store.get_run(run.run_id)
    assert terminal.status.value == "interrupted"
    assert terminal.terminal_reason == "observation_incomplete"
    assert terminal.trace_status == "incomplete"
    assert store.active_run_for_session("session-a") is None


def test_lost_chat_response_keeps_fence_until_canonical_recovery(tmp_path) -> None:
    store = _store(tmp_path)
    store.bind_session(
        session_id="session-a",
        agent_id="agent-a",
        agent_version_id="version-a",
        runtime_agent_id="runtime-a",
        digest="a" * 64,
    )
    runtime_client = _AcceptedButResponseLostClient()
    runtime_client.messages = [
        {
            "id": "reply-recovery",
            "name": "assistant",
            "role": "assistant",
            "content": [],
            "finished_reason": "completed",
            "error": None,
        },
    ]
    app = _creation_app(store, runtime_client, _Provisioner(store))

    with TestClient(app) as client:
        lost = client.post(
            "/api/runtime/chat/",
            json={
                "agent_id": "runtime-a",
                "session_id": "session-a",
                "client_operation_id": "lost-response",
                "input": {"role": "user", "content": []},
            },
        )
        blocked = client.post(
            "/api/runtime/chat/",
            json={
                "agent_id": "runtime-a",
                "session_id": "session-a",
                "client_operation_id": "blocked-second-turn",
                "input": {"role": "user", "content": []},
            },
        )

    assert lost.status_code == 503
    assert blocked.status_code == 409
    assert runtime_client.chat_attempts == 1
    run = store.active_run_for_session("session-a")
    assert run is not None
    assert run.status.value == "running"
    assert run.metadata["recovery_required"] is True
    assert run.metadata["trigger_uncertain"] is True
    assert run.error == {"type": "RuntimeUpstreamError"}
    assert "provider-test-secret" not in str(run.model_dump(mode="json"))

    store.apply_receipt(_receipt(run, "REPLY_START", {}))
    store.apply_receipt(
        _receipt(run, "REPLY_END", {"finished_reason": "completed"}),
    )
    interrupted = asyncio.run(
        reconcile_runtime_gateway(client=runtime_client, store=store),  # type: ignore[arg-type]
    )
    assert interrupted.runs_finalized == 0
    assert store.active_run_for_session("session-a") is not None

    first_idle = asyncio.run(
        reconcile_runtime_gateway(client=runtime_client, store=store),  # type: ignore[arg-type]
    )
    assert first_idle.runs_finalized == 0
    assert store.active_run_for_session("session-a") is not None

    recovered = asyncio.run(
        reconcile_runtime_gateway(client=runtime_client, store=store),  # type: ignore[arg-type]
    )
    assert recovered.runs_finalized == 1
    terminal = store.get_run(run.run_id)
    assert terminal.status.value == "interrupted"
    assert terminal.persisted_reply_ids == []
    assert store.active_run_for_session("session-a") is None
    assert not any(path.endswith("/messages") for _method, path in runtime_client.calls)


def test_request_timeout_is_not_proof_that_chat_was_unscheduled(tmp_path) -> None:
    store = _store(tmp_path)
    store.bind_session(
        session_id="session-a",
        agent_id="agent-a",
        agent_version_id="version-a",
        runtime_agent_id="runtime-a",
        digest="a" * 64,
    )
    runtime_client = _AcceptedButResponseLostClient(status_code=408)
    app = _creation_app(store, runtime_client, _Provisioner(store))

    with TestClient(app) as client:
        response = client.post(
            "/api/runtime/chat/",
            json={
                "agent_id": "runtime-a",
                "session_id": "session-a",
                "client_operation_id": "timeout-response",
                "input": {"role": "user", "content": []},
            },
        )

    assert response.status_code == 408
    run = store.active_run_for_session("session-a")
    assert run is not None
    assert run.metadata["recovery_required"] is True


def test_restart_recovery_interrupts_root_and_all_team_children(tmp_path) -> None:
    store = _store(tmp_path)
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
    store.record_team_inbox_delivery(
        RuntimeTeamInboxDelivery(
            event_id="worker-delivery",
            run_id=run.run_id,
            source_session_id="leader-session",
            target_session_id="worker-session",
        ),
    )
    assert store.reconcile_after_restart() == [run.run_id]
    runtime_client = _RecoveryClient()

    first = asyncio.run(
        reconcile_runtime_gateway(client=runtime_client, store=store),  # type: ignore[arg-type]
    )
    assert first.runs_finalized == 0
    assert runtime_client.calls == [
        ("POST", "/sessions/leader-session/interrupt"),
        ("POST", "/sessions/worker-session/interrupt"),
    ]
    assert store.get_session("leader-session").active_run_id == run.run_id
    assert store.get_session("worker-session").active_run_id == run.run_id

    asyncio.run(
        reconcile_runtime_gateway(client=runtime_client, store=store),  # type: ignore[arg-type]
    )
    final = asyncio.run(
        reconcile_runtime_gateway(client=runtime_client, store=store),  # type: ignore[arg-type]
    )
    assert final.runs_finalized == 1
    assert store.get_run(run.run_id).status.value == "interrupted"
    assert store.get_session("leader-session").active_run_id is None
    assert store.get_session("worker-session").active_run_id is None
    status_calls = [path for method, path in runtime_client.calls if method == "GET" and path.endswith("/status")]
    assert status_calls == [
        "/sessions/leader-session/status",
        "/sessions/worker-session/status",
        "/sessions/leader-session/status",
        "/sessions/worker-session/status",
    ]


def test_restart_recovery_expires_worker_hitl_after_two_idle_observations(tmp_path) -> None:
    store = _store(tmp_path)
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
    store.apply_receipt(_receipt(run, "REPLY_START", {}))
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
            event_id="worker-hitl-delivery",
            run_id=run.run_id,
            source_session_id="leader-session",
            target_session_id="worker-session",
        ),
    )
    store.apply_receipt(_receipt(run, "REPLY_END", {"finished_reason": "completed"}))
    tool_call = {
        "type": "tool_call",
        "id": "worker-tool",
        "name": "Read",
        "input": '{"file_path":"worker.txt"}',
        "state": "asking",
        "suggested_rules": [],
    }
    waiting = store.apply_receipt(
        RuntimeReceipt(
            receipt_id="receipt-worker-hitl",
            event_id="event-worker-hitl",
            session_id="worker-session",
            run_id=run.run_id,
            reply_id="worker-reply",
            type="REQUIRE_USER_CONFIRM",
            payload={"tool_calls": [tool_call]},
            trace_id=run.trace_id,
        ),
    )
    assert waiting.status.value == "waiting_human"
    store.reconcile_after_restart()
    runtime_client = _RecoveryClient()

    asyncio.run(reconcile_runtime_gateway(client=runtime_client, store=store))  # type: ignore[arg-type]
    first_idle = asyncio.run(reconcile_runtime_gateway(client=runtime_client, store=store))  # type: ignore[arg-type]
    terminal_report = asyncio.run(reconcile_runtime_gateway(client=runtime_client, store=store))  # type: ignore[arg-type]

    assert first_idle.runs_finalized == 0
    assert terminal_report.runs_finalized == 1
    assert store.get_run(run.run_id).status.value == "interrupted"
    assert store.get_session("leader-session").active_run_id is None
    assert store.get_session("worker-session").active_run_id is None
    assert not any(path.endswith("/messages") for _method, path in runtime_client.calls)
    with store.Session() as db:
        action = db.get(
            RuntimePendingActionModel,
            f"{run.run_id}:worker-reply:worker-tool",
        )
        assert action is not None and action.status == "expired"
        pending = db.scalars(
            select(RuntimePendingActionModel).where(
                RuntimePendingActionModel.run_id == run.run_id,
                RuntimePendingActionModel.status == "pending",
            ),
        ).all()
        assert pending == []


def test_runtime_only_boot_fences_running_run_before_periodic_recovery(tmp_path) -> None:
    store = _store(tmp_path)
    store.bind_session(
        session_id="session-a",
        agent_id="agent-a",
        agent_version_id="version-a",
        runtime_agent_id="runtime-a",
        digest="a" * 64,
    )
    run = store.begin_run(
        session_id="session-a",
        runtime_agent_id="runtime-a",
        input_value={"role": "user", "content": []},
        alert_id=None,
        case_id=None,
        metadata={},
    )
    store.mark_trigger_started(run.run_id)
    runtime_client = _RecoveryClient()

    untouched = asyncio.run(
        reconcile_runtime_gateway(client=runtime_client, store=store),  # type: ignore[arg-type]
    )
    assert untouched.runs_finalized == 0
    assert runtime_client.calls == []

    assert store.reconcile_after_runtime_boot("runtime-boot-new") == [run.run_id]
    first = asyncio.run(
        reconcile_runtime_gateway(client=runtime_client, store=store),  # type: ignore[arg-type]
    )
    assert first.runs_finalized == 0
    assert runtime_client.calls == [("POST", "/sessions/session-a/interrupt")]
    assert store.get_session("session-a").active_run_id == run.run_id


def test_restart_recovery_rejects_hitl_continuation_until_quiescent(tmp_path) -> None:
    store = _store(tmp_path)
    store.bind_session(
        session_id="session-a",
        agent_id="agent-a",
        agent_version_id="version-a",
        runtime_agent_id="runtime-a",
        digest="a" * 64,
    )
    run = store.begin_run(
        session_id="session-a",
        runtime_agent_id="runtime-a",
        input_value={"role": "user", "content": []},
        alert_id=None,
        case_id=None,
        metadata={},
    )
    store.mark_trigger_started(run.run_id)
    store.apply_receipt(_receipt(run, "REPLY_START", {}))
    tool_call = {
        "type": "tool_call",
        "id": "tool-a",
        "name": "Read",
        "input": '{"file_path":"a.txt"}',
        "state": "asking",
        "suggested_rules": [],
    }
    store.apply_receipt(
        _receipt(
            run,
            "REQUIRE_USER_CONFIRM",
            {"tool_calls": [tool_call]},
        ),
    )
    store.reconcile_after_restart()

    with pytest.raises(RuntimeStateConflict, match="recovery must complete"):
        store.begin_run(
            session_id="session-a",
            runtime_agent_id="runtime-a",
            input_value={
                "type": "USER_CONFIRM_RESULT",
                "reply_id": "reply-recovery",
                "confirm_results": [
                    {"confirmed": True, "tool_call": tool_call},
                ],
            },
            alert_id=None,
            case_id=None,
            metadata={},
            expected_run_id=run.run_id,
        )
    assert store.get_run(run.run_id).status.value == "waiting_human"
    assert store.get_session("session-a").active_run_id == run.run_id


def test_missing_leader_never_releases_fence_while_worker_is_running(tmp_path) -> None:
    store = _store(tmp_path)
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
    store.record_team_inbox_delivery(
        RuntimeTeamInboxDelivery(
            event_id="worker-running",
            run_id=run.run_id,
            source_session_id="leader-session",
            target_session_id="worker-session",
        ),
    )
    store.reconcile_after_runtime_boot("runtime-boot-new")

    class _PartiallyMissingClient(_RecoveryClient):
        async def request_json(self, method: str, path: str, **kwargs) -> RuntimeJsonResponse:
            self.calls.append((method, path))
            if "leader-session" in path:
                raise RuntimeUpstreamError(404, b'{"detail":"gone"}')
            if method == "POST" and path.endswith("/interrupt"):
                return RuntimeJsonResponse(202, {}, {})
            if method == "GET" and path.endswith("/status"):
                return RuntimeJsonResponse(200, {}, {"status": "running"})
            raise AssertionError((method, path, kwargs))

    runtime_client = _PartiallyMissingClient()
    asyncio.run(
        reconcile_runtime_gateway(client=runtime_client, store=store),  # type: ignore[arg-type]
    )
    second = asyncio.run(
        reconcile_runtime_gateway(client=runtime_client, store=store),  # type: ignore[arg-type]
    )

    assert second.runs_finalized == 0
    assert store.get_run(run.run_id).status.value == "running"
    assert store.get_session("leader-session").active_run_id == run.run_id
    assert store.get_session("worker-session").active_run_id == run.run_id
