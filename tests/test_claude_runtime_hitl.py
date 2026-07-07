import asyncio
import json

from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.claude_user_input_schemas import ClaudeUserInputDecisionRequest
from app.runtime.claude_user_input_service import ClaudeUserInputService
from app.runtime.runtime_db import make_session_factory
from app.runtime.schemas import ChatRequest
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import AppSettings
from app.runtime.stores.claude_user_input_store import ClaudeUserInputStore


def _settings(tmp_path, *, enable_hitl: bool) -> AppSettings:
    workspace = tmp_path / "volume" / "data" / "business-agents" / "main-agent" / "workspace"
    data = tmp_path / "volume" / "data"
    claude_root = tmp_path / "volume" / "data" / "business-agents" / "main-agent" / "claude-root"
    claude_home = claude_root / ".claude"
    workspace.mkdir(parents=True, exist_ok=True)
    workspace.joinpath(".claude").mkdir(parents=True, exist_ok=True)
    workspace.joinpath(".claude", "settings.json").write_text(
        json.dumps({"permissions": {"allow": [], "ask": ["Bash(*)"], "deny": []}}),
        encoding="utf-8",
    )
    claude_home.mkdir(parents=True, exist_ok=True)
    return AppSettings(
        _env_file=None,
        WORKSPACE_DIR=workspace,
        MAIN_WORKSPACE_DIR=workspace,
        DATA_DIR=data,
        CLAUDE_ROOT=claude_root,
        MAIN_CLAUDE_ROOT=claude_root,
        CLAUDE_HOME=claude_home,
        ENABLE_CLAUDE_WEB_HITL=enable_hitl,
        PERMISSION_PROMPT_TOOL_NAME="legacy-prompt-tool",
    )


def _service(tmp_path) -> tuple[ClaudeUserInputService, ClaudeUserInputStore]:
    store = ClaudeUserInputStore(make_session_factory(tmp_path / "runtime.sqlite3"))
    return ClaudeUserInputService(store, timeout_seconds=5), store


def _decision_from_request(request: dict[str, object], *, action: str = "allow_once") -> ClaudeUserInputDecisionRequest:
    return ClaudeUserInputDecisionRequest.model_validate({"action": action, "decision_token": request["decision_token"]})


def test_stream_hitl_emits_wait_event_and_resumes_sdk_after_allow(tmp_path, monkeypatch):
    seen = {}

    async def fake_query(*, prompt, options, transport=None):
        seen["options"] = options
        seen["prompt_item"] = await anext(prompt)
        seen["permission_result"] = await options.can_use_tool(
            "Bash",
            {"command": "echo hi"},
            {"tool_use_id": "toolu-hitl", "agent_id": "sdk-subagent"},
        )
        if False:
            yield None

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    settings = _settings(tmp_path, enable_hitl=True)
    service, store = _service(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), user_input_service=service)

    async def scenario():
        stream = runtime.stream(ChatRequest(message="needs tool approval"))
        session_event = await anext(stream)
        required_event = await anext(stream)
        request = required_event["data"]
        service.submit_decision(
            str(request["request_id"]),
            decision=_decision_from_request(request),
            decided_by="tester",
        )
        rest = []
        async for event in stream:
            rest.append(event)
        return [session_event, required_event, *rest]

    events = asyncio.run(scenario())

    assert [event["event"] for event in events] == [
        "session",
        "claude_user_input_required",
        "claude_user_input_resolved",
        "done",
    ]
    assert getattr(seen["options"], "permission_mode", None) == "default"
    assert getattr(seen["options"], "can_use_tool", None) is not None
    assert getattr(seen["options"], "hooks", None) is None
    assert getattr(seen["options"], "permission_prompt_tool_name", None) is None
    assert seen["prompt_item"]["message"]["content"] == "needs tool approval"
    assert seen["permission_result"].__class__.__name__ == "PermissionResultAllow"
    record = store.list(run_id=str(events[0]["data"]["run_id"]))[0]
    assert record.status == "resolved"
    assert record.decision == "allow_once"


def test_stream_does_not_attach_hitl_when_switch_is_disabled(tmp_path, monkeypatch):
    seen = {}

    async def fake_query(*, prompt, options, transport=None):
        seen["options"] = options
        seen["prompt_item"] = await anext(prompt)
        if False:
            yield None

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    settings = _settings(tmp_path, enable_hitl=False)
    service, store = _service(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), user_input_service=service)

    async def collect():
        return [event async for event in runtime.stream(ChatRequest(message="no hitl"))]

    events = asyncio.run(collect())

    assert [event["event"] for event in events] == ["session", "done"]
    assert getattr(seen["options"], "can_use_tool", None) is None
    assert getattr(seen["options"], "hooks", None) is None
    assert getattr(seen["options"], "permission_mode", None) is None
    assert seen["prompt_item"]["message"]["content"] == "no hitl"
    assert store.list(limit=10) == []


def test_stream_finishes_when_completion_step_raises(tmp_path, monkeypatch):
    async def fake_query(*, prompt, options, transport=None):
        await anext(prompt)
        if False:
            yield None

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    settings = _settings(tmp_path, enable_hitl=False)
    service, _store = _service(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), user_input_service=service)

    def fail_complete(*args, **kwargs):
        raise RuntimeError("completion boom")

    monkeypatch.setattr(runtime, "_complete_runtime_request", fail_complete)

    async def collect():
        return [event async for event in runtime.stream(ChatRequest(message="completion fails"))]

    async def scenario():
        return await asyncio.wait_for(collect(), timeout=2)

    events = asyncio.run(scenario())

    assert [event["event"] for event in events] == ["session", "error", "done"]
    assert "RuntimeError: completion boom" in events[1]["data"]["errors"]
