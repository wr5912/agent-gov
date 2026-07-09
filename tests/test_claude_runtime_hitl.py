import asyncio
import json

from app.runtime.agent_paths import business_agent_layout
from app.runtime.agent_profiles import build_business_agent_profile
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.claude_user_input_schemas import ClaudeUserInputDecisionRequest
from app.runtime.claude_user_input_service import ClaudeUserInputService
from app.runtime.hitl_policy import SECURITY_OPERATIONS_EXECUTE_TOOL, SECURITY_OPERATIONS_EXPERT_AGENT_ID
from app.runtime.runtime_db import make_session_factory
from app.runtime.schemas import ChatRequest
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import AppSettings
from app.runtime.stores.claude_user_input_store import ClaudeUserInputStore


def _settings(
    tmp_path,
    *,
    enable_hitl: bool,
    requires_web_hitl: bool = False,
    agent_id: str = "main-agent",
    ask_rules: list[str] | None = None,
) -> AppSettings:
    workspace = tmp_path / "volume" / "data" / "business-agents" / agent_id / "workspace"
    data = tmp_path / "volume" / "data"
    claude_root = tmp_path / "volume" / "data" / "business-agents" / agent_id / "claude-root"
    claude_home = claude_root / ".claude"
    workspace.mkdir(parents=True, exist_ok=True)
    workspace.joinpath(".claude").mkdir(parents=True, exist_ok=True)
    workspace.joinpath(".claude", "settings.json").write_text(
        json.dumps(
            {
                "permissions": {
                    "allow": ["Bash(*)"],
                    "ask": ask_rules or ["mcp__soc-playbook-execution__*"],
                    "deny": [],
                }
            }
        ),
        encoding="utf-8",
    )
    if requires_web_hitl:
        # 部署契约声明：build_business_agent_profile 读此字段 -> profile.requires_web_hitl=True
        workspace.joinpath("agent.yaml").write_text(f"agent:\n  id: {agent_id}\n  requires_web_hitl: true\n", encoding="utf-8")
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


def _pre_tool_hook_matchers(options: object) -> set[str]:
    return {getattr(matcher, "matcher", "") for matcher in getattr(options, "hooks", {}).get("PreToolUse", [])}


def _profile(settings: AppSettings, agent_id: str):
    return build_business_agent_profile(settings, agent_id=agent_id, workspace_dir=business_agent_layout(settings.data_dir, agent_id).workspace)


def test_stream_hitl_emits_wait_event_and_resumes_sdk_after_allow(tmp_path, monkeypatch):
    seen = {}

    async def fake_query(*, prompt, options, transport=None):
        seen["options"] = options
        seen["prompt_item"] = await anext(prompt)
        seen["permission_result"] = await options.can_use_tool(
            "mcp__soc-playbook-execution__submit",
            {"playbook_id": "pb-1"},
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
    assert {"Bash", "Write", "Edit"}.issubset(_pre_tool_hook_matchers(seen["options"]))
    assert getattr(seen["options"], "permission_prompt_tool_name", None) is None
    assert seen["prompt_item"]["message"]["content"] == "needs tool approval"
    assert seen["permission_result"].__class__.__name__ == "PermissionResultAllow"
    record = store.list(run_id=str(events[0]["data"]["run_id"]))[0]
    assert record.status == "resolved"
    assert record.decision == "allow_once"


def test_stream_security_operations_expert_only_execute_requests_hitl(tmp_path, monkeypatch):
    seen = {}

    async def fake_query(*, prompt, options, transport=None):
        seen["options"] = options
        await anext(prompt)
        seen["manual_result"] = await options.can_use_tool(
            "mcp__sec-ops__soc_api__manual",
            {"playbookId": "pb-1"},
            {"tool_use_id": "manual"},
        )
        seen["write_result"] = await options.can_use_tool("Write", {"file_path": "./notes.md"}, {"tool_use_id": "write"})
        seen["question_result"] = await options.can_use_tool("AskUserQuestion", {"question": "confirm?"}, {"tool_use_id": "ask"})
        seen["execute_result"] = await options.can_use_tool(
            SECURITY_OPERATIONS_EXECUTE_TOOL,
            {"actionKey": "isolate_host"},
            {"tool_use_id": "execute"},
        )
        if False:
            yield None

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    settings = _settings(
        tmp_path,
        enable_hitl=True,
        requires_web_hitl=True,
        agent_id=SECURITY_OPERATIONS_EXPERT_AGENT_ID,
        ask_rules=[SECURITY_OPERATIONS_EXECUTE_TOOL],
    )
    service, store = _service(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), user_input_service=service)
    profile = _profile(settings, SECURITY_OPERATIONS_EXPERT_AGENT_ID)

    async def scenario():
        stream = runtime.stream(ChatRequest(message="dispose"), profile=profile)
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
    assert seen["manual_result"].__class__.__name__ == "PermissionResultAllow"
    assert seen["write_result"].__class__.__name__ == "PermissionResultAllow"
    assert seen["question_result"].__class__.__name__ == "PermissionResultDeny"
    assert seen["execute_result"].__class__.__name__ == "PermissionResultAllow"
    records = store.list(run_id=str(events[0]["data"]["run_id"]))
    assert [record.tool_name for record in records] == [SECURITY_OPERATIONS_EXECUTE_TOOL]


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
    assert {"Bash", "Write", "Edit"}.issubset(_pre_tool_hook_matchers(seen["options"]))
    assert getattr(seen["options"], "permission_mode", None) is None
    assert seen["prompt_item"]["message"]["content"] == "no hitl"
    assert store.list(limit=10) == []


