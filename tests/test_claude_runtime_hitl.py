import asyncio
import json
import shutil
import threading
from pathlib import Path

import pytest
from app.runtime import claude_prompt_suggestions, session_turn_lease
from app.runtime.agent_paths import business_agent_layout
from app.runtime.agent_profiles import build_business_agent_profile
from app.runtime.async_iterators import close_async_iterator
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.claude_user_input_schemas import ClaudeUserInputDecisionRequest
from app.runtime.claude_user_input_service import ClaudeUserInputService
from app.runtime.errors import SessionConflictError
from app.runtime.runtime_db import make_session_factory
from app.runtime.schemas import ChatRequest
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import AppSettings
from app.runtime.stores.claude_user_input_store import ClaudeUserInputStore

from claude_runtime_test_utils import main_profile_resolver

SECURITY_OPERATIONS_EXPERT_AGENT_ID = "security-operations-expert"
SOC_CREATE_TOOL = "mcp__sec-ops__soc_api__create"
SOC_MANUAL_TOOL = "mcp__sec-ops__soc_api__manual"
REPO_ROOT = Path(__file__).resolve().parents[1]
SEED_AGENT_ROOT = REPO_ROOT / "docker" / "runtime-volume-seeds" / "data" / "business-agents"


def _settings(
    tmp_path,
    *,
    enable_hitl: bool,
    agent_id: str = "main-agent",
    seed_agent_id: str | None = None,
    ask_rules: list[str] | None = None,
) -> AppSettings:
    workspace = tmp_path / "volume" / "data" / "business-agents" / agent_id / "workspace"
    data = tmp_path / "volume" / "data"
    claude_root = tmp_path / "volume" / "data" / "business-agents" / agent_id / "claude-root"
    claude_home = claude_root / ".claude"
    seed_workspace = SEED_AGENT_ROOT / (seed_agent_id or agent_id) / "workspace"
    shutil.copytree(seed_workspace, workspace)
    settings_path = workspace / ".claude" / "settings.json"
    settings_data = json.loads(settings_path.read_text(encoding="utf-8"))
    if ask_rules is not None:
        settings_data["permissions"]["ask"] = ask_rules
        settings_path.write_text(json.dumps(settings_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    claude_home.mkdir(parents=True, exist_ok=True)
    return AppSettings(
        _env_file=None,
        WORKSPACE_DIR=workspace,
        MAIN_WORKSPACE_DIR=workspace,
        DATA_DIR=data,
        CLAUDE_ROOT=claude_root,
        MAIN_CLAUDE_ROOT=claude_root,
        CLAUDE_HOME=claude_home,
        RUNTIME_VOLUME_MODE="local-debug",
        ENABLE_CLAUDE_WEB_HITL=enable_hitl,
    )


def _service(tmp_path) -> tuple[ClaudeUserInputService, ClaudeUserInputStore]:
    store = ClaudeUserInputStore(make_session_factory(tmp_path / "runtime.sqlite3"))
    return ClaudeUserInputService(store, timeout_seconds=5), store


def _decision_from_request(
    request: dict[str, object],
    *,
    action: str = "allow_once",
) -> ClaudeUserInputDecisionRequest:
    return ClaudeUserInputDecisionRequest.model_validate({"action": action, "decision_token": request["decision_token"]})


def _profile(settings: AppSettings, agent_id: str):
    return build_business_agent_profile(settings, agent_id=agent_id, workspace_dir=business_agent_layout(settings.data_dir, agent_id).workspace)


def _patch_interactive_sdk_client(monkeypatch, fake_query, seen: dict[str, object]) -> None:
    import claude_agent_sdk

    class FakeClaudeSDKClient:
        def __init__(self, *, options, transport=None):
            self.options = options
            self.responses = None
            self.control_task = None

        async def connect(self, control_stream):
            async def consume_control_stream():
                async for _ in control_stream:
                    pass
                seen["control_closed"] = True

            self.control_task = asyncio.create_task(consume_control_stream())
            await asyncio.sleep(0)
            assert not self.control_task.done()
            seen["control_opened"] = True

        async def query(self, prompt, session_id="default"):
            self.responses = fake_query(prompt=prompt, options=self.options)

        async def receive_response(self):
            from claude_agent_sdk import ResultMessage

            assert self.responses is not None
            async for message in self.responses:
                yield message
                if isinstance(message, ResultMessage):
                    return

        async def disconnect(self):
            if self.responses is not None:
                await close_async_iterator(self.responses)
            assert self.control_task is not None
            await asyncio.wait_for(self.control_task, timeout=1)
            seen["disconnected"] = True

    monkeypatch.setattr(claude_prompt_suggestions, "PromptSuggestionClaudeClient", FakeClaudeSDKClient)
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", FakeClaudeSDKClient)


async def _success_result(options, *, text: str):
    from claude_agent_sdk import ResultMessage

    sdk_session_id = options.resume or options.session_id
    await options.session_store.append(
        {"project_key": options.session_store.binding.project_key, "session_id": sdk_session_id},
        [{"type": "user", "uuid": f"{text}-entry"}],
    )
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=0,
        is_error=False,
        num_turns=1,
        session_id=sdk_session_id,
        result=text,
    )


def test_stream_hitl_consumes_prompt_eof_then_resumes_sdk_after_allow(tmp_path, monkeypatch):
    seen = {}

    async def fake_query(*, prompt, options, transport=None):
        seen["options"] = options
        seen["prompt_items"] = [item async for item in prompt]
        seen["control_open_at_callback"] = not seen.get("control_closed", False)
        seen["permission_result"] = await options.can_use_tool(
            "mcp__soc-playbook-execution__submit",
            {"playbook_id": "pb-1"},
            {"tool_use_id": "toolu-hitl", "agent_id": "sdk-subagent"},
        )
        yield await _success_result(options, text="approved")

    _patch_interactive_sdk_client(monkeypatch, fake_query, seen)

    settings = _settings(tmp_path, enable_hitl=True)
    service, store = _service(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), business_profile_resolver=main_profile_resolver(settings), user_input_service=service)

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

    events = asyncio.run(asyncio.wait_for(scenario(), timeout=2))

    assert [event["event"] for event in events] == [
        "session",
        "claude_user_input_required",
        "claude_user_input_resolved",
        "message",
        "result",
        "done",
    ]
    assert getattr(seen["options"], "permission_mode", None) == "default"
    assert getattr(seen["options"], "can_use_tool", None) is not None
    assert getattr(seen["options"], "hooks", None) is None
    assert list(getattr(seen["options"], "setting_sources", None) or []) == ["project"]
    assert getattr(seen["options"], "permission_prompt_tool_name", None) is None
    assert seen["prompt_items"][0]["message"]["content"] == "needs tool approval"
    assert seen["control_open_at_callback"] is True
    assert seen["control_closed"] is True
    assert seen["disconnected"] is True
    assert seen["permission_result"].__class__.__name__ == "PermissionResultAllow"
    record = store.list(run_id=str(events[0]["data"]["run_id"]))[0]
    assert record.status == "resolved"
    assert record.decision == "allow_once"


