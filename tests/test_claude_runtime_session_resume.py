import asyncio
import json
import uuid

from app.runtime.business_agent_workspace import seed_business_agent_workspace
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.schemas import ChatRequest
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import AppSettings


def _settings(tmp_path):
    data = tmp_path / "docker" / "volume" / "data"
    settings = AppSettings(
        _env_file=None,
        DATA_DIR=data,
        GOVERNOR_CLAUDE_ROOT=tmp_path / "docker" / "volume" / "claude-roots" / "governor",
        RUNTIME_VOLUME_MODE="local-debug",
    )
    workspace = settings.main_workspace_dir
    seed_business_agent_workspace(workspace, agent_id="main-agent", name="Main Agent")
    (workspace / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "sec-ops-data": {
                        "type": "http",
                        "url": "http://localhost:58001/mcp",
                    }
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return settings


def _store_with_stale_session(settings, session_id):
    store = LocalSessionStore(settings.session_dir)
    session = store.get_or_create(session_id)
    session.sdk_session_id = "stale-sdk"
    store.save(session)
    return store


def test_build_options_does_not_reuse_api_session_id_after_resume_is_cleared(tmp_path):
    settings = _settings(tmp_path)
    store = LocalSessionStore(settings.session_dir)
    runtime = ClaudeRuntime(settings, store)
    session_id = str(uuid.uuid4())
    session = store.get_or_create(session_id)

    first_options = runtime._build_options(ChatRequest(message="first", session_id=session_id), session)
    assert getattr(first_options, "session_id", None) == session_id
    assert getattr(first_options, "resume", None) is None

    session.turns = 1
    session.sdk_session_id = None
    store.save(session)
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
    result_event = next(event for event in events if event["event"] == "result")
    assert calls == ["stale-sdk", None]
    assert "error" not in [event["event"] for event in events]
    assert result_event["data"]["sdk_session_id"] == "new-sdk"
    assert saved is not None
    assert saved.sdk_session_id == "new-sdk"


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
    result_event = next(event for event in events if event["event"] == "result")
    assert calls == ["stale-sdk", None]
    assert "error" not in [event["event"] for event in events]
    assert result_event["data"]["sdk_session_id"] == "new-sdk"
    assert saved is not None
    assert saved.sdk_session_id == "new-sdk"
