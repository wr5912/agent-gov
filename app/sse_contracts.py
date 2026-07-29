"""Typed, per-surface documentation contract for AgentGov SSE streams.

The existing Chat and Responses projectors intentionally remain independent while
the OpenAI conversion surface is transitional.  This module owns their public wire
catalog: event names, payload schemas, conditions, ordering phase, terminal flags,
comments, and realistic examples.  Contract tests compare observed projector output
with this catalog so a new wire event cannot remain undocumented.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

OpenApiObject = dict[str, object]
SsePhase = Literal["start", "content", "control", "terminal"]

CHAT_STREAM_PATH = "/api/chat/stream"
CLAUDE_SDK_EVENTS_PATH = "/api/agent-runtime/sdk-events"
RESPONSES_PATH = "/v1/responses"

_OPEN_OBJECT: OpenApiObject = {"type": "object", "additionalProperties": True}
_NULLABLE_STRING: OpenApiObject = {"type": ["string", "null"]}


def _object(
    *,
    required: tuple[str, ...] = (),
    properties: OpenApiObject | None = None,
    additional_properties: bool = True,
    description: str | None = None,
) -> OpenApiObject:
    schema: OpenApiObject = {
        "type": "object",
        "additionalProperties": additional_properties,
    }
    if required:
        schema["required"] = list(required)
    if properties:
        schema["properties"] = properties
    if description:
        schema["description"] = description
    return schema


def _standard_event(
    event_name: str,
    *,
    required: tuple[str, ...] = (),
    properties: OpenApiObject | None = None,
) -> OpenApiObject:
    event_properties: OpenApiObject = {
        "type": {"const": event_name},
        "sequence_number": {"type": "integer", "minimum": 1},
    }
    if properties:
        event_properties.update(properties)
    return _object(
        required=("type", "sequence_number", *required),
        properties=event_properties,
    )


def _agentgov_envelope(event_name: str, payload_schema: OpenApiObject) -> OpenApiObject:
    return _object(
        required=("v", "type", "run_id", "ts", "seq", "payload"),
        properties={
            "v": {"const": 1},
            "type": {"const": event_name},
            "run_id": _NULLABLE_STRING,
            "ts": {"type": "number"},
            "seq": {"type": "integer", "minimum": 1},
            "payload": payload_schema,
        },
        additional_properties=False,
    )


_SESSION_PAYLOAD = _object(
    required=("run_id", "session_id"),
    properties={
        "run_id": _NULLABLE_STRING,
        "session_id": _NULLABLE_STRING,
        "sdk_session_id": _NULLABLE_STRING,
        "agent_version_id": _NULLABLE_STRING,
        "langfuse_trace_id": _NULLABLE_STRING,
        "langfuse_trace_url": _NULLABLE_STRING,
    },
)
_ERROR_PAYLOAD = _object(
    properties={
        "error_code": {"type": "string"},
        "detail": {"type": "string"},
        "errors": {"type": "array", "items": {"type": "string"}},
    },
)
_RESULT_PAYLOAD = _object(
    required=("run_id", "session_id", "errors"),
    properties={
        "run_id": _NULLABLE_STRING,
        "session_id": _NULLABLE_STRING,
        "sdk_session_id": _NULLABLE_STRING,
        "errors": {"type": "array", "items": {"type": "string"}},
        "usage": {"type": ["object", "null"], "additionalProperties": True},
    },
)
_PROMPT_SUGGESTION_PAYLOAD = _object(
    required=("suggestion", "suggestions"),
    properties={
        "suggestion": {"type": "string"},
        "suggestions": {"type": "array", "items": {"type": "string"}},
        "run_id": _NULLABLE_STRING,
        "session_id": _NULLABLE_STRING,
    },
)
_CONFIRMATION_PAYLOAD = _object(
    required=("request_id",),
    properties={
        "request_id": {"type": "string"},
        "decision_token": {"type": "string"},
        "run_id": _NULLABLE_STRING,
        "session_id": _NULLABLE_STRING,
        "business_agent_id": _NULLABLE_STRING,
    },
)
_TOOL_CALL_PAYLOAD = _object(
    required=("tool_call_id",),
    properties={
        "tool_call_id": {"type": "string"},
        "name": {"type": "string"},
        "delta": {"type": "string"},
        "arguments": {"type": "string"},
        "input": {},
        "result": {},
        "is_error": {"type": "boolean"},
    },
)


@dataclass(frozen=True)
class SseEventSpec:
    event: str
    schema: OpenApiObject
    description: str
    phase: SsePhase
    condition: str | None = None
    terminal: bool = False
    open_family: bool = False

    def as_openapi(self) -> OpenApiObject:
        item: OpenApiObject = {
            "event": self.event,
            "schema": self.schema,
            "description": self.description,
            "phase": self.phase,
        }
        if self.condition:
            item["condition"] = self.condition
        if self.terminal:
            item["terminal"] = True
        if self.open_family:
            item["open_family"] = True
        return item


def _chat_specs() -> tuple[SseEventSpec, ...]:
    return (
        SseEventSpec("session", _SESSION_PAYLOAD, "Managed run/session identity.", "start"),
        SseEventSpec(
            "message",
            _object(
                required=("event", "text", "text_kind", "scope", "raw"),
                properties={
                    "event": {"type": "string"},
                    "text": {"type": "string"},
                    "text_kind": {"enum": ["delta", "snapshot", "transport", "metric"]},
                    "scope": {"enum": ["main", "subagent"]},
                    "parent_tool_use_id": _NULLABLE_STRING,
                    "raw": _OPEN_OBJECT,
                },
            ),
            "Legacy parsed SDK message projection.",
            "content",
        ),
        SseEventSpec(
            "trace_event",
            {"$ref": "#/components/schemas/AgentTraceEvent"},
            "Semantic trace fact derived from an SDK message.",
            "content",
            condition="event_mode=semantic",
        ),
        SseEventSpec("prompt_suggestion", _PROMPT_SUGGESTION_PAYLOAD, "Best-effort next-prompt suggestions.", "control"),
        SseEventSpec("claude_user_input_required", _CONFIRMATION_PAYLOAD, "HITL confirmation requested.", "control"),
        SseEventSpec("claude_user_input_resolved", _CONFIRMATION_PAYLOAD, "HITL confirmation resolved.", "control"),
        SseEventSpec("heartbeat", _object(required=("run_id", "timestamp")), "Data-frame keepalive for the legacy Chat surface.", "control"),
        SseEventSpec("result", _RESULT_PAYLOAD, "Runtime result metadata emitted before done.", "terminal"),
        SseEventSpec("error", _ERROR_PAYLOAD, "Runtime-managed in-stream error when available.", "terminal"),
        SseEventSpec(
            "agentgov.speech_summary",
            {"$ref": "#/components/schemas/AgentGovSpeechSummaryEnvelope"},
            "Opt-in best-effort speech summary.",
            "control",
            condition="with_speech_summary=true",
        ),
        SseEventSpec("done", {"const": "[DONE]", "type": "string"}, "Legacy stream terminator.", "terminal", terminal=True),
    )


def _sdk_specs() -> tuple[SseEventSpec, ...]:
    return (
        SseEventSpec(
            "claude.sdk.*",
            _object(description="Open event family following the pinned Claude Agent SDK dataclass."),
            "One mechanically serialized event for every value yielded by the pinned Claude Agent SDK.",
            "content",
            open_family=True,
        ),
        SseEventSpec("agentgov.session", _SESSION_PAYLOAD, "Managed run/session identity.", "start"),
        SseEventSpec("agentgov.prompt_suggestion", _PROMPT_SUGGESTION_PAYLOAD, "Best-effort next-prompt suggestions.", "control"),
        SseEventSpec("agentgov.confirmation.requested", _CONFIRMATION_PAYLOAD, "HITL confirmation requested.", "control"),
        SseEventSpec("agentgov.confirmation.resolved", _CONFIRMATION_PAYLOAD, "HITL confirmation resolved.", "control"),
        SseEventSpec(
            "agentgov.speech_summary",
            {"$ref": "#/components/schemas/AgentGovSpeechSummaryEnvelope"},
            "Opt-in best-effort speech summary.",
            "control",
            condition="with_speech_summary=true",
        ),
        SseEventSpec("agentgov.result", _RESULT_PAYLOAD, "Runtime result metadata emitted before done.", "terminal"),
        SseEventSpec("agentgov.error", _ERROR_PAYLOAD, "Runtime-managed in-stream error when available.", "terminal"),
        SseEventSpec("agentgov.done", _object(additional_properties=False), "Managed stream terminator.", "terminal", terminal=True),
    )


_RESPONSE_ITEM_SCHEMA = _object(
    required=("id", "type"),
    properties={"id": _NULLABLE_STRING, "type": {"type": "string"}},
)
_RESPONSE_PART_SCHEMA = _object(
    required=("type",),
    properties={"type": {"type": "string"}},
)


def _responses_start_specs() -> tuple[SseEventSpec, ...]:
    return (
        SseEventSpec(
            "response.created",
            _standard_event(
                "response.created",
                required=("response",),
                properties={
                    "response": _object(
                        required=(
                            "id",
                            "object",
                            "created_at",
                            "status",
                            "model",
                            "conversation",
                            "output",
                        ),
                        properties={
                            "id": _NULLABLE_STRING,
                            "object": {"const": "response"},
                            "created_at": {"type": "integer"},
                            "status": {"const": "in_progress"},
                            "model": _NULLABLE_STRING,
                            "conversation": _NULLABLE_STRING,
                            "output": {"type": "array"},
                        },
                    )
                },
            ),
            "Transitional created snapshot. A source failure before session identity may expose null id.",
            "start",
        ),
        SseEventSpec(
            "response.output_item.added",
            _standard_event(
                "response.output_item.added",
                required=("output_index", "item"),
                properties={
                    "output_index": {"type": "integer"},
                    "item": _RESPONSE_ITEM_SCHEMA,
                },
            ),
            "Reasoning or assistant output item opened.",
            "content",
        ),
        SseEventSpec(
            "response.content_part.added",
            _standard_event(
                "response.content_part.added",
                required=("item_id", "output_index", "content_index", "part"),
                properties={
                    "item_id": _NULLABLE_STRING,
                    "output_index": {"type": "integer"},
                    "content_index": {"type": "integer"},
                    "part": _RESPONSE_PART_SCHEMA,
                },
            ),
            "Reasoning or output-text content part opened.",
            "content",
        ),
    )


def _responses_delta_specs() -> tuple[SseEventSpec, ...]:
    return (
        SseEventSpec(
            "response.reasoning_text.delta",
            _standard_event(
                "response.reasoning_text.delta",
                required=("item_id", "output_index", "content_index", "delta"),
                properties={
                    "item_id": _NULLABLE_STRING,
                    "output_index": {"type": "integer"},
                    "content_index": {"type": "integer"},
                    "delta": {"type": "string"},
                },
            ),
            "Incremental reasoning text.",
            "content",
            condition="the SDK emits top-level thinking content",
        ),
        SseEventSpec(
            "response.output_text.delta",
            _standard_event(
                "response.output_text.delta",
                required=("item_id", "output_index", "content_index", "delta"),
                properties={
                    "item_id": _NULLABLE_STRING,
                    "output_index": {"type": "integer"},
                    "content_index": {"type": "integer"},
                    "delta": {"type": "string"},
                },
            ),
            "Incremental assistant output text.",
            "content",
        ),
    )


def _responses_content_done_specs() -> tuple[SseEventSpec, ...]:
    return (
        SseEventSpec(
            "response.reasoning_text.done",
            _standard_event(
                "response.reasoning_text.done",
                required=("item_id", "output_index", "content_index", "text"),
                properties={
                    "item_id": _NULLABLE_STRING,
                    "output_index": {"type": "integer"},
                    "content_index": {"type": "integer"},
                    "text": {"type": "string"},
                },
            ),
            "Completed reasoning text snapshot.",
            "content",
            condition="reasoning content was opened",
        ),
        SseEventSpec(
            "response.output_text.done",
            _standard_event(
                "response.output_text.done",
                required=("item_id", "output_index", "content_index", "text"),
                properties={
                    "item_id": _NULLABLE_STRING,
                    "output_index": {"type": "integer"},
                    "content_index": {"type": "integer"},
                    "text": {"type": "string"},
                },
            ),
            "Completed assistant output text snapshot.",
            "content",
        ),
        SseEventSpec(
            "response.content_part.done",
            _standard_event(
                "response.content_part.done",
                required=("item_id", "output_index", "content_index", "part"),
                properties={
                    "item_id": _NULLABLE_STRING,
                    "output_index": {"type": "integer"},
                    "content_index": {"type": "integer"},
                    "part": _RESPONSE_PART_SCHEMA,
                },
            ),
            "Content part closed.",
            "content",
        ),
        SseEventSpec(
            "response.output_item.done",
            _standard_event(
                "response.output_item.done",
                required=("output_index", "item"),
                properties={
                    "output_index": {"type": "integer"},
                    "item": _RESPONSE_ITEM_SCHEMA,
                },
            ),
            "Output item closed.",
            "content",
        ),
    )


def _responses_control_observation_specs() -> tuple[SseEventSpec, ...]:
    tool_specs = tuple(
        SseEventSpec(
            event_name,
            _agentgov_envelope(event_name, _TOOL_CALL_PAYLOAD),
            description,
            "control",
            condition="control mode and a server-side tool call reaches this phase",
        )
        for event_name, description in (
            ("agentgov.tool_call.started", "Server-side tool call opened."),
            ("agentgov.tool_call.arguments.delta", "Incremental server-side tool arguments."),
            ("agentgov.tool_call.arguments.done", "Server-side tool arguments completed."),
            ("agentgov.tool_call.result", "Server-side tool result observed."),
        )
    )
    return (
        SseEventSpec(
            "agentgov.session",
            _agentgov_envelope("agentgov.session", _SESSION_PAYLOAD),
            "Control-mode run/session identity.",
            "start",
            condition="control mode",
        ),
        SseEventSpec(
            "agentgov.sdk_raw",
            _agentgov_envelope("agentgov.sdk_raw", _object(required=("sdk_event", "raw"))),
            "Parsed SDK debug projection; not byte-exact Runtime stdout.",
            "control",
            condition="control mode and agentgov.debug.sdk_raw=true",
        ),
        SseEventSpec(
            "agentgov.trace_event",
            _agentgov_envelope("agentgov.trace_event", {"$ref": "#/components/schemas/AgentTraceEvent"}),
            "Complete semantic SDK trace fact.",
            "control",
            condition="control mode and agentgov.include_trace=true",
        ),
        SseEventSpec(
            "agentgov.tool_step",
            _agentgov_envelope("agentgov.tool_step", _OPEN_OBJECT),
            "Compatibility tool observation.",
            "control",
            condition="control mode and a server-side tool step occurs",
        ),
        *tool_specs,
    )


def _responses_control_lifecycle_specs() -> tuple[SseEventSpec, ...]:
    return (
        SseEventSpec(
            "agentgov.prompt_suggestion",
            _agentgov_envelope("agentgov.prompt_suggestion", _PROMPT_SUGGESTION_PAYLOAD),
            "Best-effort next-prompt suggestions.",
            "control",
            condition="control mode and suggestions are available",
        ),
        SseEventSpec(
            "agentgov.confirmation.requested",
            _agentgov_envelope("agentgov.confirmation.requested", _CONFIRMATION_PAYLOAD),
            "HITL confirmation requested.",
            "control",
            condition="control mode and Web HITL pauses a tool",
        ),
        SseEventSpec(
            "agentgov.confirmation.resolved",
            _agentgov_envelope("agentgov.confirmation.resolved", _CONFIRMATION_PAYLOAD),
            "HITL confirmation resolved.",
            "control",
            condition="control mode after a pending confirmation is resolved",
        ),
        SseEventSpec(
            "agentgov.speech_summary",
            {"$ref": "#/components/schemas/AgentGovSpeechSummaryEnvelope"},
            "Opt-in best-effort speech summary.",
            "control",
            condition="control mode and stream=true and agentgov.with_speech_summary=true",
        ),
        SseEventSpec(
            "agentgov.result",
            _agentgov_envelope("agentgov.result", _RESULT_PAYLOAD),
            "Control-mode Runtime result metadata.",
            "terminal",
            condition="control mode",
        ),
        SseEventSpec(
            "agentgov.error",
            _agentgov_envelope("agentgov.error", _ERROR_PAYLOAD),
            "Control-mode in-stream failure projection.",
            "terminal",
            condition="control mode and a managed error is available",
        ),
        SseEventSpec(
            "agentgov.done",
            _agentgov_envelope("agentgov.done", _object(additional_properties=False)),
            "Control-mode terminator emitted immediately before the standard terminal event.",
            "terminal",
            condition="control mode",
        ),
    )


def _responses_terminal_specs() -> tuple[SseEventSpec, ...]:
    return (
        SseEventSpec(
            "response.completed",
            _standard_event(
                "response.completed",
                required=("response",),
                properties={"response": {"$ref": "#/components/schemas/ResponseObject"}},
            ),
            "Successful standard terminal event; exactly one standard terminal is emitted on managed completion.",
            "terminal",
            terminal=True,
        ),
        SseEventSpec(
            "response.failed",
            _standard_event(
                "response.failed",
                required=("error",),
                properties={
                    "response": {"$ref": "#/components/schemas/ResponseObject"},
                    "error": _ERROR_PAYLOAD,
                },
            ),
            "Failed standard terminal event. Transitional source failures may omit response identity.",
            "terminal",
            terminal=True,
        ),
    )


def _responses_specs() -> tuple[SseEventSpec, ...]:
    return (
        *_responses_start_specs(),
        *_responses_delta_specs(),
        *_responses_content_done_specs(),
        *_responses_control_observation_specs(),
        *_responses_control_lifecycle_specs(),
        *_responses_terminal_specs(),
    )


SSE_CONTRACTS: dict[str, tuple[SseEventSpec, ...]] = {
    CHAT_STREAM_PATH: _chat_specs(),
    CLAUDE_SDK_EVENTS_PATH: _sdk_specs(),
    RESPONSES_PATH: _responses_specs(),
}

SSE_COMMENTS: dict[str, tuple[OpenApiObject, ...]] = {
    CLAUDE_SDK_EVENTS_PATH: (
        {
            "comment": "keepalive",
            "description": "Heartbeat comment; it is transport liveness and not a business event.",
            "example": ": keepalive run_id=<run_id> timestamp=<timestamp>\n\n",
        },
    ),
    RESPONSES_PATH: (
        {
            "comment": "keepalive",
            "description": "Heartbeat comment; it is transport liveness and not part of the Response timeline.",
            "example": ": keepalive\n\n",
        },
    ),
}

SSE_EXAMPLES: dict[str, str] = {
    CHAT_STREAM_PATH: 'event: session\ndata: {"run_id":"run_123","session_id":"sess_123"}\n\n',
    CLAUDE_SDK_EVENTS_PATH: 'event: agentgov.session\ndata: {"run_id":"run_123","session_id":"sess_123"}\n\n',
    RESPONSES_PATH: (
        'event: response.created\ndata: {"type":"response.created","sequence_number":1,'
        '"response":{"id":"resp_run_123","object":"response","created_at":1735689600,'
        '"status":"in_progress","model":"configured-model","conversation":"conv_sess_123","output":[]}}\n\n'
    ),
}


def sse_event_contract(path: str) -> list[OpenApiObject]:
    return [spec.as_openapi() for spec in SSE_CONTRACTS.get(path, ())]


def sse_event_names(path: str) -> frozenset[str]:
    return frozenset(spec.event for spec in SSE_CONTRACTS.get(path, ()))


def require_registered_sse_event(path: str, event_name: str) -> None:
    for spec in SSE_CONTRACTS.get(path, ()):
        if spec.event == event_name:
            return
        if spec.open_family and spec.event.endswith("*") and event_name.startswith(spec.event[:-1]):
            return
    raise ValueError(f"Unregistered SSE event for {path}: {event_name}")
