"""POST /v1/responses (stream=true) SSE 投影：response.* 标准通道 + agentgov.* 控制信封
（仅 control）、heartbeat 保活 comment、HITL confirmation 投影（保 decision_token / resolved 不带）。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from app.runtime import claude_prompt_suggestions
from app.runtime.async_iterators import close_async_iterator
from app.runtime.openai_responses_stream import iter_responses_sse
from app.runtime.protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from app.runtime.schemas import ChatRequest
from fastapi.testclient import TestClient

from app_test_utils import load_test_app as _load_app
from test_agent_workspace_packages import _import_new_agent


def _patch_sdk_query(monkeypatch, fake_query) -> None:
    """Install the same fake behind one-shot and bidirectional SDK drivers."""
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

            self.control_task = asyncio.create_task(consume_control_stream())
            await asyncio.sleep(0)
            assert not self.control_task.done()

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

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    monkeypatch.setattr(claude_prompt_suggestions, "query_with_prompt_suggestions", fake_query)
    monkeypatch.setattr(claude_prompt_suggestions, "PromptSuggestionClaudeClient", FakeClaudeSDKClient)


def _fake_sdk_query_success(entry_label: str = "sdk-race"):
    """真实 stream 全链用的 fake SDK query：yield AssistantMessage + ResultMessage（走持久化）。"""
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    async def fake_query(*, prompt, options, transport=None):
        async for _ in prompt:
            pass
        sdk_session_id = options.resume or options.session_id
        await options.session_store.append(
            {"project_key": options.session_store.binding.project_key, "session_id": sdk_session_id},
            [{"type": "user", "uuid": f"{entry_label}-entry"}],
        )
        yield AssistantMessage(content=[TextBlock(text="收到")], model="<synthetic>", session_id=sdk_session_id)
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=0, is_error=False, num_turns=1, session_id=sdk_session_id, result="收到")

    return fake_query


def _drive_stream(module, req: ChatRequest, on_event=None) -> list:
    events: list = []

    async def go():
        async for ev in module.runtime.stream(req):
            events.append(ev)
            if on_event is not None:
                on_event(ev)

    asyncio.run(go())
    return events


async def _aiter(frames):
    for frame in frames:
        yield frame


def _collect(frames, **kwargs) -> str:
    async def go() -> str:
        chunks = []
        async for chunk in iter_responses_sse(_aiter(frames), **kwargs):
            chunks.append(chunk)
        return "".join(chunks)

    return asyncio.run(go())


def _parse(sse_text: str):
    events = []
    for block in sse_text.split("\n\n"):
        block = block.strip("\n")
        if not block:
            continue
        name = data = None
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        if name is not None:
            events.append((name, data))
    return events


_SESSION = {
    "event": "session",
    "data": {"run_id": "run-9", "session_id": "sess-9", "sdk_session_id": "sdk-9", "agent_version_id": "ver-9", "agent_id": "soc-ops"},
}
_ASSISTANT = {"event": "message", "data": {"event": "AssistantMessage", "text": "日报正文", "raw": {}}}
_SUGGESTION = {
    "event": "prompt_suggestion",
    "data": {"suggestion": "  继续检查异常路径  ", "run_id": "hostile-run", "session_id": "hostile-session"},
}
_RESULT = {
    "event": "result",
    "data": {
        "run_id": "run-9",
        "session_id": "sess-9",
        "sdk_session_id": "sdk-9",
        "usage": {"input_tokens": 1, "output_tokens": 2},
        "stop_reason": "end_turn",
        "errors": [],
        "agent_activity": {},
    },
}
_DONE = {"event": "done", "data": "[DONE]"}


# ---------------------------------------------------------------- 控制通道映射


def test_control_stream_maps_core_events() -> None:
    text = _collect([_SESSION, _ASSISTANT, _RESULT, _DONE], model="m", effective_agent_id="soc-ops", control=True)
    events = _parse(text)
    names = [n for n, _ in events]
    assert names == [
        "response.created",
        "agentgov.session",
        "response.output_text.delta",
        "agentgov.result",
        "response.completed",
        "agentgov.done",
    ]
    by = dict(events)
    assert by["response.created"]["response"]["id"] == "resp_run-9"
    assert by["agentgov.session"]["payload"]["heartbeat_interval_s"] == 15
    assert by["agentgov.session"]["v"] == 1 and by["agentgov.session"]["run_id"] == "run-9"
    assert by["response.output_text.delta"]["delta"] == "日报正文"
    # completed 复用非流式投影：权威 output 在 output[]
    assert by["response.completed"]["response"]["output"][0]["content"][0]["text"] == "日报正文"


def test_strict_stream_emits_no_agentgov() -> None:
    text = _collect([_SESSION, _ASSISTANT, _RESULT, _SUGGESTION, _DONE], model="m", effective_agent_id="x", control=False)
    events = _parse(text)
    names = [n for n, _ in events]
    assert all(not n.startswith("agentgov.") for n in names)
    assert names == ["response.created", "response.output_text.delta", "response.completed"]
    # strict 的 completed response 不泄露 agentgov
    assert "agentgov" not in dict(events)["response.completed"]["response"]


def test_prompt_suggestion_control_uses_session_context_ids_and_precedes_done() -> None:
    events = _parse(_collect([_SESSION, _RESULT, _SUGGESTION, _DONE], model="m", effective_agent_id="x", control=True))
    names = [name for name, _ in events]
    suggestion = dict(events)["agentgov.prompt_suggestion"]

    assert names.index("agentgov.prompt_suggestion") > names.index("agentgov.result")
    assert names.index("agentgov.prompt_suggestion") < names.index("response.completed")
    assert names.index("agentgov.prompt_suggestion") < names.index("agentgov.done")
    assert suggestion["run_id"] == "run-9"
    # 附加式形状:新增 `suggestions` 完整候选列表,`suggestion` 保留且恒等 `suggestions[0]`
    # —— 对第三方承诺的 {suggestion, session_id} 字面仍成立,老客户端零改动。
    assert suggestion["payload"] == {
        "suggestion": "继续检查异常路径",
        "suggestions": ["继续检查异常路径"],
        "session_id": "sess-9",
    }


def test_heartbeat_becomes_sse_comment() -> None:
    text = _collect([_SESSION, {"event": "heartbeat", "data": {"run_id": "run-9"}}, _DONE], model="m", effective_agent_id="x", control=True)
    assert ": keepalive\n\n" in text
    assert "event: agentgov.heartbeat" not in text  # 心跳不进业务时间线


def test_done_without_result_emits_one_failed_terminal() -> None:
    events = _parse(
        _collect(
            [_SESSION, _ASSISTANT, _DONE, _DONE],
            model="m",
            effective_agent_id="soc-ops",
            control=True,
        )
    )
    names = [name for name, _ in events]

    assert names.count("response.failed") == 1
    assert names.count("response.completed") == 0
    assert names.count("agentgov.error") == 1
    assert names.count("agentgov.done") == 1
    assert dict(events)["response.failed"]["error"]["error_code"] == "STREAM_TERMINATED_WITHOUT_RESULT"


def test_frames_after_done_are_ignored() -> None:
    events = _parse(
        _collect(
            [_SESSION, _DONE, _ASSISTANT, _RESULT],
            model="m",
            effective_agent_id="soc-ops",
            control=True,
        )
    )
    names = [name for name, _ in events]

    assert names == ["response.created", "agentgov.session", "response.failed", "agentgov.error", "agentgov.done"]
    assert "response.output_text.delta" not in names
    assert "agentgov.result" not in names


def test_frames_after_failed_terminal_are_ignored_until_done() -> None:
    events = _parse(
        _collect(
            [_SESSION, {"event": "error", "data": {"errors": ["boom"]}}, _ASSISTANT, _DONE],
            model="m",
            effective_agent_id="soc-ops",
            control=True,
        )
    )
    names = [name for name, _ in events]

    assert names == ["response.created", "agentgov.session", "response.failed", "agentgov.error", "agentgov.done"]


def test_source_eof_without_done_or_result_still_emits_failed_terminal() -> None:
    events = _parse(_collect([_SESSION], model="m", effective_agent_id="soc-ops", control=False))

    assert [name for name, _ in events] == ["response.created", "response.failed"]


def test_source_exception_before_session_emits_one_standard_failed_terminal() -> None:
    async def failing_source():
        if False:
            yield {}
        raise RuntimeError("source exploded")

    async def go() -> str:
        chunks = []
        async for chunk in iter_responses_sse(
            failing_source(),
            model="m",
            effective_agent_id="soc-ops",
            control=True,
        ):
            chunks.append(chunk)
        return "".join(chunks)

    events = _parse(asyncio.run(go()))
    names = [name for name, _ in events]

    assert names == ["response.created", "agentgov.session", "response.failed", "agentgov.error", "agentgov.done"]
    assert names.count("response.failed") == 1
    assert dict(events)["response.failed"]["error"] == {
        "error_code": "STREAM_SOURCE_ERROR",
        "errors": ["RuntimeError: source exploded"],
    }


def test_projection_closes_upstream_when_client_stops_consuming() -> None:
    upstream_closed = asyncio.Event()

    async def blocking_source():
        try:
            yield _SESSION
            yield _ASSISTANT
            await asyncio.Event().wait()
        finally:
            upstream_closed.set()

    async def go() -> None:
        projected = iter_responses_sse(
            blocking_source(),
            model="m",
            effective_agent_id="soc-ops",
            control=True,
        )
        async for chunk in projected:
            if "event: response.output_text.delta" in chunk:
                break
        await projected.aclose()
        await asyncio.wait_for(upstream_closed.wait(), timeout=1)

    asyncio.run(go())


def test_projection_accepts_async_iterator_without_aclose() -> None:
    class SourceWithoutAsyncClose:
        def __init__(self) -> None:
            self._frames = iter([_SESSION, _RESULT, _DONE])

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._frames)
            except StopIteration:
                raise StopAsyncIteration from None

    async def go() -> str:
        chunks = []
        async for chunk in iter_responses_sse(
            SourceWithoutAsyncClose(),
            model="m",
            effective_agent_id="soc-ops",
            control=False,
        ):
            chunks.append(chunk)
        return "".join(chunks)

    assert [name for name, _ in _parse(asyncio.run(go()))][-1] == "response.completed"


def test_error_maps_to_failed() -> None:
    text = _collect([_SESSION, {"event": "error", "data": {"errors": ["Boom: x"]}}], model="m", effective_agent_id="x", control=True)
    by = dict(_parse(text))
    assert by["response.failed"]["error"]["errors"] == ["Boom: x"]
    assert by["agentgov.error"]["payload"]["errors"] == ["Boom: x"]


def test_result_errors_map_to_one_failed_terminal_and_control_error() -> None:
    failed_result = {**_RESULT, "data": {**_RESULT["data"], "errors": ["Claude Code API error (404): bad model"]}}
    events = _parse(
        _collect(
            [_SESSION, _ASSISTANT, failed_result, {"event": "error", "data": failed_result["data"]}, _DONE],
            model="m",
            effective_agent_id="soc-ops",
            control=True,
        )
    )
    names = [name for name, _ in events]
    assert names.count("response.failed") == 1
    assert names.count("agentgov.error") == 1
    assert "response.completed" not in names
    by = dict(events)
    assert by["response.failed"]["response"]["status"] == "failed"
    assert by["agentgov.result"]["payload"]["run_id"] == "run-9"
    assert by["agentgov.error"]["payload"]["errors"] == ["Claude Code API error (404): bad model"]


def test_tool_step_from_raw() -> None:
    tool_msg = {
        "event": "message",
        "data": {"event": "AssistantMessage", "text": "", "raw": {"content": [{"name": "Bash", "id": "tu-1", "input": {"command": "ls"}}]}},
    }
    text = _collect([_SESSION, tool_msg, _DONE], model="m", effective_agent_id="x", control=True)
    by = dict(_parse(text))
    step = by["agentgov.tool_step"]["payload"]
    assert step["kind"] == "tool_use" and step["tool_name"] == "Bash" and step["tool_use_id"] == "tu-1"


def test_delta_and_created_carry_openai_conformant_fields() -> None:
    by = dict(_parse(_collect([_SESSION, _ASSISTANT, _DONE], model="m", effective_agent_id="x", control=True)))
    delta = by["response.output_text.delta"]
    assert delta["item_id"] == "msg_run-9" and delta["output_index"] == 0 and delta["content_index"] == 0
    assert isinstance(delta["sequence_number"], int) and delta["type"] == "response.output_text.delta"
    created = by["response.created"]
    assert created["type"] == "response.created" and isinstance(created["sequence_number"], int)
    assert isinstance(created["response"]["created_at"], int)


def test_tool_step_from_raw_tool_result() -> None:
    tool_result = {"event": "message", "data": {"event": "UserMessage", "text": "", "raw": {"content": [{"tool_use_id": "tu-1", "content": "OK"}]}}}
    by = dict(_parse(_collect([_SESSION, tool_result, _DONE], model="m", effective_agent_id="x", control=True)))
    step = by["agentgov.tool_step"]["payload"]
    assert step["kind"] == "tool_result" and step["tool_use_id"] == "tu-1" and step["result"] == "OK"


def test_sdk_raw_envelope_only_when_debug_enabled() -> None:
    raw_msg = {"event": "message", "data": {"event": "SystemMessage", "text": "", "raw": {"foo": "bar"}}}
    frames = [_SESSION, raw_msg, _DONE]
    with_raw = dict(_parse(_collect(frames, model="m", effective_agent_id="x", control=True, sdk_raw=True)))
    assert with_raw["agentgov.sdk_raw"]["payload"]["raw"] == {"foo": "bar"}
    without = _collect(frames, model="m", effective_agent_id="x", control=True, sdk_raw=False)
    assert "event: agentgov.sdk_raw" not in without  # 默认关，不下发


def test_confirmation_projection_keeps_token_and_renames() -> None:
    required = {
        "event": "claude_user_input_required",
        "data": {
            "request_id": "cur-1",
            "decision_token": "tok-secret",
            "request_type": "tool_permission",
            "run_id": "run-9",
            "session_id": "sess-9",
            "business_agent_id": "soc-ops",
            "tool_name": "Bash",
            "input": {"command": "rm -rf /"},
            "risk": {"level": "high"},
        },
    }
    resolved = {
        "event": "claude_user_input_resolved",
        "data": {
            "request_id": "cur-1",
            "run_id": "run-9",
            "session_id": "sess-9",
            "business_agent_id": "soc-ops",
            "status": "resolved",
            "decision": "deny",
            "decided_by": "api_key_client",
        },
    }
    by = dict(_parse(_collect([_SESSION, required, resolved, _DONE], model="m", effective_agent_id="soc-ops", control=True)))
    req_payload = by["agentgov.confirmation.requested"]["payload"]
    assert req_payload["decision_token"] == "tok-secret"  # requested 保 token
    assert req_payload["agent_id"] == "soc-ops"  # business_agent_id -> agent_id
    assert req_payload["tool_input"] == {"command": "rm -rf /"}  # input -> tool_input
    assert req_payload["risk_reason"] == {"level": "high"}  # risk -> risk_reason
    assert req_payload["conversation_id"] == "conv_sess-9"
    res_payload = by["agentgov.confirmation.resolved"]["payload"]
    assert "decision_token" not in res_payload  # resolved 不带 token
    assert res_payload["decision"] == "deny"


# ---------------------------------------------------------------- 端点集成


def _fake_stream(frames):
    async def stream(req, *, profile=None, **kwargs):
        for frame in frames:
            yield frame

    return stream


def _fake_capturing_stream(captured: dict, frames):
    async def stream(req, *, profile=None, **kwargs):
        captured["req"] = req
        captured["profile"] = profile
        for frame in frames:
            yield frame

    return stream


def _register_biz(client: TestClient, agent_id: str = "soc-ops") -> None:
    assert _import_new_agent(client, agent_id=agent_id, name="客服").status_code == 200


def test_endpoint_stream_control(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(module.runtime, "stream", _fake_stream([_SESSION, _ASSISTANT, _RESULT, _DONE]))
    with TestClient(module.app) as client:
        _register_biz(client)
        resp = client.post("/v1/responses", json={"input": "hi", "stream": True, "agentgov": {"agent_id": "soc-ops"}})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        names = [n for n, _ in _parse(resp.text)]
        assert "response.created" in names and "response.output_text.delta" in names and "agentgov.session" in names


def test_endpoint_stream_control_maps_request_fields(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    captured: dict = {}
    monkeypatch.setattr(module.runtime, "stream", _fake_capturing_stream(captured, [_SESSION, _DONE]))
    with TestClient(module.app) as client:
        _register_biz(client)
        resp = client.post(
            "/v1/responses",
            json={
                "model": "claude-sonnet-5",
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "流式输入"}]}],
                "instructions": "只输出正文",
                "stream": True,
                "store": False,
                "metadata": {"source": "playground", "__agentgov_store__": True},
                "agentgov": {"agent_id": "soc-ops", "alert_id": "alert-1", "case_id": "case-1", "max_turns": 7},
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith("text/event-stream")

    req = captured["req"]
    assert req.message == "流式输入"
    assert req.model == "claude-sonnet-5"
    assert req.agent_id == "soc-ops"
    assert req.alert_id == "alert-1"
    assert req.case_id == "case-1"
    assert req.max_turns == 7
    assert req.system_append == "只输出正文"
    assert req.metadata == {"source": "playground", "__agentgov_store__": False}
    assert str(captured["profile"].workspace_dir).endswith("/business-agents/soc-ops/workspace")


def test_endpoint_stream_strict_uses_configured_agent_without_agentgov_events(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    captured: dict = {}
    monkeypatch.setattr(module.runtime, "stream", _fake_capturing_stream(captured, [_SESSION, _ASSISTANT, _RESULT, _DONE]))
    with TestClient(module.app) as client:
        _register_biz(client)
        client.put("/api/settings/openai-compat-agent", json={"agent_id": "soc-ops"})
        resp = client.post("/v1/responses", json={"input": "hi", "stream": True})
        assert resp.status_code == 200, resp.text

    assert str(captured["profile"].workspace_dir).endswith("/business-agents/soc-ops/workspace")
    names = [name for name, _ in _parse(resp.text)]
    assert "response.completed" in names
    assert all(not name.startswith("agentgov.") for name in names)


def test_endpoint_stream_projects_hitl_confirmation(monkeypatch, tmp_path: Path) -> None:
    module = _load_app(monkeypatch, tmp_path)
    required = {
        "event": "claude_user_input_required",
        "data": {
            "request_id": "cur-1",
            "decision_token": "tok-secret",
            "request_type": "tool_permission",
            "run_id": "run-9",
            "session_id": "sess-9",
            "business_agent_id": "soc-ops",
            "tool_name": "Bash",
            "input": {"command": "echo hi"},
            "risk": {"level": "medium"},
        },
    }
    resolved = {
        "event": "claude_user_input_resolved",
        "data": {
            "request_id": "cur-1",
            "run_id": "run-9",
            "session_id": "sess-9",
            "business_agent_id": "soc-ops",
            "status": "resolved",
            "decision": "allow_once",
            "decided_by": "tester",
        },
    }
    monkeypatch.setattr(module.runtime, "stream", _fake_stream([_SESSION, required, resolved, _DONE]))
    with TestClient(module.app) as client:
        _register_biz(client)
        resp = client.post("/v1/responses", json={"input": "hi", "stream": True, "agentgov": {"agent_id": "soc-ops"}})
        assert resp.status_code == 200, resp.text

    by = dict(_parse(resp.text))
    assert by["agentgov.confirmation.requested"]["payload"]["decision_token"] == "tok-secret"
    assert by["agentgov.confirmation.requested"]["payload"]["tool_input"] == {"command": "echo hi"}
    assert by["agentgov.confirmation.resolved"]["payload"]["decision"] == "allow_once"
    assert "decision_token" not in by["agentgov.confirmation.resolved"]["payload"]


def test_endpoint_stream_fails_closed_for_unmigratable_previous_response_session(monkeypatch, tmp_path: Path) -> None:
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    calls: list[str | None] = []

    async def fake_query(*, prompt, options, transport=None):
        calls.append(getattr(options, "resume", None))
        await anext(prompt)
        if len(calls) == 1:
            raise RuntimeError("No conversation found with session ID: stale-sdk")
        yield AssistantMessage(content=[TextBlock(text="responses stream after retry")], model="<synthetic>", session_id="new-sdk")
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="new-sdk",
            result="responses stream after retry",
        )

    _patch_sdk_query(monkeypatch, fake_query)

    module = _load_app(monkeypatch, tmp_path)
    module.feedback_store.record_run({"run_id": "prev-stale", "session_id": "sess-stale", "agent_id": DEFAULT_BUSINESS_AGENT_ID})
    session = module.session_store.get_or_create_owned("sess-stale", agent_id=DEFAULT_BUSINESS_AGENT_ID)
    session.sdk_session_id = "stale-sdk"
    module.session_store.save(session)

    with TestClient(module.app) as client:
        resp = client.post(
            "/v1/responses",
            json={
                "input": "continue",
                "stream": True,
                "previous_response_id": "resp_prev-stale",
                "agentgov": {"agent_id": DEFAULT_BUSINESS_AGENT_ID},
            },
        )
        assert resp.status_code == 200, resp.text

    names = [name for name, _ in _parse(resp.text)]
    assert calls == []
    assert "response.failed" in names
    assert "response.completed" not in names
    saved = module.session_store.get("sess-stale")
    assert saved is not None
    assert saved.sdk_session_id == "stale-sdk"
    assert saved.turns == 0


def test_stream_persists_session_and_run_before_response_completed(monkeypatch, tmp_path: Path) -> None:
    # race 回归：在 result 事件（-> response.completed）时刻，session（sdk_session_id+agent_id）与 run 必须已落库，
    # 使 /v1/conversations/items 与 /v1/responses/{id} retrieve 在完成信号时刻即可查（修复前此处未落库、会失败）。
    _patch_sdk_query(monkeypatch, _fake_sdk_query_success("sdk-race"))
    module = _load_app(monkeypatch, tmp_path)

    at_result: dict = {}
    run_id: dict = {}

    def check(ev):
        data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
        if data.get("run_id"):
            run_id["v"] = data["run_id"]
        if ev.get("event") == "result":
            s = module.session_store.get("sess-race")
            at_result["sdk_session_id"] = s.sdk_session_id if s else None
            at_result["event_sdk_session_id"] = data.get("sdk_session_id")
            at_result["agent_id"] = s.agent_id if s else None
            at_result["run_found"] = bool(run_id.get("v")) and module.feedback_store.find_run(run_id=run_id["v"]) is not None

    _drive_stream(module, ChatRequest(message="hi", session_id="sess-race"), on_event=check)

    assert at_result.get("sdk_session_id") == at_result.get("event_sdk_session_id")  # session 已落库
    assert at_result.get("sdk_session_id")
    assert at_result.get("agent_id")  # agent_id 非空（否则 items 会从空列表退化为 500）
    assert at_result.get("run_found") is True  # run 已记录（retrieve 完成即可查）


def test_stream_run_write_failure_rolls_back_session_completion(monkeypatch, tmp_path: Path) -> None:
    import app.runtime.session_turn_persistence as turn_persistence_module

    _patch_sdk_query(monkeypatch, _fake_sdk_query_success("sdk-rollback"))
    module = _load_app(monkeypatch, tmp_path)

    calls = 0

    def fail_run_write(db, record):
        nonlocal calls
        calls += 1
        raise RuntimeError("injected run write failure")

    monkeypatch.setattr(turn_persistence_module, "upsert_agent_run_record", fail_run_write)
    events = _drive_stream(module, ChatRequest(message="hi", session_id="sess-rollback"))
    session_event = next(event for event in events if event.get("event") == "session")
    run_id = session_event["data"]["run_id"]
    saved = module.session_store.get("sess-rollback")

    assert [event.get("event") for event in events][-2:] == ["error", "done"]
    assert saved is not None
    assert saved.turns == 0
    assert saved.sdk_session_id is None
    assert saved.active_run_id == run_id
    assert module.feedback_store.find_run(run_id=run_id) is None
    error_event = next(event for event in events if event.get("event") == "error")
    assert error_event["data"]["error_code"] == "RUNTIME_FINALIZATION_FAILED"
    assert error_event["data"]["recovery_status"] == "deferred_to_lease_expiry"
    assert calls == 4  # 三次 finalize + 一次原子 interrupted 恢复；持续失败时保留 lease 交给过期对账器。


@pytest.mark.parametrize("failure_point", ["before_commit", "after_commit"])
def test_stream_retries_transient_turn_finalization(
    monkeypatch,
    tmp_path: Path,
    failure_point: str,
) -> None:
    _patch_sdk_query(monkeypatch, _fake_sdk_query_success(f"sdk-retry-{failure_point}"))
    module = _load_app(monkeypatch, tmp_path)
    original = module.session_store.finalize_persisted_turn
    calls = 0

    def flaky_finalize(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            if failure_point == "after_commit":
                original(**kwargs)
            raise RuntimeError(f"transient {failure_point}")
        return original(**kwargs)

    monkeypatch.setattr(module.session_store, "finalize_persisted_turn", flaky_finalize)
    events = _drive_stream(
        module,
        ChatRequest(message="retry finalization", session_id=f"sess-retry-{failure_point}"),
    )

    session_event = next(event for event in events if event.get("event") == "session")
    run_id = session_event["data"]["run_id"]
    saved = module.session_store.get(f"sess-retry-{failure_point}")
    assert calls == 2
    assert [event.get("event") for event in events][-2:] == ["result", "done"]
    assert saved is not None and saved.turns == 1 and saved.active_run_id is None
    assert module.feedback_store.find_run(run_id=run_id) is not None


def test_stream_finalization_exhaustion_interrupts_and_allows_immediate_retry(monkeypatch, tmp_path: Path) -> None:
    from app.runtime.errors import SessionConflictError
    from app.runtime.runtime_db import SessionTurnIntentModel

    _patch_sdk_query(monkeypatch, _fake_sdk_query_success("sdk-finalize-exhausted"))
    module = _load_app(monkeypatch, tmp_path)
    original = module.session_store.finalize_persisted_turn
    calls = 0

    def fail_finalize(**kwargs):
        nonlocal calls
        calls += 1
        raise SessionConflictError("injected finalization version conflict")

    monkeypatch.setattr(module.session_store, "finalize_persisted_turn", fail_finalize)
    events = _drive_stream(module, ChatRequest(message="first", session_id="sess-finalize-exhausted"))
    session_event = next(event for event in events if event.get("event") == "session")
    run_id = session_event["data"]["run_id"]

    assert calls == 3
    assert not any(event.get("event") == "result" for event in events)
    error_event = next(event for event in events if event.get("event") == "error")
    assert error_event["data"]["error_code"] == "RUNTIME_FINALIZATION_FAILED"
    assert [event.get("event") for event in events][-2:] == ["error", "done"]
    saved = module.session_store.get("sess-finalize-exhausted")
    assert saved is not None and saved.active_run_id is None and saved.turns == 0
    with module.session_store.Session() as db:
        intent = db.get(SessionTurnIntentModel, run_id)
        assert intent is not None and intent.status == "interrupted"

    monkeypatch.setattr(module.session_store, "finalize_persisted_turn", original)
    retried = _drive_stream(module, ChatRequest(message="retry", session_id="sess-finalize-exhausted"))
    assert any(event.get("event") == "result" for event in retried)
    saved = module.session_store.get("sess-finalize-exhausted")
    assert saved is not None and saved.active_run_id is None and saved.turns == 1


def test_stream_persists_exactly_once(monkeypatch, tmp_path: Path) -> None:
    # 幂等：is_result 处落库 + finally 兜底，不得双落库。
    _patch_sdk_query(monkeypatch, _fake_sdk_query_success("sdk-once"))
    module = _load_app(monkeypatch, tmp_path)
    calls = {"n": 0}
    original = module.runtime._complete_runtime_request

    def counting(*a, **k):
        calls["n"] += 1
        return original(*a, **k)

    monkeypatch.setattr(module.runtime, "_complete_runtime_request", counting)
    _drive_stream(module, ChatRequest(message="hi", session_id="sess-once"))
    assert calls["n"] == 1  # 恰好落库一次


def test_stream_syncs_trace_before_done_allows_client_to_disconnect(monkeypatch, tmp_path: Path) -> None:
    _patch_sdk_query(monkeypatch, _fake_sdk_query_success("sdk-trace-before-done"))
    module = _load_app(monkeypatch, tmp_path)
    trace_upserts = []
    monkeypatch.setattr(module.runtime.langfuse, "current_trace_ref", lambda: ("trace-before-done", None))
    monkeypatch.setattr(
        module.runtime.langfuse,
        "upsert_trace",
        lambda trace_id, **kwargs: trace_upserts.append({"trace_id": trace_id, **kwargs}),
    )

    async def consume_until_done() -> None:
        source = module.runtime.stream(ChatRequest(message="hi", session_id="sess-trace-before-done"))
        try:
            async for event in source:
                if event.get("event") == "done":
                    assert trace_upserts
                    break
        finally:
            await close_async_iterator(source)

    asyncio.run(consume_until_done())

    assert trace_upserts[0]["trace_id"] == "trace-before-done"
    assert trace_upserts[0]["output"]["answer"] == "收到"


def test_stream_error_path_persists_once_in_finally(monkeypatch, tmp_path: Path) -> None:
    # error/无 ResultMessage 路径：finally 兜底落库一次，仍发 error+done。
    async def fake_query(*, prompt, options, transport=None):
        await anext(prompt)
        raise RuntimeError("boom before result")
        yield  # pragma: no cover

    _patch_sdk_query(monkeypatch, fake_query)
    module = _load_app(monkeypatch, tmp_path)
    calls = {"n": 0}
    original = module.runtime._abort_runtime_request

    def counting(*a, **k):
        calls["n"] += 1
        return original(*a, **k)

    monkeypatch.setattr(module.runtime, "_abort_runtime_request", counting)
    events = _drive_stream(module, ChatRequest(message="hi", session_id="sess-err"))
    names = [e.get("event") for e in events]
    assert calls["n"] == 1
    assert "error" in names and "done" in names
    saved = module.session_store.get("sess-err")
    assert saved is not None and saved.turns == 0 and saved.active_run_id is None


def test_endpoint_empty_sdk_stream_fails_and_retrieve_preserves_error(monkeypatch, tmp_path: Path) -> None:
    async def fake_query(*, prompt, options, transport=None):
        async for _ in prompt:
            pass
        if False:
            yield None

    _patch_sdk_query(monkeypatch, fake_query)
    module = _load_app(monkeypatch, tmp_path)

    with TestClient(module.app) as client:
        _register_biz(client)
        response = client.post(
            "/v1/responses",
            json={
                "input": "empty SDK stream",
                "stream": True,
                "conversation": "conv_missing-result-stream",
                "agentgov": {"agent_id": "soc-ops"},
            },
        )
        events = _parse(response.text)
        by = dict(events)
        run_id = by["agentgov.session"]["payload"]["run_id"]
        retrieved = client.get(f"/v1/responses/resp_{run_id}")

    names = [name for name, _ in events]
    assert response.status_code == 200
    assert names.count("response.failed") == 1
    assert "response.completed" not in names
    assert by["response.failed"]["error"]["error_code"] == "STREAM_TERMINATED_WITHOUT_RESULT"
    assert retrieved.status_code == 200
    assert retrieved.json()["status"] == "failed"
    assert retrieved.json()["agentgov"]["errors"] == ["SDK query ended without ResultMessage"]


@pytest.mark.parametrize("failure_point", ["before_commit", "after_commit"])
def test_stream_retries_transient_turn_abort(
    monkeypatch,
    tmp_path: Path,
    failure_point: str,
) -> None:
    async def fake_query(*, prompt, options, transport=None):
        async for _ in prompt:
            pass
        raise RuntimeError("query failed before result")
        yield  # pragma: no cover

    _patch_sdk_query(monkeypatch, fake_query)
    module = _load_app(monkeypatch, tmp_path)
    original = module.session_store.abort_persisted_turn
    calls = 0

    def flaky_abort(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            if failure_point == "after_commit":
                original(**kwargs)
            raise RuntimeError(f"transient {failure_point}")
        return original(**kwargs)

    monkeypatch.setattr(module.session_store, "abort_persisted_turn", flaky_abort)
    events = _drive_stream(
        module,
        ChatRequest(message="retry abort", session_id=f"sess-abort-{failure_point}"),
    )

    saved = module.session_store.get(f"sess-abort-{failure_point}")
    assert calls == 2
    assert [event.get("event") for event in events][-2:] == ["error", "done"]
    assert saved is not None and saved.turns == 0 and saved.active_run_id is None


def test_endpoint_sdk_result_error_is_failed_terminal_for_playground(monkeypatch, tmp_path: Path) -> None:
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    async def fake_query(*, prompt, options, transport=None):
        async for _ in prompt:
            pass
        sdk_session_id = options.resume or options.session_id
        await options.session_store.append(
            {"project_key": options.session_store.binding.project_key, "session_id": sdk_session_id},
            [{"type": "user", "uuid": "sdk-error-entry"}],
        )
        yield AssistantMessage(content=[TextBlock(text="bad model")], model="<synthetic>", session_id=sdk_session_id)
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=True,
            num_turns=1,
            session_id=sdk_session_id,
            result="bad model",
            api_error_status=404,
        )

    _patch_sdk_query(monkeypatch, fake_query)
    module = _load_app(monkeypatch, tmp_path)
    with TestClient(module.app) as client:
        _register_biz(client)
        response = client.post(
            "/v1/responses",
            json={"input": "hi", "stream": True, "conversation": "conv_sdk-error-api", "agentgov": {"agent_id": "soc-ops"}},
        )

    assert response.status_code == 200
    events = _parse(response.text)
    names = [name for name, _ in events]
    assert "response.completed" not in names
    assert names.count("response.failed") == 1
    assert names.count("agentgov.error") == 1
    by = dict(events)
    assert by["agentgov.error"]["payload"]["errors"] == ["Claude Code API error (404): bad model"]
    assert by["agentgov.result"]["payload"]["sdk_session_id"]
