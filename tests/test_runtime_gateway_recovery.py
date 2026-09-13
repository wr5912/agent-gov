from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from agentscope_runtime.service import create_runtime_app
from agentscope_runtime.settings import RuntimeSettings
from app.routers.error_handlers import register_error_handlers
from app.runtime.runtime_db import make_session_factory
from app.runtime_gateway._router_recovery import _find_workspace_session
from app.runtime_gateway.client import (
    AgentScopeRuntimeClient,
    RuntimeJsonResponse,
    RuntimeUpstreamError,
)
from app.runtime_gateway.contracts import RunStatus, RuntimeChildSessionRegistration, RuntimeReceipt
from app.runtime_gateway.models import RuntimePendingActionModel
from app.runtime_gateway.provisioning import RuntimeAgentBinding
from app.runtime_gateway.router import create_runtime_router, reconcile_runtime_gateway
from app.runtime_gateway.store import RuntimeRunStore, RuntimeStateConflict, SessionCreationStatus
from app.version import APP_VERSION
from fastapi import FastAPI
from fastapi.testclient import TestClient

from runtime_hitl_test_utils import fingerprinted_hitl_payload
from runtime_loopback import serve_loopback

SECRET = "runtime-recovery-real-boundary-secret"


def _store(tmp_path: Path) -> RuntimeRunStore:
    store = RuntimeRunStore(make_session_factory(tmp_path / "runtime.db"))
    store.bind_agent_version(
        agent_id="agent-a",
        agent_version_id="version-a",
        digest="a" * 64,
        runtime_agent_id="runtime-a",
    )
    return store


def _begin(store: RuntimeRunStore, *, session_id: str = "session-a"):
    store.bind_session(
        session_id=session_id,
        agent_id="agent-a",
        agent_version_id="version-a",
        runtime_agent_id="runtime-a",
        digest="a" * 64,
    )
    run = store.begin_run(
        session_id=session_id,
        runtime_agent_id="runtime-a",
        input_value={"role": "user", "content": []},
        alert_id=None,
        case_id=None,
        metadata={},
    )
    store.mark_trigger_started(run.run_id)
    return store.get_run(run.run_id)


def _interrupted_receipt(run, *, session_id: str | None = None) -> RuntimeReceipt:
    resolved_session = session_id or run.session_id
    return RuntimeReceipt(
        receipt_id=f"receipt-interrupted-{resolved_session}",
        event_id=f"event-interrupted-{resolved_session}",
        session_id=resolved_session,
        run_id=run.run_id,
        reply_id=None,
        type="RUN_INTERRUPTED",
        payload={},
        trace_id=run.trace_id,
    )


def _runtime_settings(tmp_path: Path) -> RuntimeSettings:
    data_dir = tmp_path / "runtime-data"
    business_root = tmp_path / "business-agents"
    candidates_root = tmp_path / "candidates"
    workspaces_root = tmp_path / "workspaces"
    for directory in (data_dir, business_root, candidates_root, workspaces_root):
        directory.mkdir(parents=True, exist_ok=True)
    return RuntimeSettings(
        shared_secret=SECRET,
        provider_api_key="runtime-recovery-provider-key",
        agentgov_api_base_url="http://127.0.0.1:9",
        data_dir=data_dir,
        business_agents_root=business_root,
        candidates_root=candidates_root,
        workspaces_root=workspaces_root,
        database_url=f"sqlite+aiosqlite:///{data_dir / 'agentscope.db'}",
    )


def test_concurrent_session_creation_intent_has_one_durable_owner(tmp_path: Path) -> None:
    store = _store(tmp_path)

    def begin_intent(_index: int):
        return store.start_session_creation(
            idempotency_key="concurrent-create",
            agent_id="agent-a",
            agent_version_id="version-a",
            runtime_agent_id="runtime-a",
            digest="a" * 64,
            workspace_id=f"agent-a--v-{'a' * 64}",
            requested_name="Concurrent review",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(begin_intent, range(2)))

    assert {item.intent_id for item, _owned in results} == {results[0][0].intent_id}
    assert sorted(owned for _item, owned in results) == [False, True]
    persisted = store.session_creation_for_key("concurrent-create")
    assert persisted is not None
    assert persisted.status == SessionCreationStatus.PENDING.value
    assert persisted.session_id is None


