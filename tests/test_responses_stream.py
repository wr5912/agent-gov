"""POST /v1/responses (stream=true) SSE 投影：response.* 标准通道 + agentgov.* 控制信封
（仅 control）、heartbeat 保活 comment、HITL confirmation 投影（保 decision_token / resolved 不带）。"""

from __future__ import annotations

import asyncio

from app.runtime.openai_responses_stream import iter_responses_sse

from responses_stream_test_support import (
    ASSISTANT_FRAME as _ASSISTANT,
)
from responses_stream_test_support import (
    DONE_FRAME as _DONE,
)
from responses_stream_test_support import (
    RESULT_FRAME as _RESULT,
)
from responses_stream_test_support import (
    SESSION_FRAME as _SESSION,
)
from responses_stream_test_support import (
    as_managed as _as_managed,
)
from responses_stream_test_support import (
    parse_sse as _parse,
)


async def _aiter(frames):
    for frame in frames:
        yield _as_managed(frame)


def _collect(frames, **kwargs) -> str:
    async def go() -> str:
        chunks = []
        async for chunk in iter_responses_sse(_aiter(frames), **kwargs):
            chunks.append(chunk)
        return "".join(chunks)

    return asyncio.run(go())


_SUGGESTION = {
    "event": "prompt_suggestion",
    "data": {"suggestion": "  继续检查异常路径  ", "run_id": "hostile-run", "session_id": "hostile-session"},
}
_CANCELLED = {
    "event": "cancelled",
    "data": {"run_id": "run-9", "session_id": "sess-9", "turn_status": "cancelled"},
}
_NON_EXECUTABLE_HIGH_RISK_TOOL_INPUT = {
    "test_fixture": {
        "kind": "non_executable_high_risk_input",
        "risk_category": "restricted_operation",
        "executable": False,
    }
}


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
        "agentgov.done",
        "response.completed",
    ]
    by = dict(events)
    assert by["response.created"]["response"]["id"] == "resp_run-9"
    assert by["agentgov.session"]["payload"]["heartbeat_interval_s"] == 15
    assert by["agentgov.session"]["v"] == 1 and by["agentgov.session"]["run_id"] == "run-9"
    assert by["agentgov.session"]["payload"]["langfuse_trace_id"] == "trace-9"
    assert by["agentgov.session"]["payload"]["langfuse_trace_url"].endswith("/project/agent-gov/traces/trace-9")
    assert by["response.output_text.delta"]["delta"] == "日报正文"
    # completed 复用非流式投影：权威 output 在 output[]
    assert by["response.completed"]["response"]["output"][0]["content"][0]["text"] == "日报正文"


def test_cancelled_stream_preserves_partial_output_and_emits_incomplete_terminal() -> None:
    events = _parse(
        _collect(
            [_SESSION, _ASSISTANT, _CANCELLED, _DONE],
            model="m",
            effective_agent_id="soc-ops",
            control=True,
        )
    )
    names = [name for name, _ in events]

    assert names == [
        "response.created",
        "agentgov.session",
        "response.output_text.delta",
        "agentgov.cancelled",
        "agentgov.done",
        "response.incomplete",
    ]
    incomplete = dict(events)["response.incomplete"]["response"]
    assert incomplete["status"] == "incomplete"
    assert incomplete["output"][0]["content"][0]["text"] == "日报正文"
    assert "response.failed" not in names


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
    assert dict(events)["agentgov.session"]["payload"]["langfuse_trace_id"] == "trace-9"


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

    assert names == ["response.created", "agentgov.session", "agentgov.error", "agentgov.done", "response.failed"]
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

    assert names == ["response.created", "agentgov.session", "agentgov.error", "agentgov.done", "response.failed"]


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

    assert names == ["response.created", "agentgov.session", "agentgov.error", "agentgov.done", "response.failed"]
    assert names.count("response.failed") == 1
    assert dict(events)["response.failed"]["error"] == {
        "error_code": "STREAM_SOURCE_ERROR",
        "errors": ["RuntimeError: source exploded"],
    }


def test_projection_closes_upstream_when_client_stops_consuming() -> None:
    upstream_closed = asyncio.Event()

    async def blocking_source():
        try:
            yield _as_managed(_SESSION)
            yield _as_managed(_ASSISTANT)
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
                return _as_managed(next(self._frames))
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
            "input": _NON_EXECUTABLE_HIGH_RISK_TOOL_INPUT,
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
    assert req_payload["tool_input"] == _NON_EXECUTABLE_HIGH_RISK_TOOL_INPUT  # input -> tool_input
    assert req_payload["risk_reason"] == {"level": "high"}  # risk -> risk_reason
    assert req_payload["conversation_id"] == "conv_sess-9"
    res_payload = by["agentgov.confirmation.resolved"]["payload"]
    assert "decision_token" not in res_payload  # resolved 不带 token
    assert res_payload["decision"] == "deny"
