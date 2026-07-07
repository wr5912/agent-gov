"""``POST /v1/responses`` (stream=true) 的 SSE 投影层。

把 ``runtime.stream`` 产出的 ``{event, data}`` 帧重映射成 Responses-style SSE：

- 标准 OpenAI 通道：``response.created`` / ``response.output_text.delta`` / ``response.completed`` /
  ``response.failed``（两模式都发，纯 OpenAI 客户端可解析）。
- AgentGov 控制通道：``agentgov.*`` 统一信封 ``{v, type, run_id, ts, seq, payload}``，**仅 control 模式下发**
  （strict 客户端零污染）。session / tool_step / confirmation / result / error / done。
- 保活：``heartbeat`` -> SSE comment 行（``: keepalive``），不进业务时间线。

不变量：``heartbeat_interval_s`` 随 ``agentgov.session`` 下发，客户端据此派生 idle（不硬编码 180）；
``decision_token`` 只在 ``agentgov.confirmation.requested`` 下发，resolved 不带。
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Optional

from app.runtime.json_types import JsonObject
from app.runtime.openai_responses_adapter import (
    conversation_id_from_session,
    response_from_chat_response,
    response_id_from_run,
)
from app.runtime.schemas import ChatResponse

# 与 claude_runtime_stream.py:258 的 15s 空闲保活一致；client_idle 必须 > 该值。
HEARTBEAT_INTERVAL_S = 15
_ENVELOPE_VERSION = 1


def _sse(event_name: str, data: JsonObject, *, event_id: Optional[int] = None) -> str:
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event_name}")
    lines.append(f"data: {json.dumps(data, ensure_ascii=False)}")
    return "\n".join(lines) + "\n\n"


def _str(value: object) -> Optional[str]:
    return value if isinstance(value, str) else None


def _tool_step_from_raw(raw: object) -> Optional[JsonObject]:
    """从 message.raw 的 content blocks 投影一个工具时间线步（best-effort，dev/观测层）。"""
    if not isinstance(raw, dict):
        return None
    content = raw.get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        if not isinstance(block, dict):
            continue
        if "name" in block and "input" in block:  # tool_use
            return {"kind": "tool_use", "tool_name": block.get("name"), "tool_use_id": block.get("id"), "input": block.get("input")}
        if "tool_use_id" in block:  # tool_result
            return {"kind": "tool_result", "tool_use_id": block.get("tool_use_id"), "result": block.get("content")}
    return None


def _project_confirmation_requested(data: JsonObject) -> JsonObject:
    """HITL 请求事件投影：保原 public_payload 全字段集（fidelity，含 decision_token/risk/status/context）
    并加对外重命名别名（agent_id/tool_input/risk_reason/conversation_id）。"""
    raw_input = data.get("input") if isinstance(data.get("input"), dict) else {}
    session = data.get("session_id") or data.get("api_session_id")
    return {
        **data,
        "agent_id": data.get("business_agent_id"),
        "tool_input": data.get("input"),
        "risk_reason": data.get("risk"),
        "conversation_id": conversation_id_from_session(_str(session)),
        "question": raw_input.get("question"),
        "options": raw_input.get("options"),
    }


def _project_confirmation_resolved(data: JsonObject) -> JsonObject:
    """HITL 结果事件投影：保原字段集（无 decision_token，resolved public_payload 本就不含）+ 别名。"""
    session = data.get("session_id") or data.get("api_session_id")
    return {
        **data,
        "agent_id": data.get("business_agent_id"),
        "conversation_id": conversation_id_from_session(_str(session)),
    }


def _completed_response(data: JsonObject, *, model: Optional[str], effective_agent_id: Optional[str], answer_parts: list[str], control: bool) -> JsonObject:
    """由 result 帧 + 累计文本增量重建 response 对象（复用非流式投影，单一来源）。"""
    chat = ChatResponse(
        run_id=str(data.get("run_id") or ""),
        session_id=str(data.get("session_id") or ""),
        sdk_session_id=_str(data.get("sdk_session_id")),
        agent_version_id=_str(data.get("agent_version_id")),
        answer="\n".join(answer_parts),
        agent_activity=data.get("agent_activity") if isinstance(data.get("agent_activity"), dict) else {},
        usage=data.get("usage") if isinstance(data.get("usage"), dict) else None,
        total_cost_usd=data.get("total_cost_usd") if isinstance(data.get("total_cost_usd"), (int, float)) else None,
        stop_reason=_str(data.get("stop_reason")),
        errors=list(data.get("errors")) if isinstance(data.get("errors"), list) else [],
    )
    response = response_from_chat_response(chat, model=model, agent_id=effective_agent_id, metadata={}).model_dump(exclude_none=True)
    if not control:
        response.pop("agentgov", None)  # strict：纯 OpenAI 响应对象，不泄露 agentgov
    return response


async def iter_responses_sse(
    source: AsyncIterator[JsonObject],
    *,
    model: Optional[str],
    effective_agent_id: Optional[str],
    control: bool,
    sdk_raw: bool = False,
) -> AsyncIterator[str]:
    """消费 ``runtime.stream`` 帧，产出 Responses-style SSE 字符串。"""
    seq = 0
    run_id: Optional[str] = None
    answer_parts: list[str] = []

    def envelope(type_: str, content: JsonObject) -> str:
        nonlocal seq
        seq += 1
        body = {"v": _ENVELOPE_VERSION, "type": type_, "run_id": run_id, "ts": time.time(), "seq": seq, "payload": content}
        return _sse(type_, body, event_id=seq)

    async for frame in source:
        event = frame.get("event")
        data = frame.get("data")
        data = data if isinstance(data, dict) else {}

        if event == "session":
            run_id = _str(data.get("run_id"))
            yield _sse(
                "response.created",
                {
                    "response": {
                        "id": response_id_from_run(run_id),
                        "object": "response",
                        "status": "in_progress",
                        "model": model,
                        "conversation": conversation_id_from_session(_str(data.get("session_id"))),
                    }
                },
            )
            if control:
                yield envelope("agentgov.session", {**data, "heartbeat_interval_s": HEARTBEAT_INTERVAL_S})
        elif event == "message":
            ev = str(data.get("event") or "")
            text = data.get("text") or ""
            if ev.startswith("AssistantMessage") and text:
                answer_parts.append(text)
                yield _sse("response.output_text.delta", {"delta": text})
            elif control:
                step = _tool_step_from_raw(data.get("raw"))
                if step:
                    yield envelope("agentgov.tool_step", step)
                if sdk_raw:
                    yield envelope("agentgov.sdk_raw", {"raw": data.get("raw")})
        elif event == "result":
            yield _sse(
                "response.completed",
                {"response": _completed_response(data, model=model, effective_agent_id=effective_agent_id, answer_parts=answer_parts, control=control)},
            )
            if control:
                yield envelope("agentgov.result", data)
        elif event == "error":
            yield _sse("response.failed", {"error": {"errors": data.get("errors")}})
            if control:
                yield envelope("agentgov.error", data)
        elif event == "heartbeat":
            yield ": keepalive\n\n"
        elif event == "claude_user_input_required" and control:
            yield envelope("agentgov.confirmation.requested", _project_confirmation_requested(data))
        elif event == "claude_user_input_resolved" and control:
            yield envelope("agentgov.confirmation.resolved", _project_confirmation_resolved(data))
        elif event == "done" and control:
            yield envelope("agentgov.done", {})
