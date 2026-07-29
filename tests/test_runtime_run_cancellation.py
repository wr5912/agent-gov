from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest
from app.routers.agent_run_control import create_agent_run_control_router
from app.routers.error_handlers import register_error_handlers
from app.runtime.active_run_coordinator import ActiveRunCoordinator
from app.runtime.agent_profiles import build_business_agent_profile
from app.runtime.async_iterators import close_async_iterator
from app.runtime.claude_runtime import ClaudeRuntime, RuntimeQueryState
from app.runtime.http_disconnect import run_while_request_connected
from app.runtime.managed_claude_events import (
    AgentGovControlEvent,
    ClaudeSdkMessageEvent,
)
from app.runtime.managed_streaming_response import ManagedStreamingResponse
from app.runtime.protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from app.runtime.run_control import (
    AgentRunCancellationTimeoutError,
    RunCancellationService,
)
from app.runtime.runtime_db import (
    AgentRunModel,
    SdkSessionEntryModel,
    SessionTurnIntentModel,
)
from app.runtime.schemas import ChatRequest
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import AppSettings
from app.runtime.stores.feedback_store import FeedbackStore
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect, Request

from business_agent_test_utils import create_test_business_agent_workspace
from claude_runtime_test_utils import route_interactive_client_through_query


def _settings(tmp_path: Path) -> AppSettings:
    settings = AppSettings(
        _env_file=None,
        DATA_DIR=tmp_path / "data",
        GOVERNOR_CLAUDE_ROOT=tmp_path / "governor-claude-root",
        RUNTIME_VOLUME_MODE="local-debug",
    )
    create_test_business_agent_workspace(
        settings.default_workspace_dir,
        agent_id=DEFAULT_BUSINESS_AGENT_ID,
        name="Security Operations Expert",
    )
    settings.default_workspace_dir.joinpath(".mcp.json").write_text(
        json.dumps({"mcpServers": {}}),
        encoding="utf-8",
    )
    return settings


def _runtime(
    settings: AppSettings,
    store: LocalSessionStore,
    feedback_store: FeedbackStore,
) -> ClaudeRuntime:
    return ClaudeRuntime(
        settings,
        store,
        feedback_store,
        business_profile_resolver=lambda agent_id: build_business_agent_profile(
            settings,
            agent_id=agent_id or DEFAULT_BUSINESS_AGENT_ID,
            workspace_dir=settings.default_workspace_dir,
        ),
    )


