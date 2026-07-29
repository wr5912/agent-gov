"""Independent Claude SDK -> OpenAI Responses streaming projection.

The projector consumes the typed managed SDK source directly. It does not consume
the Chat endpoint's frames, so Chat can be removed without changing this contract.
Claude tools are executed by the server-side agent loop; their observations use
``agentgov.tool_call.*`` rather than OpenAI ``function_call`` items, which would
incorrectly ask the client to execute them.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Optional

from app.runtime.agent_trace import AgentTraceEvent, AgentTraceProjector
from app.runtime.async_iterators import close_async_iterator
from app.runtime.json_types import JsonObject
from app.runtime.managed_claude_events import (
    AgentGovControlEvent,
    AgentGovHeartbeatEvent,
    ClaudeSdkMessageEvent,
    ManagedClaudeEvent,
    sdk_message_to_json,
    stream_delta,
    stream_event_payload,
)
from app.runtime.message_utils import (
    is_top_level_message,
    message_event_name,
    reconcile_stream_snapshot,
)
from app.runtime.openai_responses_adapter import (
    conversation_id_from_session,
    response_from_chat_response,
    response_id_from_run,
    response_output_items,
)
from app.runtime.openai_responses_tools import (
    ServerToolObservationProjector,
    ToolObservation,
)
from app.runtime.schemas import ChatResponse
from app.runtime.speech_summary import build_speech_summary_envelope
from app.sse_contracts import RESPONSES_PATH, require_registered_sse_event

HEARTBEAT_INTERVAL_S = 15
_ENVELOPE_VERSION = 1


def _sse(event_name: str, data: JsonObject, *, event_id: Optional[int] = None) -> str:
    require_registered_sse_event(RESPONSES_PATH, event_name)
    lines: list[str] = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event_name}")
    lines.append(f"data: {json.dumps(data, ensure_ascii=False)}")
    return "\n".join(lines) + "\n\n"


def _str(value: object) -> Optional[str]:
    return value if isinstance(value, str) else None


def _suggestion_list(data: JsonObject) -> list[str]:
    raw = data.get("suggestions")
    items = raw if isinstance(raw, list) else [data.get("suggestion")]
    return [item.strip() for item in items if isinstance(item, str) and item.strip()]


def _tool_step_from_trace(event: AgentTraceEvent) -> Optional[JsonObject]:
    if event.kind == "tool_use":
        return {
            "kind": "tool_use",
            "tool_name": event.payload.get("tool_name"),
            "tool_use_id": event.payload.get("tool_use_id"),
            "input": event.payload.get("input"),
        }
    if event.kind == "tool_result":
        return {
            "kind": "tool_result",
            "tool_use_id": event.payload.get("tool_use_id"),
            "result": event.payload.get("content"),
            "is_error": event.payload.get("is_error"),
        }
    return None


def _project_confirmation_requested(data: JsonObject) -> JsonObject:
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
    session = data.get("session_id") or data.get("api_session_id")
    return {
        **data,
        "agent_id": data.get("business_agent_id"),
        "conversation_id": conversation_id_from_session(_str(session)),
    }


def _created_response(
    run_id: Optional[str],
    model: Optional[str],
    session_id: Optional[str],
    created_at: int,
) -> JsonObject:
    return {
        "id": response_id_from_run(run_id),
        "object": "response",
        "created_at": created_at,
        "status": "in_progress",
        "model": model,
        "conversation": conversation_id_from_session(session_id),
        "output": [],
    }


def _content_snapshots(message: Any) -> tuple[str | None, str | None]:
    content = getattr(message, "content", None)
    if not isinstance(content, list):
        return None, None
    thinking_parts: list[str] = []
    text_parts: list[str] = []
    for block in content:
        thinking = getattr(block, "thinking", None)
        text = getattr(block, "text", None)
        if isinstance(thinking, str):
            thinking_parts.append(thinking)
        if isinstance(text, str):
            text_parts.append(text)
    return (
        "".join(thinking_parts) if thinking_parts else None,
        "".join(text_parts) if text_parts else None,
    )


@dataclass
class _ResponsesSseProjector:
    model: Optional[str]
    effective_agent_id: Optional[str]
    control: bool
    sdk_raw: bool
    include_trace: bool
    seq: int = 0
    run_id: Optional[str] = None
    session_id: Optional[str] = None
    created_at: Optional[int] = None
    answer_parts: list[str] = field(default_factory=list)
    reasoning_parts: list[str] = field(default_factory=list)
    partial_text_segment: str = ""
    partial_reasoning_segment: str = ""
    reasoning_open: bool = False
    reasoning_done: bool = False
    message_open: bool = False
    message_done: bool = False
    terminal_status: Optional[str] = None
    pending_completed_response: JsonObject | None = None
    pending_failure: JsonObject | None = None
    done_emitted: bool = False
    agentgov_error_emitted: bool = False
    trace_projector: AgentTraceProjector | None = None
    tool_observer: ServerToolObservationProjector = field(default_factory=ServerToolObservationProjector)

    @property
    def reasoning_item_id(self) -> Optional[str]:
        return f"rs_{self.run_id}" if self.run_id else None

    @property
    def message_item_id(self) -> Optional[str]:
        return f"msg_{self.run_id}" if self.run_id else None

    @property
    def message_output_index(self) -> int:
        return 1 if self.reasoning_open or self.reasoning_done or self.reasoning_parts else 0

    @property
    def reasoning_text(self) -> str:
        return "\n\n".join(self.reasoning_parts or ([self.partial_reasoning_segment] if self.partial_reasoning_segment else []))

    @property
    def answer_text(self) -> str:
        return "\n".join(self.answer_parts or ([self.partial_text_segment] if self.partial_text_segment else []))

    def _next(self) -> int:
        self.seq += 1
        return self.seq

    def _std(self, event_name: str, data: JsonObject) -> str:
        return _sse(event_name, {"type": event_name, "sequence_number": self._next(), **data})

    def _envelope(self, type_: str, content: JsonObject) -> str:
        seq_no = self._next()
        body: JsonObject = {
            "v": _ENVELOPE_VERSION,
            "type": type_,
            "run_id": self.run_id,
            "ts": time.time(),
            "seq": seq_no,
            "payload": content,
        }
        return _sse(type_, body, event_id=seq_no)

    def project(self, event: ManagedClaudeEvent) -> list[str]:
        if isinstance(event, AgentGovHeartbeatEvent):
            return [": keepalive\n\n"]
        if isinstance(event, ClaudeSdkMessageEvent):
            if self.done_emitted or self.pending_completed_response is not None or self.pending_failure is not None:
                return []
            return self._project_sdk_message(event.message)
        if not isinstance(event, AgentGovControlEvent):
            raise TypeError(f"Unsupported managed Claude event: {event.__class__.__name__}")
        if event.name == "prompt_suggestion":
            return self._project_prompt_suggestion(event.data)
        if event.name == "speech_summary":
            return self._project_speech_summary(event.data)
        if self.done_emitted:
            return []
        if event.name == "session":
            return self._project_session(event.data)
        if event.name == "result":
            return self._project_result(event.data)
        if event.name == "error":
            return self._project_error(event.data)
        if event.name == "claude_user_input_required" and self.control:
            return [
                self._envelope(
                    "agentgov.confirmation.requested",
                    _project_confirmation_requested(event.data),
                )
            ]
        if event.name == "claude_user_input_resolved" and self.control:
            return [
                self._envelope(
                    "agentgov.confirmation.resolved",
                    _project_confirmation_resolved(event.data),
                )
            ]
        if event.name == "done":
            return self._project_done()
        return []

    def _project_session(self, data: JsonObject) -> list[str]:
        self.run_id = _str(data.get("run_id"))
        self.session_id = _str(data.get("session_id"))
        self.created_at = int(time.time())
        if self.control and self.run_id:
            self.trace_projector = AgentTraceProjector(self.run_id)
        chunks = [
            self._std(
                "response.created",
                {
                    "response": _created_response(
                        self.run_id,
                        self.model,
                        self.session_id,
                        self.created_at,
                    )
                },
            )
        ]
        if self.control:
            chunks.append(
                self._envelope(
                    "agentgov.session",
                    {**data, "heartbeat_interval_s": HEARTBEAT_INTERVAL_S},
                )
            )
        return chunks

    def _project_sdk_message(self, message: Any) -> list[str]:
        raw = sdk_message_to_json(message)
        chunks: list[str] = []
        top_level = is_top_level_message(message)
        if message.__class__.__name__ == "StreamEvent":
            chunks.extend(self._project_stream_event(message, include_standard=top_level))
        else:
            chunks.extend(self._project_complete_message(message, raw, include_standard=top_level))
        if self.control and self.sdk_raw:
            chunks.append(
                self._envelope(
                    "agentgov.sdk_raw",
                    {"sdk_event": message.__class__.__name__, "raw": raw},
                )
            )
        return chunks

    def _project_stream_event(self, message: Any, *, include_standard: bool) -> list[str]:
        delta = stream_delta(message)
        if delta is not None:
            kind, value = delta
            if kind == "thinking_delta" and include_standard:
                return self._project_reasoning_delta(value)
            if kind == "text_delta" and include_standard:
                return self._project_text_delta(value)
            if kind == "input_json_delta":
                return self._tool_chunks(self.tool_observer.arguments_delta(message, value))
        event = stream_event_payload(message)
        if not event:
            return []
        event_type = event.get("type")
        if event_type == "content_block_start":
            return self._tool_chunks(self.tool_observer.content_block_start(event))
        if event_type == "content_block_stop":
            return self._tool_chunks(self.tool_observer.content_block_stop(event))
        return []

    def _open_reasoning(self) -> list[str]:
        if self.reasoning_open or self.reasoning_done:
            return []
        self.reasoning_open = True
        item_id = self.reasoning_item_id
        return [
            self._std(
                "response.output_item.added",
                {
                    "output_index": 0,
                    "item": {
                        "id": item_id,
                        "type": "reasoning",
                        "status": "in_progress",
                        "summary": [],
                        "content": [],
                    },
                },
            ),
            self._std(
                "response.content_part.added",
                {
                    "item_id": item_id,
                    "output_index": 0,
                    "content_index": 0,
                    "part": {"type": "reasoning_text", "text": ""},
                },
            ),
        ]

    def _project_reasoning_delta(self, text: str) -> list[str]:
        if not text:
            return []
        chunks = self._open_reasoning()
        self.partial_reasoning_segment += text
        chunks.append(
            self._std(
                "response.reasoning_text.delta",
                {
                    "item_id": self.reasoning_item_id,
                    "output_index": 0,
                    "content_index": 0,
                    "delta": text,
                },
            )
        )
        return chunks

    def _finish_reasoning(self) -> list[str]:
        if not self.reasoning_open or self.reasoning_done:
            return []
        text = self.reasoning_text
        self.reasoning_done = True
        self.reasoning_open = False
        item_id = self.reasoning_item_id
        return [
            self._std(
                "response.reasoning_text.done",
                {
                    "item_id": item_id,
                    "output_index": 0,
                    "content_index": 0,
                    "text": text,
                },
            ),
            self._std(
                "response.content_part.done",
                {
                    "item_id": item_id,
                    "output_index": 0,
                    "content_index": 0,
                    "part": {"type": "reasoning_text", "text": text},
                },
            ),
            self._std(
                "response.output_item.done",
                {
                    "output_index": 0,
                    "item": {
                        "id": item_id,
                        "type": "reasoning",
                        "status": "completed",
                        "summary": [],
                        "content": [{"type": "reasoning_text", "text": text}],
                    },
                },
            ),
        ]

    def _open_message(self) -> list[str]:
        if self.message_open or self.message_done:
            return []
        chunks = self._finish_reasoning()
        self.message_open = True
        index = self.message_output_index
        item_id = self.message_item_id
        chunks.extend(
            [
                self._std(
                    "response.output_item.added",
                    {
                        "output_index": index,
                        "item": {
                            "id": item_id,
                            "type": "message",
                            "status": "in_progress",
                            "role": "assistant",
                            "content": [],
                        },
                    },
                ),
                self._std(
                    "response.content_part.added",
                    {
                        "item_id": item_id,
                        "output_index": index,
                        "content_index": 0,
                        "part": {
                            "type": "output_text",
                            "text": "",
                            "annotations": [],
                        },
                    },
                ),
            ]
        )
        return chunks

    def _project_text_delta(self, text: str) -> list[str]:
        if not text:
            return []
        chunks = self._open_message()
        self.partial_text_segment += text
        chunks.append(
            self._std(
                "response.output_text.delta",
                {
                    "item_id": self.message_item_id,
                    "output_index": self.message_output_index,
                    "content_index": 0,
                    "delta": text,
                },
            )
        )
        return chunks

    def _finish_message(self) -> list[str]:
        if not self.message_open or self.message_done:
            return []
        text = self.answer_text
        self.message_done = True
        self.message_open = False
        index = self.message_output_index
        item_id = self.message_item_id
        part: JsonObject = {
            "type": "output_text",
            "text": text,
            "annotations": [],
        }
        item: JsonObject = {
            "id": item_id,
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [part],
        }
        return [
            self._std(
                "response.output_text.done",
                {
                    "item_id": item_id,
                    "output_index": index,
                    "content_index": 0,
                    "text": text,
                },
            ),
            self._std(
                "response.content_part.done",
                {
                    "item_id": item_id,
                    "output_index": index,
                    "content_index": 0,
                    "part": part,
                },
            ),
            self._std(
                "response.output_item.done",
                {"output_index": index, "item": item},
            ),
        ]

    def _project_complete_message(
        self,
        message: Any,
        raw: JsonObject,
        *,
        include_standard: bool,
    ) -> list[str]:
        chunks: list[str] = []
        if include_standard and message.__class__.__name__.startswith("AssistantMessage"):
            thinking_snapshot, text_snapshot = _content_snapshots(message)
            if thinking_snapshot is not None:
                suffix = reconcile_stream_snapshot(
                    self.partial_reasoning_segment,
                    thinking_snapshot,
                )
                if suffix:
                    chunks.extend(self._project_reasoning_delta(suffix))
                self.reasoning_parts.append(thinking_snapshot)
                self.partial_reasoning_segment = ""
            if text_snapshot is not None:
                suffix = reconcile_stream_snapshot(self.partial_text_segment, text_snapshot)
                if suffix:
                    chunks.extend(self._project_text_delta(suffix))
                self.answer_parts.append(text_snapshot)
                self.partial_text_segment = ""

        if self.control:
            chunks.extend(self._project_trace_message(message, raw))
            chunks.extend(self._tool_chunks(self.tool_observer.complete_content(raw)))
        return chunks

    def _project_trace_message(self, message: Any, raw: JsonObject) -> list[str]:
        if self.trace_projector is None:
            return []
        event_name = message_event_name(message)
        trace_events = self.trace_projector.project_message({**raw, "event": event_name})
        chunks: list[str] = []
        if self.include_trace:
            chunks.extend(
                self._envelope(
                    "agentgov.trace_event",
                    trace_event.model_dump(mode="json"),
                )
                for trace_event in trace_events
            )
        for trace_event in trace_events:
            step = _tool_step_from_trace(trace_event)
            if step:
                chunks.append(self._envelope("agentgov.tool_step", step))
        return chunks

    def _tool_chunks(self, observations: list[ToolObservation]) -> list[str]:
        if not self.control:
            return []
        return [self._envelope(event_name, payload) for event_name, payload in observations]

    def _project_prompt_suggestion(self, data: JsonObject) -> list[str]:
        if not self.control or self.done_emitted or self.pending_failure is not None:
            return []
        suggestions = _suggestion_list(data)
        if not suggestions:
            return []
        return [
            self._envelope(
                "agentgov.prompt_suggestion",
                {
                    "suggestion": suggestions[0],
                    "suggestions": suggestions,
                    "session_id": self.session_id,
                },
            )
        ]

    def _project_speech_summary(self, data: JsonObject) -> list[str]:
        if not self.control or self.done_emitted:
            return []
        seq_no = self._next()
        envelope = build_speech_summary_envelope(data, seq=seq_no)
        return [_sse("agentgov.speech_summary", envelope, event_id=seq_no)]

    def _response_from_result(self, data: JsonObject) -> JsonObject:
        chat = ChatResponse(
            run_id=str(data.get("run_id") or ""),
            session_id=str(data.get("session_id") or ""),
            sdk_session_id=_str(data.get("sdk_session_id")),
            agent_version_id=_str(data.get("agent_version_id")),
            answer=self.answer_text,
            agent_activity=data.get("agent_activity") if isinstance(data.get("agent_activity"), dict) else {},
            usage=data.get("usage") if isinstance(data.get("usage"), dict) else None,
            total_cost_usd=(data.get("total_cost_usd") if isinstance(data.get("total_cost_usd"), (int, float)) else None),
            stop_reason=_str(data.get("stop_reason")),
            errors=list(data.get("errors")) if isinstance(data.get("errors"), list) else [],
        )
        response = response_from_chat_response(
            chat,
            model=self.model,
            agent_id=self.effective_agent_id,
            metadata={},
            created_at=self.created_at,
        ).model_dump(exclude_none=True)
        response["output"] = [
            item.model_dump(mode="json")
            for item in response_output_items(
                run_id=chat.run_id,
                text=self.answer_text or None,
                reasoning=self.reasoning_text or None,
            )
        ]
        if not self.control:
            response.pop("agentgov", None)
        return response

    def _project_result(self, data: JsonObject) -> list[str]:
        chunks = [*self._finish_reasoning(), *self._finish_message()]
        response = self._response_from_result(data)
        raw_errors = data.get("errors")
        errors = [str(error) for error in raw_errors] if isinstance(raw_errors, list) else []
        failed_now = bool(errors) and self.terminal_status is None
        if failed_now:
            self.pending_completed_response = None
            self.pending_failure = {"response": response, "error": {"errors": errors}}
        elif not errors and self.terminal_status is None:
            self.pending_completed_response = response
        if self.control:
            chunks.append(self._envelope("agentgov.result", data))
            if failed_now:
                chunks.extend(self._project_agentgov_error({**data, "errors": errors}))
        return chunks

    def _project_error(self, data: JsonObject) -> list[str]:
        if self.done_emitted:
            return []
        self.pending_completed_response = None
        if self.pending_failure is None:
            self.pending_failure = {"error": data}
        return self._project_agentgov_error(data)

    def _project_agentgov_error(self, data: JsonObject) -> list[str]:
        if not self.control or self.agentgov_error_emitted:
            return []
        self.agentgov_error_emitted = True
        return [self._envelope("agentgov.error", data)]

    def _project_done(self) -> list[str]:
        if self.done_emitted:
            return []
        self.done_emitted = True
        chunks: list[str] = []
        if self.pending_completed_response is None and self.pending_failure is None:
            detail = "Agent stream ended without a ResultMessage"
            error: JsonObject = {
                "error_code": "STREAM_TERMINATED_WITHOUT_RESULT",
                "errors": [detail],
            }
            self.pending_failure = {"error": error}
            chunks.extend(self._project_agentgov_error(error))
        if self.control:
            chunks.append(self._envelope("agentgov.done", {}))
        if self.pending_failure is None and self.pending_completed_response is not None:
            self.terminal_status = "completed"
            chunks.append(
                self._std(
                    "response.completed",
                    {"response": self.pending_completed_response},
                )
            )
        else:
            self.terminal_status = "failed"
            chunks.append(self._std("response.failed", self.pending_failure or {"error": {}}))
        return chunks


async def iter_responses_sse(
    source: AsyncIterator[ManagedClaudeEvent],
    *,
    model: Optional[str],
    effective_agent_id: Optional[str],
    control: bool,
    sdk_raw: bool = False,
    include_trace: bool = False,
) -> AsyncIterator[str]:
    projector = _ResponsesSseProjector(
        model=model,
        effective_agent_id=effective_agent_id,
        control=control,
        sdk_raw=sdk_raw,
        include_trace=include_trace,
    )
    try:
        try:
            async for event in source:
                for chunk in projector.project(event):
                    yield chunk
        except Exception as exc:
            if projector.created_at is None:
                for chunk in projector._project_session({}):
                    yield chunk
            detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            error_code = getattr(exc, "error_code", None)
            error_data: JsonObject = {
                "error_code": (error_code if isinstance(error_code, str) and error_code else "STREAM_SOURCE_ERROR"),
                "errors": [detail],
            }
            error_details = getattr(exc, "error_details", None)
            if isinstance(error_details, dict):
                error_data.update(error_details)
            for chunk in projector._project_error(error_data):
                yield chunk
    finally:
        await close_async_iterator(source)
    for chunk in projector._project_done():
        yield chunk