def test_stream_imported_security_agent_uses_native_hitl_without_id_gate_or_input_rewrite(tmp_path, monkeypatch):
    seen = {}

    async def fake_query(*, prompt, options, transport=None):
        seen["options"] = options
        await anext(prompt)
        seen["control_open_at_callback"] = not seen.get("control_closed", False)
        seen["create_result"] = await options.can_use_tool(
            SOC_CREATE_TOOL,
            {"playbookId": "model-draft"},
            {"tool_use_id": "create"},
        )
        seen["manual_result"] = await options.can_use_tool(
            SOC_MANUAL_TOOL,
            {"playbookId": "model-draft"},
            {"tool_use_id": "manual"},
        )
        if False:
            yield None

    _patch_interactive_sdk_client(monkeypatch, fake_query, seen)

    imported_agent_id = "security-operations-derived"
    settings = _settings(
        tmp_path,
        enable_hitl=True,
        agent_id=imported_agent_id,
        seed_agent_id=SECURITY_OPERATIONS_EXPERT_AGENT_ID,
        ask_rules=[SOC_CREATE_TOOL, SOC_MANUAL_TOOL],
    )
    service, store = _service(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), business_profile_resolver=main_profile_resolver(settings), user_input_service=service)
    profile = _profile(settings, imported_agent_id)

    async def scenario():
        stream = runtime.stream(ChatRequest(message="dispose"), profile=profile)
        session_event = await anext(stream)
        create_required = await anext(stream)
        service.submit_decision(
            str(create_required["data"]["request_id"]),
            decision=_decision_from_request(create_required["data"]),
            decided_by="tester",
        )
        create_resolved = await anext(stream)
        manual_required = await anext(stream)
        service.submit_decision(
            str(manual_required["data"]["request_id"]),
            decision=_decision_from_request(manual_required["data"], action="deny"),
            decided_by="tester",
        )
        rest = []
        async for event in stream:
            rest.append(event)
        return [session_event, create_required, create_resolved, manual_required, *rest]

    events = asyncio.run(scenario())

    assert [event["event"] for event in events] == [
        "session",
        "claude_user_input_required",
        "claude_user_input_resolved",
        "claude_user_input_required",
        "claude_user_input_resolved",
        "done",
    ]
    assert seen["create_result"].__class__.__name__ == "PermissionResultAllow"
    assert getattr(seen["create_result"], "updated_input", None) is None
    assert seen["manual_result"].__class__.__name__ == "PermissionResultDeny"
    assert seen["control_open_at_callback"] is True
    records = store.list(run_id=str(events[0]["data"]["run_id"]))
    assert len(records) == 2
    assert {record.tool_name for record in records} == {SOC_CREATE_TOOL, SOC_MANUAL_TOOL}
    assert {record.business_agent_id for record in records} == {imported_agent_id}
    assert {record.decision for record in records} == {"allow_once", "deny"}