def test_cancel_waits_for_durable_terminal_and_allows_immediate_same_session_retry(
    tmp_path,
    monkeypatch,
):
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    route_interactive_client_through_query(monkeypatch)
    first_query_started = asyncio.Event()
    query_count = 0

    async def fake_query(*, prompt, options, transport=None):
        nonlocal query_count
        query_count += 1
        async for _ in prompt:
            pass
        sdk_session_id = options.resume or options.session_id
        entry_id = f"entry-{query_count}"
        await options.session_store.append(
            {
                "project_key": options.session_store.binding.project_key,
                "session_id": sdk_session_id,
            },
            [{"type": "assistant", "uuid": entry_id}],
        )
        if query_count == 1:
            first_query_started.set()
            yield AssistantMessage(
                content=[TextBlock(text="partial")],
                model="<synthetic>",
                session_id=sdk_session_id,
            )
            await asyncio.Event().wait()
            return
        yield AssistantMessage(
            content=[TextBlock(text="after cancel")],
            model="<synthetic>",
            session_id=sdk_session_id,
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id=sdk_session_id,
            result="after cancel",
        )

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    settings = _settings(tmp_path)
    store = LocalSessionStore(settings.session_dir)
    feedback_store = FeedbackStore(
        data_dir=settings.data_dir,
        workspace_dir=settings.default_workspace_dir,
    )
    runtime = _runtime(settings, store, feedback_store)
    cancellation = RunCancellationService(
        session_store=store,
        coordinator=runtime.active_runs,
        timeout_seconds=1,
    )

    async def exercise():
        source = runtime.stream_events(
            ChatRequest(
                message="first",
                session_id="cancel-session",
                agent_id=DEFAULT_BUSINESS_AGENT_ID,
            )
        )
        session_event = await anext(source)
        assert isinstance(session_event, AgentGovControlEvent)
        run_id = str(session_event.data["run_id"])
        await asyncio.wait_for(first_query_started.wait(), timeout=1)
        while True:
            event = await anext(source)
            if isinstance(event, ClaudeSdkMessageEvent):
                break

        cancelled = await cancellation.cancel(run_id)
        duplicate = await cancellation.cancel(run_id)
        terminal_events = []
        async for event in source:
            terminal_events.append(event)

        retry = await runtime.run(
            ChatRequest(
                message="second",
                session_id="cancel-session",
                agent_id=DEFAULT_BUSINESS_AGENT_ID,
            )
        )
        return run_id, cancelled, duplicate, terminal_events, retry

    run_id, cancelled, duplicate, terminal_events, retry = asyncio.run(exercise())

    assert cancelled == duplicate
    assert cancelled.turn_status == "cancelled"
    assert cancelled.cancelled is True
    assert cancelled.session_active_run_id is None
    assert retry.answer == "after cancel"
    assert [event.name for event in terminal_events if isinstance(event, AgentGovControlEvent)] == [
        "cancelled",
        "done",
    ]
    saved = store.get("cancel-session")
    assert saved is not None
    assert saved.active_run_id is None
    assert saved.turns == 1
    with store.Session() as db:
        intent = db.get(SessionTurnIntentModel, run_id)
        run = db.get(AgentRunModel, run_id)
        entries = list(db.query(SdkSessionEntryModel).filter(SdkSessionEntryModel.origin_run_id == run_id).all())
        assert intent is not None and intent.status == "cancelled"
        assert run is not None and run.payload_json["turn_status"] == "cancelled"
        assert entries and all(entry.committed_at is None and entry.discarded_at for entry in entries)


