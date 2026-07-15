"""``POST /v1/responses`` (stream=true) 的 SSE 投影层。

把 ``runtime.stream`` 产出的 ``{event, data}`` 帧重映射成 Responses-style SSE：

- 标准 OpenAI 通道：``response.created`` / ``response.output_text.delta`` / ``response.completed`` /
  ``response.failed``（两模式都发，纯 OpenAI 客户端可解析）。
- AgentGov 控制通道：``agentgov.*`` 统一信封 ``{v, type, run_id, ts, seq, payload}``，**仅 control 模式下发**
  （strict 客户端零污染）。session / tool_step / confirmation / prompt_suggestion / result / error / done。
- 保活：``heartbeat`` -> SSE comment 行（``: keepalive``），不进业务时间线。

不变量：``heartbeat_interval_s`` 随 ``agentgov.session`` 下发，客户端据此派生 idle（不硬编码 180）；
``decision_token`` 只在 ``agentgov.confirmation.requested`` 下发，resolved 不带。
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Optional

from app.runtime.async_iterators import close_async_iterator
from app.runtime.json_types import JsonObject
from app.runtime.openai_responses_adapter import (
    conversation_id_from_session,
    response_from_chat_response,
    response_id_from_run,
)
from app.runtime.response_disposition_control import TrustedResponseDispositionContext
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


def _created_response(run_id: Optional[str], model: Optional[str], session_id: Optional[str], created_at: int) -> JsonObject:
    """response.created 事件的 response 对象（OpenAI 形状：id/object/created_at/status/model/conversation）。"""
    return {
        "id": response_id_from_run(run_id),
        "object": "response",
        "created_at": created_at,
        "status": "in_progress",
        "model": model,
        "conversation": conversation_id_from_session(session_id),
    }


def _response_from_result(
    data: JsonObject,
    *,
    model: Optional[str],
    effective_agent_id: Optional[str],
    answer_parts: list[str],
    control: bool,
    created_at: Optional[int],
    response_disposition: TrustedResponseDispositionContext | None,
) -> JsonObject:
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
    response = response_from_chat_response(
        chat,
        model=model,
        agent_id=effective_agent_id,
        metadata={},
        created_at=created_at,
        response_disposition=response_disposition,
    ).model_dump(exclude_none=True)
    if not control:
        response.pop("agentgov", None)  # strict：纯 OpenAI 响应对象，不泄露 agentgov
    return response


@dataclass
class _ResponsesSseProjector:
    model: Optional[str]
    effective_agent_id: Optional[str]
    control: bool
    sdk_raw: bool
    response_disposition: TrustedResponseDispositionContext | None = None
    seq: int = 0
    run_id: Optional[str] = None
    session_id: Optional[str] = None
    item_id: Optional[str] = None
    created_at: Optional[int] = None
    answer_parts: list[str] = field(default_factory=list)
    terminal_status: Optional[str] = None
    pending_completed_response: JsonObject | None = None
    done_emitted: bool = False

    def _next(self) -> int:
        self.seq += 1
        return self.seq

    def _std(self, event_name: str, data: JsonObject) -> str:
        # 标准 OpenAI Responses 事件：补 type + 全局 sequence_number（规范必填），使纯 OpenAI SDK 可解析。
        return _sse(event_name, {"type": event_name, "sequence_number": self._next(), **data})

    def _envelope(self, type_: str, content: JsonObject) -> str:
        seq_no = self._next()
        body = {"v": _ENVELOPE_VERSION, "type": type_, "run_id": self.run_id, "ts": time.time(), "seq": seq_no, "payload": content}
        return _sse(type_, body, event_id=seq_no)

    def project(self, frame: JsonObject) -> list[str]:
        event = frame.get("event")
        data = frame.get("data")
        data = data if isinstance(data, dict) else {}
        if event == "prompt_suggestion":
            return self._project_prompt_suggestion(data)
        if event != "done" and (self.done_emitted or self.terminal_status is not None or self.pending_completed_response is not None):
            return []
        if event == "session":
            return self._project_session(data)
        if event == "message":
            return self._project_message(data)
        if event == "result":
            return self._project_result(data)
        if event == "error":
            return self._project_error(data)
        if event == "heartbeat":
            return [": keepalive\n\n"]
        if event == "claude_user_input_required" and self.control:
            return [self._envelope("agentgov.confirmation.requested", _project_confirmation_requested(data))]
        if event == "claude_user_input_resolved" and self.control:
            return [self._envelope("agentgov.confirmation.resolved", _project_confirmation_resolved(data))]
        if event == "done":
            return self._project_done()
        return []

    def _project_session(self, data: JsonObject) -> list[str]:
        self.run_id = _str(data.get("run_id"))
        self.session_id = _str(data.get("session_id"))
        self.item_id = f"msg_{self.run_id}" if self.run_id else None
        self.created_at = int(time.time())
        chunks = [
            self._std(
                "response.created",
                {"response": _created_response(self.run_id, self.model, self.session_id, self.created_at)},
            )
        ]
        if self.control:
            chunks.append(self._envelope("agentgov.session", {**data, "heartbeat_interval_s": HEARTBEAT_INTERVAL_S}))
        return chunks

    def _project_prompt_suggestion(self, data: JsonObject) -> list[str]:
        if not self.control or self.done_emitted or self.terminal_status is not None:
            return []
        suggestion = _str(data.get("suggestion"))
        if not suggestion or not suggestion.strip():
            return []
        return [
            self._envelope(
                "agentgov.prompt_suggestion",
                {"suggestion": suggestion.strip(), "session_id": self.session_id},
            )
        ]

    def _project_message(self, data: JsonObject) -> list[str]:
        event_name = str(data.get("event") or "")
        text = data.get("text") or ""
        if event_name.startswith("AssistantMessage") and text:
            self.answer_parts.append(text)
            return [
                self._std(
                    "response.output_text.delta",
                    {"item_id": self.item_id, "output_index": 0, "content_index": 0, "delta": text},
                )
            ]
        if not self.control:
            return []
        chunks: list[str] = []
        step = _tool_step_from_raw(data.get("raw"))
        if step:
            chunks.append(self._envelope("agentgov.tool_step", step))
        if self.sdk_raw:
            chunks.append(self._envelope("agentgov.sdk_raw", {"raw": data.get("raw")}))
        return chunks

    def _project_result(self, data: JsonObject) -> list[str]:
        response = _response_from_result(
            data,
            model=self.model,
            effective_agent_id=self.effective_agent_id,
            answer_parts=self.answer_parts,
            control=self.control,
            created_at=self.created_at,
            response_disposition=self.response_disposition,
        )
        raw_errors = data.get("errors")
        errors = [str(error) for error in raw_errors] if isinstance(raw_errors, list) else []
        failed_now = bool(errors) and self.terminal_status is None
        chunks: list[str] = []
        if failed_now:
            self.terminal_status = "failed"
            self.pending_completed_response = None
            chunks.append(self._std("response.failed", {"response": response, "error": {"errors": errors}}))
        elif not errors and self.terminal_status is None:
            self.pending_completed_response = response
        if self.control:
            chunks.append(self._envelope("agentgov.result", data))
            if failed_now:
                chunks.append(self._envelope("agentgov.error", {**data, "errors": errors}))
        return chunks

    def _project_error(self, data: JsonObject) -> list[str]:
        if self.terminal_status is not None:
            return []
        self.terminal_status = "failed"
        self.pending_completed_response = None
        chunks = [self._std("response.failed", {"error": data})]
        if self.control:
            chunks.append(self._envelope("agentgov.error", data))
        return chunks

    def _project_done(self) -> list[str]:
        if self.done_emitted:
            return []
        self.done_emitted = True
        chunks: list[str] = []
        if self.terminal_status is None and self.pending_completed_response is not None:
            self.terminal_status = "completed"
            chunks.append(self._std("response.completed", {"response": self.pending_completed_response}))
        elif self.terminal_status is None:
            self.terminal_status = "failed"
            detail = "Agent stream ended without a ResultMessage"
            error = {"error_code": "STREAM_TERMINATED_WITHOUT_RESULT", "errors": [detail]}
            chunks.append(self._std("response.failed", {"error": error}))
            if self.control:
                chunks.append(self._envelope("agentgov.error", error))
        if self.control:
            chunks.append(self._envelope("agentgov.done", {}))
        return chunks


async def iter_responses_sse(
    source: AsyncIterator[JsonObject],
    *,
    model: Optional[str],
    effective_agent_id: Optional[str],
    control: bool,
    sdk_raw: bool = False,
    response_disposition: TrustedResponseDispositionContext | None = None,
) -> AsyncIterator[str]:
    """消费 ``runtime.stream`` 帧，产出 Responses-style SSE 字符串。"""
    projector = _ResponsesSseProjector(
        model=model,
        effective_agent_id=effective_agent_id,
        control=control,
        sdk_raw=sdk_raw,
        response_disposition=response_disposition,
    )
    try:
        try:
            async for frame in source:
                for chunk in projector.project(frame):
                    yield chunk
        except Exception as exc:
            if projector.created_at is None:
                for chunk in projector._project_session({}):
                    yield chunk
            detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            for chunk in projector._project_error({"error_code": "STREAM_SOURCE_ERROR", "errors": [detail]}):
                yield chunk
    finally:
        await close_async_iterator(source)
    for chunk in projector._project_done():
        yield chunk
