import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest
from app.runtime import claude_prompt_suggestions, session_turn_lease
from app.runtime.agent_paths import business_agent_layout
from app.runtime.agent_profiles import build_business_agent_profile
from app.runtime.api_auth import ApiPrincipal
from app.runtime.async_iterators import close_async_iterator
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.claude_user_input_schemas import ClaudeUserInputDecisionRequest
from app.runtime.claude_user_input_service import ClaudeUserInputService
from app.runtime.errors import SessionConflictError
from app.runtime.response_disposition_control import (
    SECURITY_OPERATIONS_EXPERT_AGENT_ID,
    SOC_CREATE_TOOL,
    SOC_MANUAL_TOOL,
    TrustedResponseDispositionContext,
)
from app.runtime.runtime_db import make_session_factory
from app.runtime.schemas import ChatRequest, RuntimeChatRequest
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import AppSettings
from app.runtime.stores.claude_user_input_store import ClaudeUserInputStore
from app.runtime.stores.response_disposition_claim_store import ResponseDispositionClaimStore
from scripts.runtime_template_renderer import build_render_context, render_template_file

SOC_EXECUTE_TOOL = "mcp__sec-ops__soc_api__execute"
REPO_ROOT = Path(__file__).resolve().parents[1]
SEED_AGENT_ROOT = REPO_ROOT / "docker" / "runtime-volume-seeds" / "data" / "business-agents"


def _settings(
    tmp_path,
    *,
    enable_hitl: bool,
    agent_id: str = "main-agent",
    ask_rules: list[str] | None = None,
) -> AppSettings:
    workspace = tmp_path / "volume" / "data" / "business-agents" / agent_id / "workspace"
    data = tmp_path / "volume" / "data"
    claude_root = tmp_path / "volume" / "data" / "business-agents" / agent_id / "claude-root"
    claude_home = claude_root / ".claude"
    seed_workspace = SEED_AGENT_ROOT / agent_id / "workspace"
    shutil.copytree(seed_workspace, workspace)
    context = build_render_context(mode="local-debug", env=os.environ, runtime_root=tmp_path / "volume")
    mcp_path = workspace / ".mcp.json"
    mcp_path.write_text(
        render_template_file(mcp_path.read_text(encoding="utf-8"), rel_path=Path(".mcp.json"), context=context),
        encoding="utf-8",
    )
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


def _protected_service(
    tmp_path,
) -> tuple[ClaudeUserInputService, ClaudeUserInputStore, ResponseDispositionClaimStore]:
    factory = make_session_factory(tmp_path / "runtime.sqlite3")
    store = ClaudeUserInputStore(factory)
    claim_store = ResponseDispositionClaimStore(factory)
    return (
        ClaudeUserInputService(store, timeout_seconds=5, response_disposition_claim_store=claim_store),
        store,
        claim_store,
    )


def _decision_from_request(
    request: dict[str, object],
    *,
    action: str = "allow_once",
    updated_input: dict[str, object] | None = None,
) -> ClaudeUserInputDecisionRequest:
    payload: dict[str, object] = {"action": action, "decision_token": request["decision_token"]}
    if updated_input is not None:
        payload["updated_input"] = updated_input
    return ClaudeUserInputDecisionRequest.model_validate(payload)


def _approved_context() -> TrustedResponseDispositionContext:
    return TrustedResponseDispositionContext(
        phase="approved_execution",
        case_id="case-1",
        approval_request_id="approval-1",
        playbook_digest="a" * 64,
        execution_run_id="execution-1",
    )


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