def test_stream_without_native_ask_consumes_finite_prompt_without_permission_bridge(tmp_path, monkeypatch):
    seen = {}

    async def fake_query(*, prompt, options, transport=None):
        seen["options"] = options
        seen["prompt_items"] = [item async for item in prompt]
        yield await _success_result(options, text="ordinary")

    monkeypatch.setattr("app.runtime.claude_runtime_stream.read_requires_web_hitl", lambda _workspace: False)
    monkeypatch.setattr(claude_prompt_suggestions, "query_with_prompt_suggestions", fake_query)

    settings = _settings(tmp_path, enable_hitl=False)
    service, store = _service(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), business_profile_resolver=main_profile_resolver(settings), user_input_service=service)

    async def collect():
        return [event async for event in runtime.stream(ChatRequest(message="no hitl"))]

    events = asyncio.run(asyncio.wait_for(collect(), timeout=2))

    assert [event["event"] for event in events] == ["session", "message", "result", "done"]
    assert getattr(seen["options"], "can_use_tool", None) is None
    assert getattr(seen["options"], "hooks", None) is None
    assert getattr(seen["options"], "permission_mode", None) is None
    assert seen["prompt_items"][0]["message"]["content"] == "no hitl"
    assert store.list(limit=10) == []


def test_stream_fail_loud_when_hitl_required_but_disabled(tmp_path, monkeypatch):
    # project settings 含 ask 且 HITL 关闭：命中 ask 工具时 fail-loud deny，不依赖第二份 agent.yaml 标志。
    seen = {}

    async def fake_query(*, prompt, options, transport=None):
        seen["options"] = options
        seen["prompt_items"] = [item async for item in prompt]
        seen["control_open_at_callback"] = not seen.get("control_closed", False)
        seen["permission_result"] = await options.can_use_tool("mcp__soc-playbook-execution__submit", {}, {"tool_use_id": "t1"})
        if False:
            yield None

    _patch_interactive_sdk_client(monkeypatch, fake_query, seen)

    settings = _settings(tmp_path, enable_hitl=False)
    service, store = _service(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), business_profile_resolver=main_profile_resolver(settings), user_input_service=service)

    async def collect():
        return [event async for event in runtime.stream(ChatRequest(message="dispose"))]

    events = asyncio.run(asyncio.wait_for(collect(), timeout=5))
    names = [event["event"] for event in events]
    assert getattr(seen["options"], "can_use_tool", None) is not None  # fail-loud 回调已挂
    assert seen["permission_result"].__class__.__name__ == "PermissionResultDeny"
    assert seen["control_open_at_callback"] is True
    assert "ENABLE_CLAUDE_WEB_HITL" in seen["permission_result"].message
    assert "error" in names  # 命中 ask 工具产明确 error 事件（非静默）
    assert store.list(limit=10) == []  # 未创建 HITL 请求（HITL 关）