def test_non_stream_owner_is_cancellable_through_the_same_run_coordinator(
    tmp_path,
    monkeypatch,
):
    settings = _settings(tmp_path)
    store = LocalSessionStore(settings.session_dir)
    feedback_store = FeedbackStore(
        data_dir=settings.data_dir,
        workspace_dir=settings.default_workspace_dir,
    )
    runtime = _runtime(settings, store, feedback_store)
    started = asyncio.Event()

    async def blocked_claimed(req, *, context, profile, heartbeat):
        state = RuntimeQueryState(sdk_session_id=context.session.sdk_session_id)
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            await asyncio.to_thread(
                runtime._abort_runtime_request,
                req,
                context,
                state,
                terminal_status="cancelled",
                error=exc,
            )
            raise

    monkeypatch.setattr(runtime, "_run_claimed", blocked_claimed)
    cancellation = RunCancellationService(
        session_store=store,
        coordinator=runtime.active_runs,
        timeout_seconds=1,
    )

    async def exercise():
        task = asyncio.create_task(
            runtime.run(
                ChatRequest(
                    message="non-stream",
                    session_id="non-stream-session",
                    agent_id=DEFAULT_BUSINESS_AGENT_ID,
                )
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        claimed = store.get("non-stream-session")
        assert claimed is not None and claimed.active_run_id
        response = await cancellation.cancel(claimed.active_run_id)
        task_result = await asyncio.gather(task, return_exceptions=True)
        return response, task_result[0]

    response, task_result = asyncio.run(exercise())

    assert response.turn_status == "cancelled"
    assert response.session_active_run_id is None
    assert isinstance(task_result, asyncio.CancelledError)
    saved = store.get("non-stream-session")
    assert saved is not None and saved.active_run_id is None


def test_non_stream_owner_failure_aborts_turn_and_releases_session_fence(
    tmp_path,
    monkeypatch,
):
    settings = _settings(tmp_path)
    store = LocalSessionStore(settings.session_dir)
    feedback_store = FeedbackStore(
        data_dir=settings.data_dir,
        workspace_dir=settings.default_workspace_dir,
    )
    runtime = _runtime(settings, store, feedback_store)

    async def failing_claimed(req, *, context, profile, heartbeat):
        raise RuntimeError("failure outside the normal query error boundary")

    monkeypatch.setattr(runtime, "_run_claimed", failing_claimed)

    async def exercise():
        with pytest.raises(RuntimeError, match="outside the normal query error boundary"):
            await runtime.run(
                ChatRequest(
                    message="non-stream failure",
                    session_id="non-stream-failure-session",
                    agent_id=DEFAULT_BUSINESS_AGENT_ID,
                )
            )

    asyncio.run(exercise())

    saved = store.get("non-stream-failure-session")
    assert saved is not None and saved.active_run_id is None
    with store.Session() as db:
        intent = db.query(SessionTurnIntentModel).filter(SessionTurnIntentModel.session_id == "non-stream-failure-session").one()
        run = db.get(AgentRunModel, intent.run_id)
        assert intent.status == "failed"
        assert run is not None and run.payload_json["turn_status"] == "failed"


def test_cancellation_during_threaded_turn_admission_cannot_leave_a_session_fence(
    tmp_path,
    monkeypatch,
):
    settings = _settings(tmp_path)
    store = LocalSessionStore(settings.session_dir)
    feedback_store = FeedbackStore(
        data_dir=settings.data_dir,
        workspace_dir=settings.default_workspace_dir,
    )
    runtime = _runtime(settings, store, feedback_store)
    admission_entered = threading.Event()
    release_admission = threading.Event()
    original_begin = store.begin_persisted_turn

    def delayed_begin(*args, **kwargs):
        admission_entered.set()
        assert release_admission.wait(timeout=2)
        return original_begin(*args, **kwargs)

    monkeypatch.setattr(store, "begin_persisted_turn", delayed_begin)

    async def exercise():
        task = asyncio.create_task(
            runtime.run(
                ChatRequest(
                    message="cancel during admission",
                    session_id="admission-session",
                    agent_id=DEFAULT_BUSINESS_AGENT_ID,
                )
            )
        )
        assert await asyncio.to_thread(admission_entered.wait, 1)
        task.cancel()
        release_admission.set()
        return (await asyncio.gather(task, return_exceptions=True))[0]

    task_result = asyncio.run(exercise())

    assert isinstance(task_result, asyncio.CancelledError)
    saved = store.get("admission-session")
    assert saved is not None and saved.active_run_id is None
    with store.Session() as db:
        intents = list(db.query(SessionTurnIntentModel).filter(SessionTurnIntentModel.session_id == "admission-session").all())
        assert len(intents) == 1
        assert intents[0].status == "cancelled"
        run = db.get(AgentRunModel, intents[0].run_id)
        assert run is not None and run.payload_json["turn_status"] == "cancelled"


def test_cancel_api_is_idempotent_and_running_without_owner_fails_closed(
    tmp_path,
):
    settings = _settings(tmp_path)
    store = LocalSessionStore(settings.session_dir)
    session = store.get_or_create_owned(
        "ownerless-session",
        agent_id=DEFAULT_BUSINESS_AGENT_ID,
    )
    admission = store.begin_persisted_turn(
        session,
        run_id="ownerless-run",
        agent_id=DEFAULT_BUSINESS_AGENT_ID,
        new_sdk_session_id="ownerless-sdk",
        sdk_project_key="ownerless-project",
        resolve_agent_version_id=lambda: "version-1",
        request={"message": "blocked"},
        created_at="2026-07-28T00:00:00+00:00",
    )
    assert admission.session.active_run_id == "ownerless-run"

    service = RunCancellationService(
        session_store=store,
        coordinator=ActiveRunCoordinator(),
        timeout_seconds=0.1,
    )
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(
        create_agent_run_control_router(
            cancellation_service=service,
            require_api_key=lambda: None,
        )
    )

    with TestClient(app) as client:
        unavailable = client.post("/api/agent-runs/ownerless-run/cancel")
        missing = client.post("/api/agent-runs/missing/cancel")

    assert unavailable.status_code == 409
    assert unavailable.json()["error_code"] == "AGENT_RUN_CANCELLATION_UNAVAILABLE"
    assert missing.status_code == 404
    assert missing.json()["error_code"] == "AGENT_RUN_NOT_FOUND"
    saved = store.get("ownerless-session")
    assert saved is not None and saved.active_run_id == "ownerless-run"


def test_cancel_times_out_when_owner_does_not_reach_durable_terminal(
    tmp_path,
):
    settings = _settings(tmp_path)
    store = LocalSessionStore(settings.session_dir)
    session = store.get_or_create_owned(
        "timeout-session",
        agent_id=DEFAULT_BUSINESS_AGENT_ID,
    )
    store.begin_persisted_turn(
        session,
        run_id="timeout-run",
        agent_id=DEFAULT_BUSINESS_AGENT_ID,
        new_sdk_session_id="timeout-sdk",
        sdk_project_key="timeout-project",
        resolve_agent_version_id=lambda: "version-1",
        request={"message": "blocked"},
        created_at="2026-07-28T00:00:00+00:00",
    )
    coordinator = ActiveRunCoordinator()
    service = RunCancellationService(
        session_store=store,
        coordinator=coordinator,
        timeout_seconds=0.01,
    )

    async def exercise():
        owner = asyncio.create_task(asyncio.Event().wait())

        async def ignore_cancel() -> None:
            return None

        coordinator.register(
            run_id="timeout-run",
            session_id="timeout-session",
            owner_task=owner,
            cancel_callback=ignore_cancel,
        )
        try:
            with pytest.raises(AgentRunCancellationTimeoutError):
                await service.cancel("timeout-run")
        finally:
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)

    asyncio.run(exercise())


def test_startup_reconciliation_interrupts_previous_process_turn_immediately(
    tmp_path,
):
    settings = _settings(tmp_path)
    store = LocalSessionStore(settings.session_dir)
    session = store.get_or_create_owned(
        "restart-session",
        agent_id=DEFAULT_BUSINESS_AGENT_ID,
    )
    store.begin_persisted_turn(
        session,
        run_id="restart-run",
        agent_id=DEFAULT_BUSINESS_AGENT_ID,
        new_sdk_session_id="restart-sdk",
        sdk_project_key="restart-project",
        resolve_agent_version_id=lambda: "version-1",
        request={"message": "interrupted by restart"},
        created_at="2026-07-28T00:00:00+00:00",
    )

    reconciled = store.reconcile_running_turns_after_restart()

    assert reconciled == ["restart-run"]
    assert store.reconcile_running_turns_after_restart() == []
    saved = store.get("restart-session")
    assert saved is not None and saved.active_run_id is None
    with store.Session() as db:
        intent = db.get(SessionTurnIntentModel, "restart-run")
        run = db.get(AgentRunModel, "restart-run")
        assert intent is not None and intent.status == "interrupted"
        assert run is not None and run.payload_json["turn_status"] == "interrupted"


def test_managed_streaming_response_closes_nested_source_on_asgi_23_send_disconnect():
    inner_closed = asyncio.Event()
    send_started = asyncio.Event()
    release_send = asyncio.Event()

    async def inner():
        try:
            yield b"partial"
            await asyncio.Event().wait()
        finally:
            inner_closed.set()

    async def outer():
        source = inner()
        try:
            async for chunk in source:
                yield chunk
        finally:
            await close_async_iterator(source)

    response = ManagedStreamingResponse(outer())
    request_sent = False

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await send_started.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            send_started.set()
            await release_send.wait()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/stream",
        "raw_path": b"/stream",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1),
        "server": ("testserver", 80),
        "root_path": "",
    }

    async def exercise():
        await asyncio.wait_for(response(scope, receive, send), timeout=1)
        await asyncio.wait_for(inner_closed.wait(), timeout=1)

    try:
        asyncio.run(exercise())
    finally:
        release_send.set()


def test_non_stream_http_disconnect_cancels_and_drains_owned_operation():
    operation_started = asyncio.Event()
    operation_closed = asyncio.Event()

    async def operation():
        operation_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            operation_closed.set()

    async def receive():
        await operation_started.wait()
        return {"type": "http.disconnect"}

    request = Request(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/non-stream",
            "raw_path": b"/non-stream",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("testserver", 80),
            "root_path": "",
        },
        receive,
    )

    async def exercise():
        with pytest.raises(ClientDisconnect):
            await run_while_request_connected(request, operation())
        await asyncio.wait_for(operation_closed.wait(), timeout=1)

    asyncio.run(exercise())
