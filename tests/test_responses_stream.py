"""POST /v1/responses (stream=true) SSE 投影：response.* 标准通道 + agentgov.* 控制信封
（仅 control）、heartbeat 保活 comment、HITL confirmation 投影（保 decision_token / resolved 不带）。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.runtime.openai_responses_stream import iter_responses_sse
from fastapi.testclient import TestClient

from test_api_execution_optimizer import _load_app


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
        "response.completed",
        "agentgov.result",
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
    text = _collect([_SESSION, _ASSISTANT, _RESULT, _DONE], model="m", effective_agent_id="x", control=False)
    events = _parse(text)
    names = [n for n, _ in events]
    assert all(not n.startswith("agentgov.") for n in names)
    assert names == ["response.created", "response.output_text.delta", "response.completed"]
    # strict 的 completed response 不泄露 agentgov
    assert "agentgov" not in dict(events)["response.completed"]["response"]


def test_heartbeat_becomes_sse_comment() -> None:
    text = _collect([_SESSION, {"event": "heartbeat", "data": {"run_id": "run-9"}}, _DONE], model="m", effective_agent_id="x", control=True)
    assert ": keepalive\n\n" in text
    assert "event: agentgov.heartbeat" not in text  # 心跳不进业务时间线


def test_error_maps_to_failed() -> None:
    text = _collect([_SESSION, {"event": "error", "data": {"errors": ["Boom: x"]}}], model="m", effective_agent_id="x", control=True)
    by = dict(_parse(text))
    assert by["response.failed"]["error"]["errors"] == ["Boom: x"]
    assert by["agentgov.error"]["payload"]["errors"] == ["Boom: x"]


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


def _register_biz(client: TestClient, agent_id: str = "soc-ops") -> None:
    assert client.post("/api/agent-registry", json={"name": "客服", "agent_id": agent_id}).status_code == 201


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