def test_stream_imported_security_agent_fails_closed_when_native_hitl_is_disabled(tmp_path, monkeypatch):
    seen = {}

    async def fake_query(*, prompt, options, transport=None):
        seen["options"] = options
        await anext(prompt)
        seen["control_open_at_callback"] = not seen.get("control_closed", False)
        seen["create_result"] = await options.can_use_tool(SOC_CREATE_TOOL, {}, {"tool_use_id": "create"})
        seen["manual_result"] = await options.can_use_tool(SOC_MANUAL_TOOL, {}, {"tool_use_id": "manual"})
        if False:
            yield None

    _patch_interactive_sdk_client(monkeypatch, fake_query, seen)

    imported_agent_id = "security-operations-derived"
    settings = _settings(
        tmp_path,
        enable_hitl=False,
        agent_id=imported_agent_id,
        seed_agent_id=SECURITY_OPERATIONS_EXPERT_AGENT_ID,
        ask_rules=[SOC_CREATE_TOOL, SOC_MANUAL_TOOL],
    )
    service, store = _service(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), business_profile_resolver=main_profile_resolver(settings), user_input_service=service)
    profile = _profile(settings, imported_agent_id)

    async def collect():
        return [event async for event in runtime.stream(ChatRequest(message="dispose"), profile=profile)]

    events = asyncio.run(asyncio.wait_for(collect(), timeout=5))
    names = [event["event"] for event in events]
    assert seen["create_result"].__class__.__name__ == "PermissionResultDeny"
    assert "ENABLE_CLAUDE_WEB_HITL" in seen["create_result"].message
    assert seen["manual_result"].__class__.__name__ == "PermissionResultDeny"
    assert "ENABLE_CLAUDE_WEB_HITL" in seen["manual_result"].message
    assert seen["control_open_at_callback"] is True
    assert "error" in names
    assert store.list(limit=10) == []


def test_run_fail_loud_when_hitl_required(tmp_path, monkeypatch):
    # 非流式 run() 不覆盖项目 permission mode；can_use_tool 仅为原生 ask 提供 fail-closed 交互桥。
    seen = {}

    async def fake_query(*, prompt, options, transport=None):
        seen["options"] = options
        seen["prompt_items"] = [item async for item in prompt]
        seen["control_open_at_callback"] = not seen.get("control_closed", False)
        seen["permission_result"] = await options.can_use_tool("mcp__soc-playbook-execution__submit", {}, {"tool_use_id": "t1"})
        if False:
            yield None

    _patch_interactive_sdk_client(monkeypatch, fake_query, seen)

    settings = _settings(tmp_path, enable_hitl=False)
    service, _store = _service(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), business_profile_resolver=main_profile_resolver(settings), user_input_service=service)

    asyncio.run(runtime.run(ChatRequest(message="dispose")))
    assert getattr(seen["options"], "permission_mode", None) == "default"
    assert getattr(seen["options"], "can_use_tool", None) is not None
    assert seen["permission_result"].__class__.__name__ == "PermissionResultDeny"
    assert seen["control_open_at_callback"] is True