def test_stream_fail_loud_when_hitl_required_but_disabled(tmp_path, monkeypatch):
    # requires_web_hitl 的 Agent + HITL 关：挂 fail-loud deny 回调（非静默 None），命中 ask 工具 deny + error 事件
    seen = {}

    async def fake_query(*, prompt, options, transport=None):
        seen["options"] = options
        await anext(prompt)
        seen["permission_result"] = await options.can_use_tool("mcp__soc-playbook-execution__submit", {}, {"tool_use_id": "t1"})
        if False:
            yield None

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    settings = _settings(tmp_path, enable_hitl=False, requires_web_hitl=True)
    service, store = _service(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), user_input_service=service)

    async def collect():
        return [event async for event in runtime.stream(ChatRequest(message="dispose"))]

    events = asyncio.run(asyncio.wait_for(collect(), timeout=5))
    names = [event["event"] for event in events]
    assert getattr(seen["options"], "can_use_tool", None) is not None  # fail-loud 回调已挂
    assert seen["permission_result"].__class__.__name__ == "PermissionResultDeny"
    assert "ENABLE_CLAUDE_WEB_HITL" in seen["permission_result"].message
    assert "error" in names  # 命中 ask 工具产明确 error 事件（非静默）
    assert store.list(limit=10) == []  # 未创建 HITL 请求（HITL 关）


def test_stream_security_operations_expert_disabled_hitl_only_execute_fails_loud(tmp_path, monkeypatch):
    seen = {}

    async def fake_query(*, prompt, options, transport=None):
        seen["options"] = options
        await anext(prompt)
        seen["manual_result"] = await options.can_use_tool("mcp__sec-ops__soc_api__manual", {}, {"tool_use_id": "manual"})
        seen["execute_result"] = await options.can_use_tool(SECURITY_OPERATIONS_EXECUTE_TOOL, {}, {"tool_use_id": "execute"})
        if False:
            yield None

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    settings = _settings(
        tmp_path,
        enable_hitl=False,
        requires_web_hitl=True,
        agent_id=SECURITY_OPERATIONS_EXPERT_AGENT_ID,
        ask_rules=[SECURITY_OPERATIONS_EXECUTE_TOOL],
    )
    service, store = _service(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), user_input_service=service)
    profile = _profile(settings, SECURITY_OPERATIONS_EXPERT_AGENT_ID)

    async def collect():
        return [event async for event in runtime.stream(ChatRequest(message="dispose"), profile=profile)]

    events = asyncio.run(asyncio.wait_for(collect(), timeout=5))
    names = [event["event"] for event in events]
    assert seen["manual_result"].__class__.__name__ == "PermissionResultAllow"
    assert seen["execute_result"].__class__.__name__ == "PermissionResultDeny"
    assert "ENABLE_CLAUDE_WEB_HITL" in seen["execute_result"].message
    assert "error" in names
    assert store.list(limit=10) == []


def test_run_fail_loud_when_hitl_required(tmp_path, monkeypatch):
    # 非流式 run() + requires_web_hitl：permission_mode=default（非 bypassPermissions）+ can_use_tool deny ask 型工具
    seen = {}

    async def fake_query(*, prompt, options, transport=None):
        seen["options"] = options
        seen["permission_result"] = await options.can_use_tool("mcp__soc-playbook-execution__submit", {}, {"tool_use_id": "t1"})
        if False:
            yield None

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    settings = _settings(tmp_path, enable_hitl=False, requires_web_hitl=True)
    service, _store = _service(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), user_input_service=service)

    asyncio.run(runtime.run(ChatRequest(message="dispose")))
    assert getattr(seen["options"], "permission_mode", None) == "default"  # 非 bypassPermissions
    assert getattr(seen["options"], "can_use_tool", None) is not None
    assert seen["permission_result"].__class__.__name__ == "PermissionResultDeny"


def test_run_security_operations_expert_non_execute_tools_are_direct_allowed(tmp_path, monkeypatch):
    seen = {}

    async def fake_query(*, prompt, options, transport=None):
        seen["options"] = options
        seen["manual_result"] = await options.can_use_tool("mcp__sec-ops__soc_api__manual", {}, {"tool_use_id": "manual"})
        seen["write_result"] = await options.can_use_tool("Write", {"file_path": "./notes.md"}, {"tool_use_id": "write"})
        seen["question_result"] = await options.can_use_tool("AskUserQuestion", {"question": "confirm?"}, {"tool_use_id": "ask"})
        seen["execute_result"] = await options.can_use_tool(SECURITY_OPERATIONS_EXECUTE_TOOL, {}, {"tool_use_id": "execute"})
        if False:
            yield None

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    settings = _settings(
        tmp_path,
        enable_hitl=False,
        requires_web_hitl=True,
        agent_id=SECURITY_OPERATIONS_EXPERT_AGENT_ID,
        ask_rules=[SECURITY_OPERATIONS_EXECUTE_TOOL],
    )
    service, _store = _service(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), user_input_service=service)
    profile = _profile(settings, SECURITY_OPERATIONS_EXPERT_AGENT_ID)

    asyncio.run(runtime.run(ChatRequest(message="dispose"), profile=profile))
    assert getattr(seen["options"], "permission_mode", None) == "default"
    assert seen["manual_result"].__class__.__name__ == "PermissionResultAllow"
    assert seen["write_result"].__class__.__name__ == "PermissionResultAllow"
    assert seen["question_result"].__class__.__name__ == "PermissionResultDeny"
    assert seen["execute_result"].__class__.__name__ == "PermissionResultDeny"


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
