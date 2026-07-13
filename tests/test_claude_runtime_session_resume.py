import asyncio
import uuid

import pytest
from app.runtime import session_turn_lease
from app.runtime.async_iterators import close_async_iterator
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.errors import SessionConflictError
from app.runtime.openai_responses_stream import iter_responses_sse
from app.runtime.schemas import ChatRequest
from app.runtime.session_store import LocalSession, LocalSessionStore
from app.runtime.settings import AppSettings
from app.runtime.stores.feedback_store import FeedbackStore


def _settings(tmp_path):
    workspace = tmp_path / "docker" / "volume" / "main-workspace"
    data = tmp_path / "docker" / "volume" / "data"
    claude_root = tmp_path / "docker" / "volume" / "claude-roots" / "main"
    claude_home = claude_root / ".claude"
    workspace.mkdir(parents=True, exist_ok=True)
    claude_home.mkdir(parents=True, exist_ok=True)
    return AppSettings(
        _env_file=None,
        WORKSPACE_DIR=workspace,
        MAIN_WORKSPACE_DIR=workspace,
        DATA_DIR=data,
        CLAUDE_ROOT=claude_root,
        MAIN_CLAUDE_ROOT=claude_root,
        CLAUDE_HOME=claude_home,
    )


def _store_with_stale_session(settings, session_id):
    from claude_agent_sdk import project_key_for_directory

    store = LocalSessionStore(settings.session_dir)
    session = store.get_or_create_owned(session_id, agent_id="main-agent")
    session.sdk_session_id = "stale-sdk"
    session.sdk_project_key = project_key_for_directory(str(settings.main_workspace_dir))
    session.sdk_store_ready_at = "2026-07-13T00:00:00+00:00"
    store.save(session)
    return store


def test_session_owner_is_claimed_once_and_cannot_change(tmp_path):
    settings = _settings(tmp_path)
    store = LocalSessionStore(settings.session_dir)

    claimed = store.get_or_create_owned("sess-owned", agent_id="soc-ops")

    assert claimed.agent_id == "soc-ops"
    with pytest.raises(SessionConflictError, match="different business agent"):
        store.get_or_create_owned("sess-owned", agent_id="other-agent")
    detached = store.get("sess-owned")
    assert detached is not None
    detached.agent_id = "other-agent"
    with pytest.raises(SessionConflictError, match="different business agent"):
        store.save(detached)
    assert store.get("sess-owned").agent_id == "soc-ops"


def test_historical_session_without_owner_cannot_be_claimed(tmp_path):
    settings = _settings(tmp_path)
    store = LocalSessionStore(settings.session_dir)
    store.save(LocalSession(session_id="sess-legacy", sdk_session_id="sdk-legacy", turns=1))

    with pytest.raises(SessionConflictError, match="no unambiguous business agent owner"):
        store.get_or_create_owned("sess-legacy", agent_id="soc-ops")


def test_concurrent_session_completion_cannot_overwrite_sdk_mapping_when_timestamps_match(tmp_path, monkeypatch):
    monkeypatch.setattr("app.runtime.session_store.utc_now", lambda: "2026-01-01T00:00:00+00:00")
    settings = _settings(tmp_path)
    store = LocalSessionStore(settings.session_dir)
    store.get_or_create_owned("sess-concurrent", agent_id="soc-ops")
    first = store.get("sess-concurrent")
    assert first is not None
    first = store.claim_turn(first, run_id="run-first", agent_id="soc-ops")
    second = store.get("sess-concurrent")
    assert second is not None

    store.complete_turn(
        first,
        run_id="run-first",
        agent_id="soc-ops",
        sdk_session_id="sdk-first",
        title="first",
    )
    with pytest.raises(SessionConflictError, match="changed concurrently"):
        store.complete_turn(
            second,
            run_id="run-first",
            agent_id="soc-ops",
            sdk_session_id="sdk-second",
            title="second",
        )

    saved = store.get("sess-concurrent")
    assert saved is not None
    assert saved.sdk_session_id == "sdk-first"
    assert saved.turns == 1


def test_active_turn_blocks_delete_until_claim_is_released(tmp_path):
    settings = _settings(tmp_path)
    store = LocalSessionStore(settings.session_dir)
    session = store.get_or_create_owned("sess-active", agent_id="soc-ops")
    claimed = store.claim_turn(session, run_id="run-active", agent_id="soc-ops")

    assert claimed.active_run_id == "run-active"
    with pytest.raises(SessionConflictError, match="active turn"):
        store.delete("sess-active")

    assert store.release_turn("sess-active", run_id="run-active") is True
    assert store.delete("sess-active") is True