def test_stream_security_operations_expert_requires_ro_for_approved_create_and_manual(tmp_path, monkeypatch):
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
        seen["write_result"] = await options.can_use_tool("Write", {"file_path": "./notes.md"}, {"tool_use_id": "write"})
        seen["question_result"] = await options.can_use_tool("AskUserQuestion", {"question": "confirm?"}, {"tool_use_id": "ask"})
        seen["execute_result"] = await options.can_use_tool(
            SOC_EXECUTE_TOOL,
            {"actionKey": "isolate_host"},
            {"tool_use_id": "execute"},
        )
        if False:
            yield None

    _patch_interactive_sdk_client(monkeypatch, fake_query, seen)

    settings = _settings(
        tmp_path,
        enable_hitl=True,
        agent_id=SECURITY_OPERATIONS_EXPERT_AGENT_ID,
        ask_rules=[SOC_CREATE_TOOL, SOC_MANUAL_TOOL],
    )
    service, store, claim_store = _protected_service(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), user_input_service=service)
    profile = _profile(settings, SECURITY_OPERATIONS_EXPERT_AGENT_ID)
    disposition = _approved_context()
    claim_store.claim(disposition)

    async def scenario():
        stream = runtime.stream(RuntimeChatRequest(message="dispose", response_disposition=disposition), profile=profile)
        session_event = await anext(stream)
        create_required = await anext(stream)
        service.submit_decision(
            str(create_required["data"]["request_id"]),
            decision=_decision_from_request(
                create_required["data"],
                updated_input={"playbookId": "approved-pb-1"},
            ),
            decided_by="tester",
            principal=ApiPrincipal.RESPONSE_ORCHESTRATOR,
        )
        create_resolved = await anext(stream)
        manual_required = await anext(stream)
        service.submit_decision(
            str(manual_required["data"]["request_id"]),
            decision=_decision_from_request(
                manual_required["data"],
                updated_input={"playbookId": "approved-pb-1", "mode": "manual"},
            ),
            decided_by="tester",
            principal=ApiPrincipal.RESPONSE_ORCHESTRATOR,
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
    assert seen["create_result"].updated_input == {"playbookId": "approved-pb-1"}
    assert seen["manual_result"].__class__.__name__ == "PermissionResultAllow"
    assert seen["manual_result"].updated_input == {"playbookId": "approved-pb-1", "mode": "manual"}
    assert seen["write_result"].__class__.__name__ == "PermissionResultDeny"
    assert seen["question_result"].__class__.__name__ == "PermissionResultDeny"
    assert seen["execute_result"].__class__.__name__ == "PermissionResultDeny"
    assert seen["control_open_at_callback"] is True
    records = store.list(run_id=str(events[0]["data"]["run_id"]))
    assert len(records) == 2
    assert {record.tool_name for record in records} == {SOC_CREATE_TOOL, SOC_MANUAL_TOOL}
    claim = claim_store.get("approval-1")
    assert claim is not None
    assert claim.create_authorized is True
    assert claim.manual_authorized is True


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
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), user_input_service=service)

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
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), user_input_service=service)

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


def test_stream_security_operations_expert_disabled_hitl_denies_approved_tools_and_execute(tmp_path, monkeypatch):
    seen = {}

    async def fake_query(*, prompt, options, transport=None):
        seen["options"] = options
        await anext(prompt)
        seen["control_open_at_callback"] = not seen.get("control_closed", False)
        seen["create_result"] = await options.can_use_tool(SOC_CREATE_TOOL, {}, {"tool_use_id": "create"})
        seen["manual_result"] = await options.can_use_tool(SOC_MANUAL_TOOL, {}, {"tool_use_id": "manual"})
        seen["execute_result"] = await options.can_use_tool(SOC_EXECUTE_TOOL, {}, {"tool_use_id": "execute"})
        if False:
            yield None

    _patch_interactive_sdk_client(monkeypatch, fake_query, seen)

    settings = _settings(
        tmp_path,
        enable_hitl=False,
        agent_id=SECURITY_OPERATIONS_EXPERT_AGENT_ID,
        ask_rules=[SOC_CREATE_TOOL, SOC_MANUAL_TOOL],
    )
    service, store = _service(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), user_input_service=service)
    profile = _profile(settings, SECURITY_OPERATIONS_EXPERT_AGENT_ID)
    disposition = _approved_context()

    async def collect():
        request = RuntimeChatRequest(message="dispose", response_disposition=disposition)
        return [event async for event in runtime.stream(request, profile=profile)]

    events = asyncio.run(asyncio.wait_for(collect(), timeout=5))
    names = [event["event"] for event in events]
    assert seen["create_result"].__class__.__name__ == "PermissionResultDeny"
    assert "ENABLE_CLAUDE_WEB_HITL" in seen["create_result"].message
    assert seen["manual_result"].__class__.__name__ == "PermissionResultDeny"
    assert "ENABLE_CLAUDE_WEB_HITL" in seen["manual_result"].message
    assert seen["execute_result"].__class__.__name__ == "PermissionResultDeny"
    assert "未授权" in seen["execute_result"].message
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
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), user_input_service=service)

    asyncio.run(runtime.run(ChatRequest(message="dispose")))
    assert getattr(seen["options"], "permission_mode", None) == "default"
    assert getattr(seen["options"], "can_use_tool", None) is not None
    assert seen["permission_result"].__class__.__name__ == "PermissionResultDeny"
    assert seen["control_open_at_callback"] is True