def test_run_imported_security_agent_denies_native_ask_without_stream_hitl(tmp_path, monkeypatch):
    seen = {}

    async def fake_query(*, prompt, options, transport=None):
        seen["options"] = options
        seen["control_open_at_callback"] = not seen.get("control_closed", False)
        seen["create_result"] = await options.can_use_tool(SOC_CREATE_TOOL, {}, {"tool_use_id": "create"})
        seen["manual_result"] = await options.can_use_tool(SOC_MANUAL_TOOL, {}, {"tool_use_id": "manual"})
        if False:
            yield None

    _patch_interactive_sdk_client(monkeypatch, fake_query, seen)

    imported_agent_id = "security-operations-derived"
    settings = _settings(
        tmp_path,
        enable_hitl=False,
        agent_id=imported_agent_id,
        seed_agent_id=SECURITY_OPERATIONS_EXPERT_AGENT_ID,
        ask_rules=[SOC_CREATE_TOOL, SOC_MANUAL_TOOL],
    )
    service, _store = _service(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), business_profile_resolver=main_profile_resolver(settings), user_input_service=service)
    profile = _profile(settings, imported_agent_id)

    asyncio.run(runtime.run(ChatRequest(message="dispose"), profile=profile))
    assert getattr(seen["options"], "permission_mode", None) == "default"
    assert seen["create_result"].__class__.__name__ == "PermissionResultDeny"
    assert seen["manual_result"].__class__.__name__ == "PermissionResultDeny"
    assert seen["control_open_at_callback"] is True