def test_expired_turn_lease_can_be_reclaimed(tmp_path):
    settings = _settings(tmp_path)
    store = LocalSessionStore(settings.session_dir)
    session = store.get_or_create_owned("sess-expired", agent_id="soc-ops")
    store.claim_turn(session, run_id="run-expired", agent_id="soc-ops", lease_seconds=-1)
    latest = store.get("sess-expired")

    assert latest is not None and latest.active_run_id is None
    reclaimed = store.claim_turn(latest, run_id="run-new", agent_id="soc-ops")
    assert reclaimed.active_run_id == "run-new"


def test_turn_lease_renewal_extends_expiry_without_changing_completion_version(tmp_path):
    settings = _settings(tmp_path)
    store = LocalSessionStore(settings.session_dir)
    session = store.get_or_create_owned("sess-renew", agent_id="soc-ops")
    claimed = store.claim_turn(session, run_id="run-renew", agent_id="soc-ops", lease_seconds=1)

    renewed_expiry = store.renew_turn("sess-renew", run_id="run-renew", lease_seconds=120)
    renewed = store.get("sess-renew")

    assert renewed is not None
    assert renewed.active_run_expires_at == renewed_expiry
    assert renewed_expiry > claimed.active_run_expires_at
    completed = store.complete_turn(
        claimed,
        run_id="run-renew",
        agent_id="soc-ops",
        sdk_session_id="sdk-renewed",
        title="renewed",
    )
    assert completed.turns == 1
    assert completed.active_run_id is None


def test_turn_lease_renewal_rejects_wrong_run_id(tmp_path):
    settings = _settings(tmp_path)
    store = LocalSessionStore(settings.session_dir)
    session = store.get_or_create_owned("sess-renew-fenced", agent_id="soc-ops")
    store.claim_turn(session, run_id="run-owner", agent_id="soc-ops")

    with pytest.raises(SessionConflictError, match="no longer owned"):
        store.renew_turn("sess-renew-fenced", run_id="run-intruder")

    saved = store.get("sess-renew-fenced")
    assert saved is not None and saved.active_run_id == "run-owner"