def test_session_creation_idempotency_key_binds_exact_name_including_null(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    common = {
        "agent_id": "agent-a",
        "agent_version_id": "version-a",
        "runtime_agent_id": "runtime-a",
        "digest": "a" * 64,
        "workspace_id": f"agent-a--v-{'a' * 64}",
    }
    created, owned = store.start_session_creation(
        idempotency_key="named-create",
        requested_name=None,
        **common,
    )

    replay, replay_owned = store.start_session_creation(
        idempotency_key="named-create",
        requested_name=None,
        **common,
    )
    assert owned is True
    assert replay_owned is False
    assert replay.intent_id == created.intent_id
    with pytest.raises(RuntimeStateConflict, match="immutable Session request"):
        store.start_session_creation(
            idempotency_key="named-create",
            requested_name="Incident review",
            **common,
        )
    with pytest.raises(RuntimeStateConflict, match="immutable Session request"):
        store.session_creation_for_request(
            key="named-create",
            runtime_agent_id="runtime-a",
            requested_name="Incident review",
        )


class _RecoverySessionListClient:
    def __init__(self, sessions: list[object]) -> None:
        self.sessions = sessions

    async def request_json(self, method: str, path: str, **_kwargs) -> RuntimeJsonResponse:
        assert (method, path) == ("GET", "/sessions/")
        return RuntimeJsonResponse(
            200,
            {"content-type": "application/json"},
            {"sessions": self.sessions, "total": len(self.sessions)},
        )


def test_session_recovery_uses_only_canonical_session_identity() -> None:
    canonical = _RecoverySessionListClient(
        [
            {
                "session": {
                    "id": "session-canonical",
                    "config": {"workspace_id": "workspace-a"},
                },
                "session_id": "conflicting-top-level",
            },
        ],
    )
    assert (
        asyncio.run(
            _find_workspace_session(
                canonical,  # type: ignore[arg-type]
                "runtime-a",
                "workspace-a",
            ),
        )
        == "session-canonical"
    )

    top_level_only = _RecoverySessionListClient(
        [
            {
                "session_id": "legacy-session",
                "session": {"config": {"workspace_id": "workspace-a"}},
            },
        ],
    )
    with pytest.raises(RuntimeUpstreamError, match="HTTP 502"):
        asyncio.run(
            _find_workspace_session(
                top_level_only,  # type: ignore[arg-type]
                "runtime-a",
                "workspace-a",
            ),
        )


@pytest.mark.parametrize(
    "session_view",
    [
        {"session": {"id": "session-a"}},
        {"session": {"id": "session-a", "config": {}}},
        {
            "session": {"id": "session-a", "config": {"workspace_id": ""}},
            "workspace_id": "legacy-workspace",
        },
    ],
    ids=("missing-config", "missing-workspace", "empty-canonical-workspace"),
)
def test_session_recovery_rejects_noncanonical_workspace_identity(
    session_view: object,
) -> None:
    with pytest.raises(RuntimeUpstreamError, match="HTTP 502"):
        asyncio.run(
            _find_workspace_session(
                _RecoverySessionListClient([session_view]),  # type: ignore[arg-type]
                "runtime-a",
                "workspace-a",
            ),
        )


@pytest.mark.parametrize(
    ("canonical_receipt", "expected_reason", "expected_trace_status"),
    [
        (False, "observation_incomplete", "incomplete"),
        (True, "interrupted", "pending"),
    ],
    ids=("missing-receipt-fallback", "canonical-interruption"),
)
def test_real_runtime_404_recovery_requires_two_quiescent_probes(
    tmp_path: Path,
    canonical_receipt: bool,
    expected_reason: str,
    expected_trace_status: str,
) -> None:
    store = _store(tmp_path)
    run = _begin(store, session_id="session-missing")
    store.mark_cancel_requested(run.run_id)
    if canonical_receipt:
        recovering = store.apply_receipt(_interrupted_receipt(run))
        assert recovering.status is RunStatus.RUNNING
    settings = _runtime_settings(tmp_path)

    with serve_loopback(create_runtime_app(settings), lifespan="on") as runtime_url:

        async def recover() -> tuple[int, int, int]:
            client = AgentScopeRuntimeClient(runtime_url, shared_secret=SECRET)
            try:
                first = await reconcile_runtime_gateway(client=client, store=store)
                second = await reconcile_runtime_gateway(client=client, store=store)
                third = await reconcile_runtime_gateway(client=client, store=store)
                return first.runs_finalized, second.runs_finalized, third.runs_finalized
            finally:
                await client.close()

        assert asyncio.run(recover()) == (0, 0, 1)

    terminal = store.get_run(run.run_id)
    assert terminal.status is RunStatus.CANCELLED
    assert terminal.terminal_reason == expected_reason
    assert terminal.trace_status == expected_trace_status
    assert store.active_run_for_session(run.session_id) is None


def test_restart_recovery_retains_team_fences_and_expires_hitl_only_at_terminal(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = _begin(store, session_id="leader-session")
    store.bind_team_child(
        RuntimeChildSessionRegistration(
            run_id=run.run_id,
            parent_session_id="leader-session",
            child_session_id="worker-session",
            child_runtime_agent_id="worker-agent",
            team_id="team-1",
        ),
    )
    tool_call = {
        "type": "tool_call",
        "id": "worker-tool",
        "name": "Read",
        "input": '{"file_path":"worker.txt"}',
        "state": "asking",
    }
    store.apply_receipt(
        RuntimeReceipt(
            receipt_id="receipt-worker-hitl",
            event_id="event-worker-hitl",
            session_id="worker-session",
            run_id=run.run_id,
            reply_id="worker-reply",
            type="REQUIRE_USER_CONFIRM",
            payload=fingerprinted_hitl_payload([tool_call]),
            trace_id=run.trace_id,
        ),
    )

    assert store.reconcile_after_restart() == [run.run_id]
    with pytest.raises(RuntimeStateConflict, match="recovery must complete"):
        store.begin_run(
            session_id="leader-session",
            runtime_agent_id="runtime-a",
            input_value={
                "type": "USER_CONFIRM_RESULT",
                "reply_id": "worker-reply",
                "confirm_results": [{"confirmed": True, "tool_call": tool_call}],
            },
            alert_id=None,
            case_id=None,
            metadata={},
            client_operation_id="continuation-during-recovery",
            expected_run_id=run.run_id,
        )
    store.apply_receipt(_interrupted_receipt(run, session_id="leader-session"))
    store.apply_receipt(_interrupted_receipt(run, session_id="worker-session"))

    assert store.settle_after_quiescent_observation(run.run_id, error="first idle") is None
    assert store.get_session("leader-session").active_run_id == run.run_id
    assert store.get_session("worker-session").active_run_id == run.run_id
    with store.Session() as db:
        assert db.get(RuntimePendingActionModel, f"{run.run_id}:worker-reply:worker-tool").status == "pending"

    terminal = store.settle_after_quiescent_observation(run.run_id, error="second idle")
    assert terminal is not None and terminal.status is RunStatus.INTERRUPTED
    assert terminal.terminal_reason == "interrupted"
    assert store.get_session("leader-session").active_run_id is None
    assert store.get_session("worker-session").active_run_id is None
    with store.Session() as db:
        assert db.get(RuntimePendingActionModel, f"{run.run_id}:worker-reply:worker-tool").status == "expired"


def test_runtime_boot_marks_only_active_runs_for_recovery(tmp_path: Path) -> None:
    store = _store(tmp_path)
    active = _begin(store, session_id="session-active")
    store.bind_session(
        session_id="session-terminal",
        agent_id="agent-a",
        agent_version_id="version-a",
        runtime_agent_id="runtime-a",
        digest="a" * 64,
    )
    terminal = store.begin_run(
        session_id="session-terminal",
        runtime_agent_id="runtime-a",
        input_value={"role": "user", "content": []},
        alert_id=None,
        case_id=None,
        metadata={},
    )
    store.fail_trigger(terminal.run_id, error={"type": "test"})

    assert store.reconcile_after_runtime_boot("runtime-boot-new", APP_VERSION) == [active.run_id]
    recovering = store.get_run(active.run_id)
    assert recovering.metadata["recovery_required"] is True
    assert recovering.metadata["recovery_quiescent_observations"] == 0
    assert recovering.metadata["runtime_boot_version"] == APP_VERSION
    assert store.get_run(terminal.run_id).status is RunStatus.FAILED


class _BoundProvisioner:
    """把测试限定在 chat 受理边界，版本与 Session 仍使用真实 store。"""

    def __init__(self, store: RuntimeRunStore) -> None:
        self.store = store

    def require_current_runtime(self, runtime_agent_id: str) -> RuntimeAgentBinding:
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


class _LostChatResponseClient:
    """确定性注入“Runtime 已受理，但控制面只收到错误响应”的故障。"""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.chat_attempts = 0

    async def request_json(self, method: str, path: str, **_kwargs):
        assert method == "POST" and path == "/chat/"
        self.chat_attempts += 1
        raise RuntimeUpstreamError(
            self.status_code,
            b'{"detail":"accepted but response lost","secret":"must-not-leak"}',
        )


class _WorkerChatResponseClient:
    def __init__(self) -> None:
        self.chat_attempts = 0

    async def request_json(self, method: str, path: str, **_kwargs):
        from app.runtime_gateway.client import RuntimeJsonResponse

        assert method == "POST" and path == "/chat/"
        self.chat_attempts += 1
        return RuntimeJsonResponse(200, {"content-type": "application/json"}, {"status": "started", "session_id": "worker-session"})


@pytest.mark.parametrize("status_code", [400, 422, 429])
def test_proven_unscheduled_chat_failure_terminates_fence_without_retrigger(
    tmp_path: Path,
    status_code: int,
) -> None:
    store = _store(tmp_path)
    store.bind_session(
        session_id="session-a",
        agent_id="agent-a",
        agent_version_id="version-a",
        runtime_agent_id="runtime-a",
        digest="a" * 64,
    )
    runtime = _LostChatResponseClient(status_code)
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(
        create_runtime_router(
            client=runtime,  # type: ignore[arg-type]
            store=store,
            provisioner=_BoundProvisioner(store),  # type: ignore[arg-type]
            model_type="openai_credential",
            credential_id="provider",
            model_name="model",
            model_parameters={},
            require_api_key=lambda: None,
        ),
    )
    request_data = {
        "agent_id": "runtime-a",
        "session_id": "session-a",
        "client_operation_id": f"not-scheduled-{status_code}",
        "input": {"role": "user", "content": []},
    }

    with TestClient(app) as client:
        first = client.post("/api/runtime/chat/", json=request_data)
        replay = client.post("/api/runtime/chat/", json=request_data)

    assert [first.status_code, replay.status_code] == [status_code, 409]
    assert runtime.chat_attempts == 1
    run = store.run_for_client_operation(
        session_id="session-a",
        client_operation_id=f"not-scheduled-{status_code}",
    )
    assert run.status is RunStatus.FAILED
    assert run.terminal_reason == "trigger_failed"
    assert store.active_run_for_session("session-a") is None


def test_worker_hitl_chat_response_keeps_root_public_identity_and_durable_replay(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _begin(store)
    active = store.active_run_for_session("session-a")
    assert active is not None
    store.fail_trigger(active.run_id, error={"type": "prepare-worker-response"})
    runtime = _WorkerChatResponseClient()
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(
        create_runtime_router(
            client=runtime,  # type: ignore[arg-type]
            store=store,
            provisioner=_BoundProvisioner(store),  # type: ignore[arg-type]
            model_type="openai_credential",
            credential_id="provider",
            model_name="model",
            model_parameters={},
            require_api_key=lambda: None,
        )
    )
    payload = {
        "agent_id": "runtime-a",
        "session_id": "session-a",
        "client_operation_id": "worker-hitl-response",
        "input": {"role": "user", "content": [{"type": "text", "text": "continue worker"}]},
    }

    with TestClient(app) as client:
        first = client.post("/api/runtime/chat/", json=payload)
        replay = client.post("/api/runtime/chat/", json=payload)

    assert first.status_code == replay.status_code == 200
    assert (
        first.json()
        == replay.json()
        == {
            "status": "started",
            "session_id": "session-a",
            "worker_session_id": "worker-session",
        }
    )
    assert first.headers["X-AgentGov-Session-Id"] == "session-a"
    assert runtime.chat_attempts == 1


@pytest.mark.parametrize("status_code", [408, 500, 503])
def test_uncertain_chat_response_keeps_exact_run_fence_and_never_retriggers(
    tmp_path: Path,
    status_code: int,
) -> None:
    store = _store(tmp_path)
    _begin(store)
    active = store.active_run_for_session("session-a")
    assert active is not None
    store.fail_trigger(active.run_id, error={"type": "fixture-reset"})
    runtime = _LostChatResponseClient(status_code)
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(
        create_runtime_router(
            client=runtime,  # type: ignore[arg-type]
            store=store,
            provisioner=_BoundProvisioner(store),  # type: ignore[arg-type]
            model_type="openai_credential",
            credential_id="provider",
            model_name="model",
            model_parameters={},
            require_api_key=lambda: None,
        ),
    )
    payload = {
        "agent_id": "runtime-a",
        "session_id": "session-a",
        "client_operation_id": "response-loss",
        "input": {"role": "user", "content": []},
    }

    with TestClient(app) as client:
        first = client.post("/api/runtime/chat/", json=payload)
        replay = client.post("/api/runtime/chat/", json=payload)
        competing = client.post(
            "/api/runtime/chat/",
            json={**payload, "client_operation_id": "competing-operation"},
        )

    assert [first.status_code, replay.status_code, competing.status_code] == [status_code, 409, 409]
    assert runtime.chat_attempts == 1
    run = store.run_for_client_operation(
        session_id="session-a",
        client_operation_id="response-loss",
    )
    assert run.status is RunStatus.RUNNING
    assert run.metadata["recovery_required"] is True
    assert run.metadata["trigger_uncertain"] is True
    assert run.error == {"type": "RuntimeUpstreamError"}
    assert "must-not-leak" not in str(run.model_dump(mode="json"))
    assert store.active_run_for_session("session-a").run_id == run.run_id
