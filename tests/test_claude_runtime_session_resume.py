import asyncio

from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.schemas import ChatRequest
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import AppSettings


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
        ENABLE_POLICY_HOOKS=True,
    )


def _store_with_stale_session(settings, session_id):
    store = LocalSessionStore(settings.session_dir)
    session = store.get_or_create(session_id)
    session.sdk_session_id = "stale-sdk"
    store.save(session)
    return store


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
    assert calls == ["stale-sdk", None]
    assert result.answer == "hello after retry"
    assert result.errors == []
    assert result.sdk_session_id == "new-sdk"
    assert saved is not None
    assert saved.sdk_session_id == "new-sdk"
    assert saved.turns == 1


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
    result_event = next(event for event in events if event["event"] == "result")
    assert calls == ["stale-sdk", None]
    assert [event["event"] for event in events] == ["session", "message", "message", "result", "done"]
    assert "error" not in [event["event"] for event in events]
    assert result_event["data"]["sdk_session_id"] == "new-stream-sdk"
    assert saved is not None
    assert saved.sdk_session_id == "new-stream-sdk"
    assert saved.turns == 1