def test_non_stream_turn_renews_lease_while_sdk_query_runs(tmp_path, monkeypatch):
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    renewed = asyncio.Event()

    async def fake_query(*, prompt, options, transport=None):
        async for _ in prompt:
            pass
        await asyncio.wait_for(renewed.wait(), timeout=1)
        sdk_session_id = options.resume or options.session_id
        await options.session_store.append(
            {"project_key": options.session_store.binding.project_key, "session_id": sdk_session_id},
            [{"type": "user", "uuid": "heartbeat-entry"}],
        )
        yield AssistantMessage(content=[TextBlock(text="renewed")], model="<synthetic>", session_id=sdk_session_id)
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id=sdk_session_id,
            result="renewed",
        )

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    monkeypatch.setattr(session_turn_lease, "DEFAULT_SESSION_TURN_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(session_turn_lease, "DEFAULT_SESSION_TURN_LEASE_SECONDS", 1.0)
    settings = _settings(tmp_path)
    store = LocalSessionStore(settings.session_dir)
    original_renew = store.renew_turn

    def observe_renewal(session_id, *, run_id, run_generation=None, lease_seconds=None):
        expires_at = original_renew(
            session_id,
            run_id=run_id,
            run_generation=run_generation,
            lease_seconds=lease_seconds,
        )
        renewed.set()
        return expires_at

    monkeypatch.setattr(store, "renew_turn", observe_renewal)
    runtime = ClaudeRuntime(settings, store)

    response = asyncio.run(runtime.run(ChatRequest(message="hello", session_id="sess-heartbeat-run")))

    saved = store.get("sess-heartbeat-run")
    assert response.answer == "renewed"
    assert renewed.is_set()
    assert saved is not None and saved.turns == 1 and saved.active_run_id is None


def test_stream_turn_fails_closed_when_lease_renewal_loses_ownership(tmp_path, monkeypatch):
    query_cancelled = asyncio.Event()

    async def blocking_query(*, prompt, options, transport=None):
        try:
            async for _ in prompt:
                pass
            await asyncio.Event().wait()
        finally:
            query_cancelled.set()
        if False:  # pragma: no cover - keep this function an async generator
            yield None

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", blocking_query)
    monkeypatch.setattr(session_turn_lease, "DEFAULT_SESSION_TURN_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(session_turn_lease, "DEFAULT_SESSION_TURN_LEASE_SECONDS", 1.0)
    settings = _settings(tmp_path)
    store = LocalSessionStore(settings.session_dir)

    def lose_lease(session_id, *, run_id, run_generation=None, lease_seconds=None):
        raise SessionConflictError(f"Session {session_id} lease lost for {run_id}")

    monkeypatch.setattr(store, "renew_turn", lose_lease)
    runtime = ClaudeRuntime(settings, store)

    async def exercise_failure():
        with pytest.raises(SessionConflictError, match="lease lost"):
            await _collect_stream(runtime, ChatRequest(message="hello", session_id="sess-heartbeat-lost"))
        await asyncio.wait_for(query_cancelled.wait(), timeout=1)

    asyncio.run(exercise_failure())

    saved = store.get("sess-heartbeat-lost")
    assert saved is not None and saved.turns == 0 and saved.active_run_id is None


@pytest.mark.parametrize("via_responses_sse", [False, True], ids=["runtime", "responses-sse"])
def test_client_cancel_closes_sdk_task_and_discards_unfinished_turn(tmp_path, monkeypatch, via_responses_sse):
    from claude_agent_sdk import AssistantMessage, TextBlock

    query_cancelled = asyncio.Event()
    sdk_task: dict[str, asyncio.Task] = {}

    async def blocking_query(*, prompt, options, transport=None):
        async for _ in prompt:
            pass
        sdk_session_id = options.resume or options.session_id
        await options.session_store.append(
            {"project_key": options.session_store.binding.project_key, "session_id": sdk_session_id},
            [{"type": "assistant", "uuid": "partial-entry"}],
        )
        current_task = asyncio.current_task()
        assert current_task is not None
        sdk_task["value"] = current_task
        try:
            yield AssistantMessage(content=[TextBlock(text="partial")], model="<synthetic>", session_id=sdk_session_id)
            await asyncio.Event().wait()
        finally:
            query_cancelled.set()

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", blocking_query)
    settings = _settings(tmp_path)
    store = LocalSessionStore(settings.session_dir)
    feedback_store = FeedbackStore(data_dir=settings.data_dir, workspace_dir=settings.main_workspace_dir)
    runtime = ClaudeRuntime(settings, store, feedback_store)
    session_id = "sess-client-cancel"

    async def exercise_cancel() -> str:
        runtime_source = runtime.stream(ChatRequest(message="hello", session_id=session_id))
        if via_responses_sse:
            consumer = iter_responses_sse(
                runtime_source,
                model="test-model",
                effective_agent_id="main-agent",
                control=True,
            )
            async for chunk in consumer:
                if "event: response.output_text.delta" in chunk:
                    break
        else:
            consumer = runtime_source
            async for event in consumer:
                if event["event"] == "message":
                    break

        claimed = store.get(session_id)
        assert claimed is not None and claimed.active_run_id is not None
        run_id = claimed.active_run_id
        await close_async_iterator(consumer)
        await asyncio.wait_for(query_cancelled.wait(), timeout=1)
        assert sdk_task["value"].done()
        return run_id

    run_id = asyncio.run(exercise_cancel())
    saved = store.get(session_id)

    assert saved is not None
    assert saved.turns == 0
    assert saved.sdk_session_id is None
    assert saved.active_run_id is None
    assert feedback_store.find_run(run_id=run_id) is not None


def test_client_cancel_closes_sdk_task_when_hitl_cleanup_fails(tmp_path, monkeypatch):
    from claude_agent_sdk import AssistantMessage, TextBlock

    query_cancelled = asyncio.Event()
    cleanup_attempted = asyncio.Event()
    sdk_task: dict[str, asyncio.Task] = {}

    async def blocking_query(*, prompt, options, transport=None):
        async for _ in prompt:
            pass
        sdk_session_id = options.resume or options.session_id
        await options.session_store.append(
            {"project_key": options.session_store.binding.project_key, "session_id": sdk_session_id},
            [{"type": "assistant", "uuid": "partial-entry"}],
        )
        current_task = asyncio.current_task()
        assert current_task is not None
        sdk_task["value"] = current_task
        try:
            yield AssistantMessage(content=[TextBlock(text="partial")], model="<synthetic>", session_id=sdk_session_id)
            await asyncio.Event().wait()
        finally:
            query_cancelled.set()

    class FailingUserInputService:
        async def cancel_run(self, run_id, *, decision="client_cancelled"):
            cleanup_attempted.set()
            raise RuntimeError("injected HITL cleanup failure")

        def clear_run_grants(self, run_id):
            pass

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", blocking_query)
    settings = _settings(tmp_path)
    store = LocalSessionStore(settings.session_dir)
    feedback_store = FeedbackStore(data_dir=settings.data_dir, workspace_dir=settings.main_workspace_dir)
    runtime = ClaudeRuntime(
        settings,
        store,
        feedback_store,
        user_input_service=FailingUserInputService(),
    )
    session_id = "sess-client-cancel-cleanup-error"

    async def exercise_cancel() -> str:
        runtime_source = runtime.stream(ChatRequest(message="hello", session_id=session_id))
        consumer = iter_responses_sse(
            runtime_source,
            model="test-model",
            effective_agent_id="main-agent",
            control=True,
        )
        async for chunk in consumer:
            if "event: response.output_text.delta" in chunk:
                break

        claimed = store.get(session_id)
        assert claimed is not None and claimed.active_run_id is not None
        run_id = claimed.active_run_id
        with pytest.raises(RuntimeError, match="injected HITL cleanup failure"):
            await close_async_iterator(consumer)
        await asyncio.wait_for(query_cancelled.wait(), timeout=1)
        assert cleanup_attempted.is_set()
        assert sdk_task["value"].done()
        return run_id

    run_id = asyncio.run(exercise_cancel())
    saved = store.get(session_id)

    assert saved is not None
    assert saved.turns == 0
    assert saved.sdk_session_id is None
    assert saved.active_run_id is None
    assert feedback_store.find_run(run_id=run_id) is not None


def test_blocking_sdk_stream_cannot_lose_session_mapping_to_concurrent_delete(tmp_path, monkeypatch):
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_query(*, prompt, options, transport=None):
        async for _ in prompt:
            pass
        started.set()
        await release.wait()
        sdk_session_id = options.resume or options.session_id
        await options.session_store.append(
            {"project_key": options.session_store.binding.project_key, "session_id": sdk_session_id},
            [{"type": "user", "uuid": "blocking-entry"}],
        )
        yield AssistantMessage(
            content=[TextBlock(text="completed")],
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
            result="completed",
        )

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    settings = _settings(tmp_path)
    store = LocalSessionStore(settings.session_dir)
    runtime = ClaudeRuntime(settings, store)

    async def exercise_race():
        task = asyncio.create_task(_collect_stream(runtime, ChatRequest(message="hello", session_id="sess-stream-active")))
        await asyncio.wait_for(started.wait(), timeout=5)
        with pytest.raises(SessionConflictError, match="active turn"):
            store.delete("sess-stream-active")
        release.set()
        return await asyncio.wait_for(task, timeout=5)

    events = asyncio.run(exercise_race())
    saved = store.get("sess-stream-active")

    assert saved is not None
    assert uuid.UUID(saved.sdk_session_id)
    assert saved.turns == 1
    assert saved.active_run_id is None
    assert "error" not in [event["event"] for event in events]


async def _collect_stream(runtime: ClaudeRuntime, request: ChatRequest):
    return [event async for event in runtime.stream(request)]


def test_build_options_does_not_reuse_api_session_id_after_resume_is_cleared(tmp_path):
    settings = _settings(tmp_path)
    store = LocalSessionStore(settings.session_dir)
    runtime = ClaudeRuntime(settings, store)
    session_id = str(uuid.uuid4())
    session = store.get_or_create_owned(session_id, agent_id="main-agent")

    first_options = runtime._build_options(ChatRequest(message="first", session_id=session_id), session)
    assert getattr(first_options, "session_id", None) == session_id
    assert getattr(first_options, "resume", None) is None

    session = store.claim_turn(session, run_id="run-first", agent_id="main-agent")
    store.complete_turn(
        session,
        run_id="run-first",
        agent_id="main-agent",
        sdk_session_id=None,
        title="first",
    )
    resumed_after_config_change = store.get(session_id)
    assert resumed_after_config_change is not None
    second_options = runtime._build_options(
        ChatRequest(message="second", session_id=session_id),
        resumed_after_config_change,
    )

    assert getattr(second_options, "session_id", None) is None
    assert getattr(second_options, "resume", None) is None


def test_run_retries_once_when_saved_sdk_session_is_missing(tmp_path, monkeypatch):
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    calls = []

    async def fake_query(*, prompt, options, transport=None):
        calls.append(getattr(options, "resume", None))
        async for _ in prompt:
            pass
        if len(calls) == 1:
            raise RuntimeError("No conversation found with session ID: stale-sdk")
        yield AssistantMessage(content=[TextBlock(text="hello after retry")], model="<synthetic>", session_id="new-sdk")
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="new-sdk",
            result="hello after retry",
        )

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    settings = _settings(tmp_path)
    store = _store_with_stale_session(settings, "sess-stale")
    runtime = ClaudeRuntime(settings, store)

    result = asyncio.run(runtime.run(ChatRequest(message="hello", session_id="sess-stale")))

    saved = store.get("sess-stale")
    assert calls == ["stale-sdk"]
    assert result.answer == ""
    assert result.errors == ["RuntimeError: No conversation found with session ID: stale-sdk"]
    assert result.sdk_session_id == "stale-sdk"
    assert saved is not None
    assert saved.sdk_session_id == "stale-sdk"
    assert saved.turns == 0


def test_stream_retries_once_when_saved_sdk_session_is_missing(tmp_path, monkeypatch):
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    calls = []

    async def fake_query(*, prompt, options, transport=None):
        calls.append(getattr(options, "resume", None))
        async for _ in prompt:
            pass
        if len(calls) == 1:
            raise RuntimeError("No conversation found with session ID: stale-sdk")
        yield AssistantMessage(
            content=[TextBlock(text="stream after retry")],
            model="<synthetic>",
            session_id="new-stream-sdk",
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="new-stream-sdk",
            result="stream after retry",
        )

    async def collect(runtime):
        req = ChatRequest(message="stream", session_id="sess-stream-stale")
        return [item async for item in runtime.stream(req)]

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    settings = _settings(tmp_path)
    store = _store_with_stale_session(settings, "sess-stream-stale")
    runtime = ClaudeRuntime(settings, store)

    events = asyncio.run(collect(runtime))

    saved = store.get("sess-stream-stale")
    assert calls == ["stale-sdk"]
    assert [event["event"] for event in events] == ["session", "error", "done"]
    assert saved is not None
    assert saved.sdk_session_id == "stale-sdk"
    assert saved.turns == 0


def test_stream_retries_when_process_error_stderr_reports_missing_session(tmp_path, monkeypatch):
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
    from claude_agent_sdk._errors import ProcessError

    calls = []

    async def fake_query(*, prompt, options, transport=None):
        calls.append(getattr(options, "resume", None))
        async for _ in prompt:
            pass
        if len(calls) == 1:
            raise ProcessError(
                "Command failed with exit code 1 (exit code: 1)\nError output: Check stderr output for details",
                exit_code=1,
                stderr="No conversation found with session ID: stale-sdk",
            )
        yield AssistantMessage(
            content=[TextBlock(text="stream after stderr retry")],
            model="<synthetic>",
            session_id="new-sdk",
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="new-sdk",
            result="stream after stderr retry",
        )

    async def collect(runtime):
        req = ChatRequest(message="stream", session_id="sess-process-stale")
        return [item async for item in runtime.stream(req)]

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    settings = _settings(tmp_path)
    store = _store_with_stale_session(settings, "sess-process-stale")
    runtime = ClaudeRuntime(settings, store)

    events = asyncio.run(collect(runtime))

    saved = store.get("sess-process-stale")
    assert calls == ["stale-sdk"]
    assert "result" not in [event["event"] for event in events]
    assert "error" in [event["event"] for event in events]
    assert saved is not None
    assert saved.sdk_session_id == "stale-sdk"
    assert saved.turns == 0


def test_stream_retries_when_process_error_hides_stderr_detail(tmp_path, monkeypatch):
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
    from claude_agent_sdk._errors import ProcessError

    calls = []

    async def fake_query(*, prompt, options, transport=None):
        calls.append(getattr(options, "resume", None))
        async for _ in prompt:
            pass
        if len(calls) == 1:
            raise ProcessError(
                "Command failed with exit code 1 (exit code: 1)\nError output: Check stderr output for details",
                exit_code=1,
            )
        yield AssistantMessage(
            content=[TextBlock(text="stream after hidden stderr retry")],
            model="<synthetic>",
            session_id="new-sdk",
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="new-sdk",
            result="stream after hidden stderr retry",
        )

    async def collect(runtime):
        req = ChatRequest(message="stream", session_id="sess-hidden-stale")
        return [item async for item in runtime.stream(req)]

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    settings = _settings(tmp_path)
    store = _store_with_stale_session(settings, "sess-hidden-stale")
    runtime = ClaudeRuntime(settings, store)

    events = asyncio.run(collect(runtime))

    saved = store.get("sess-hidden-stale")
    assert calls == ["stale-sdk"]
    assert "result" not in [event["event"] for event in events]
    assert "error" in [event["event"] for event in events]
    assert saved is not None
    assert saved.sdk_session_id == "stale-sdk"
    assert saved.turns == 0