def test_run_security_operations_expert_denies_all_permission_requests_without_stream_hitl(tmp_path, monkeypatch):
    seen = {}

    async def fake_query(*, prompt, options, transport=None):
        seen["options"] = options
        seen["control_open_at_callback"] = not seen.get("control_closed", False)
        seen["create_result"] = await options.can_use_tool(SOC_CREATE_TOOL, {}, {"tool_use_id": "create"})
        seen["manual_result"] = await options.can_use_tool(SOC_MANUAL_TOOL, {}, {"tool_use_id": "manual"})
        seen["write_result"] = await options.can_use_tool("Write", {"file_path": "./notes.md"}, {"tool_use_id": "write"})
        seen["question_result"] = await options.can_use_tool("AskUserQuestion", {"question": "confirm?"}, {"tool_use_id": "ask"})
        seen["execute_result"] = await options.can_use_tool(SOC_EXECUTE_TOOL, {}, {"tool_use_id": "execute"})
        if False:
            yield None

    _patch_interactive_sdk_client(monkeypatch, fake_query, seen)

    settings = _settings(
        tmp_path,
        enable_hitl=False,
        agent_id=SECURITY_OPERATIONS_EXPERT_AGENT_ID,
        ask_rules=[SOC_CREATE_TOOL, SOC_MANUAL_TOOL],
    )
    service, _store = _service(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), user_input_service=service)
    profile = _profile(settings, SECURITY_OPERATIONS_EXPERT_AGENT_ID)

    request = RuntimeChatRequest(message="dispose", response_disposition=_approved_context())
    asyncio.run(runtime.run(request, profile=profile))
    assert getattr(seen["options"], "permission_mode", None) == "default"
    assert seen["create_result"].__class__.__name__ == "PermissionResultDeny"
    assert seen["manual_result"].__class__.__name__ == "PermissionResultDeny"
    assert seen["write_result"].__class__.__name__ == "PermissionResultDeny"
    assert seen["question_result"].__class__.__name__ == "PermissionResultDeny"
    assert seen["execute_result"].__class__.__name__ == "PermissionResultDeny"
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
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))
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
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), user_input_service=service)

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
    runtime = ClaudeRuntime(settings, session_store, user_input_service=service)
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
    query_cancelled = asyncio.Event()

    async def blocking_query(*, prompt, options, transport=None):
        seen["prompt_items"] = [item async for item in prompt]
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
        raise SessionConflictError(f"Session {session_id} lease lost for {run_id}")

    monkeypatch.setattr(session_store, "renew_turn", lose_lease)
    runtime = ClaudeRuntime(settings, session_store, user_input_service=service)
    session_id = "interactive-lease-loss"

    async def scenario():
        source = runtime.stream(ChatRequest(message="lease", session_id=session_id))
        assert (await anext(source))["event"] == "session"
        with pytest.raises(SessionConflictError, match="lease lost"):
            await anext(source)
        await asyncio.wait_for(query_cancelled.wait(), timeout=1)

    asyncio.run(asyncio.wait_for(scenario(), timeout=2))
    saved = session_store.get(session_id)

    assert saved is not None
    assert saved.turns == 0
    assert saved.sdk_session_id is None
    assert saved.active_run_id is None
    assert seen["control_closed"] is True
    assert seen["disconnected"] is True