@pytest.mark.parametrize("mode", ["stream", "run"])
def test_runtime_reads_native_ask_from_workspace_on_each_turn(mode, tmp_path, monkeypatch):
    seen = {}

    async def fake_query(*, prompt, options, transport=None):
        seen["prompt_items"] = [item async for item in prompt]
        seen["permission_result"] = await options.can_use_tool(
            "mcp__dynamic__execute",
            {},
            {"tool_use_id": "dynamic"},
        )
        if False:
            yield None

    _patch_interactive_sdk_client(monkeypatch, fake_query, seen)
    settings = _settings(tmp_path, enable_hitl=False)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), business_profile_resolver=main_profile_resolver(settings))
    settings_path = settings.main_workspace_dir / ".claude" / "settings.json"
    settings_data = json.loads(settings_path.read_text(encoding="utf-8"))
    settings_data["permissions"]["ask"].append("mcp__dynamic__execute")
    settings_path.write_text(json.dumps(settings_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if mode == "stream":

        async def collect():
            return [event async for event in runtime.stream(ChatRequest(message="dynamic ask"))]

        asyncio.run(asyncio.wait_for(collect(), timeout=2))
    else:
        asyncio.run(runtime.run(ChatRequest(message="dynamic ask")))

    assert seen["permission_result"].__class__.__name__ == "PermissionResultDeny"
    assert seen["prompt_items"][0]["message"]["content"] == "dynamic ask"


def test_stream_finishes_when_completion_step_raises(tmp_path, monkeypatch):
    async def fake_query(*, prompt, options, transport=None):
        await anext(prompt)
        from claude_agent_sdk import ResultMessage

        sdk_session_id = options.resume or options.session_id
        await options.session_store.append(
            {"project_key": options.session_store.binding.project_key, "session_id": sdk_session_id},
            [{"type": "user", "uuid": "completion-test-entry"}],
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id=sdk_session_id,
            result="done",
        )

    monkeypatch.setattr("app.runtime.claude_runtime_stream.read_requires_web_hitl", lambda _workspace: False)
    monkeypatch.setattr(claude_prompt_suggestions, "query_with_prompt_suggestions", fake_query)

    settings = _settings(tmp_path, enable_hitl=False)
    service, _store = _service(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), business_profile_resolver=main_profile_resolver(settings), user_input_service=service)

    def fail_complete(*args, **kwargs):
        raise RuntimeError("completion boom")

    monkeypatch.setattr(runtime, "_complete_runtime_request", fail_complete)

    async def collect():
        return [event async for event in runtime.stream(ChatRequest(message="completion fails"))]

    async def scenario():
        return await asyncio.wait_for(collect(), timeout=2)

    events = asyncio.run(scenario())

    assert [event["event"] for event in events] == ["session", "message", "error", "done"]
    assert "RuntimeError: completion boom" in events[2]["data"]["errors"]


def test_interactive_stream_client_cancel_closes_sdk_and_aborts_turn(tmp_path, monkeypatch):
    from claude_agent_sdk import AssistantMessage, TextBlock

    seen = {}
    query_cancelled = asyncio.Event()

    async def blocking_query(*, prompt, options, transport=None):
        seen["prompt_items"] = [item async for item in prompt]
        sdk_session_id = options.resume or options.session_id
        await options.session_store.append(
            {"project_key": options.session_store.binding.project_key, "session_id": sdk_session_id},
            [{"type": "assistant", "uuid": "interactive-cancel-entry"}],
        )
        try:
            yield AssistantMessage(content=[TextBlock(text="partial")], model="<synthetic>", session_id=sdk_session_id)
            await asyncio.Event().wait()
        finally:
            query_cancelled.set()

    _patch_interactive_sdk_client(monkeypatch, blocking_query, seen)
    settings = _settings(tmp_path, enable_hitl=False)
    session_store = LocalSessionStore(settings.session_dir)
    service, _store = _service(tmp_path)
    runtime = ClaudeRuntime(settings, session_store, business_profile_resolver=main_profile_resolver(settings), user_input_service=service)
    session_id = "interactive-client-cancel"

    async def scenario():
        source = runtime.stream(ChatRequest(message="cancel", session_id=session_id))
        assert (await anext(source))["event"] == "session"
        assert (await anext(source))["event"] == "message"
        await close_async_iterator(source)
        await asyncio.wait_for(query_cancelled.wait(), timeout=1)

    asyncio.run(asyncio.wait_for(scenario(), timeout=2))
    saved = session_store.get(session_id)

    assert saved is not None
    assert saved.turns == 0
    assert saved.sdk_session_id is None
    assert saved.active_run_id is None
    assert seen["control_closed"] is True
    assert seen["disconnected"] is True


def test_interactive_stream_lease_loss_cancels_sdk_and_aborts_turn(tmp_path, monkeypatch):
    seen = {}
    query_started = threading.Event()
    query_cancelled = asyncio.Event()

    async def blocking_query(*, prompt, options, transport=None):
        seen["prompt_items"] = [item async for item in prompt]
        query_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            query_cancelled.set()
        if False:  # pragma: no cover - keep this function an async generator
            yield None

    _patch_interactive_sdk_client(monkeypatch, blocking_query, seen)
    monkeypatch.setattr(session_turn_lease, "DEFAULT_SESSION_TURN_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(session_turn_lease, "DEFAULT_SESSION_TURN_LEASE_SECONDS", 1.0)
    settings = _settings(tmp_path, enable_hitl=False)
    session_store = LocalSessionStore(settings.session_dir)
    service, _store = _service(tmp_path)

    def lose_lease(session_id, *, run_id, run_generation=None, lease_seconds=None):
        if not query_started.wait(timeout=1):
            raise AssertionError("SDK query did not start before lease renewal")
        raise SessionConflictError(f"Session {session_id} lease lost for {run_id}")

    monkeypatch.setattr(session_store, "renew_turn", lose_lease)
    runtime = ClaudeRuntime(settings, session_store, business_profile_resolver=main_profile_resolver(settings), user_input_service=service)
    session_id = "interactive-lease-loss"

    async def scenario():
        source = runtime.stream(ChatRequest(message="lease", session_id=session_id))
        assert (await anext(source))["event"] == "session"
        with pytest.raises(SessionConflictError, match="lease lost"):
            await anext(source)
        assert query_cancelled.is_set()

    asyncio.run(asyncio.wait_for(scenario(), timeout=2))
    saved = session_store.get(session_id)

    assert saved is not None
    assert saved.turns == 0
    assert saved.sdk_session_id is None
    assert saved.active_run_id is None
    assert seen["control_closed"] is True
    assert seen["disconnected"] is True
