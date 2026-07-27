from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.types import JsonValue

from .json_types import JsonObject

AgentTraceKind = Literal[
    "thinking",
    "text",
    "tool_use",
    "tool_result",
    "hook",
    "task",
    "system",
    "result",
    "sdk_message",
    "content_block",
]
AgentTraceScope = Literal["main", "subagent"]
AgentTraceCompleteness = Literal["complete", "unavailable"]
TurnStatus = Literal["running", "succeeded", "failed", "cancelled", "interrupted"]


class AgentTraceEvent(BaseModel):
    """One stable semantic event derived from a complete Claude SDK message."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    run_id: str
    sequence: int = Field(ge=1)
    message_index: int = Field(ge=0)
    block_index: int | None = Field(default=None, ge=0)
    kind: AgentTraceKind
    source_event: str
    scope: AgentTraceScope
    parent_tool_use_id: str | None = None
    subagent_id: str | None = None
    payload: dict[str, JsonValue] = Field(default_factory=dict)


class AgentRunTraceResponse(BaseModel):
    """Refresh-safe Trace projection backed by the persisted AgentRun timeline."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    session_id: str | None = None
    sdk_session_id: str | None = None
    agent_version_id: str | None = None
    langfuse_trace_id: str | None = None
    langfuse_trace_url: str | None = None
    alert_id: str | None = None
    case_id: str | None = None
    turn_status: TurnStatus | None = None
    turn_index: int | None = Field(default=None, ge=0)
    turn_error: JsonObject | None = None
    errors: list[str] = Field(default_factory=list)
    completeness: AgentTraceCompleteness
    events: list[AgentTraceEvent] = Field(default_factory=list)
    agent_activity: JsonObject = Field(default_factory=dict)
    created_at: str | None = None
    completed_at: str | None = None


class AgentTraceProjector:
    """Incrementally project complete SDK facts; transport-only frames are ignored."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.message_index = 0
        self.sequence = 0

    def project_message(self, message: JsonObject) -> list[AgentTraceEvent]:
        message_index = self.message_index
        self.message_index += 1
        source_event = _source_event(message)
        if _is_transport_only(source_event, message):
            return []

        content = message.get("content")
        if isinstance(content, list):
            events: list[AgentTraceEvent] = []
            for block_index, block in enumerate(content):
                if not isinstance(block, dict):
                    events.append(
                        self._event(
                            message_index=message_index,
                            block_index=block_index,
                            kind="content_block",
                            source_event=source_event,
                            message=message,
                            payload={"content": block},
                        )
                    )
                    continue
                kind, payload = _project_content_block(block)
                events.append(
                    self._event(
                        message_index=message_index,
                        block_index=block_index,
                        kind=kind,
                        source_event=source_event,
                        message=message,
                        payload=payload,
                    )
                )
            return events

        kind, payload = _project_message(source_event, message)
        return [
            self._event(
                message_index=message_index,
                block_index=None,
                kind=kind,
                source_event=source_event,
                message=message,
                payload=payload,
            )
        ]

    def _event(
        self,
        *,
        message_index: int,
        block_index: int | None,
        kind: AgentTraceKind,
        source_event: str,
        message: JsonObject,
        payload: dict[str, JsonValue],
    ) -> AgentTraceEvent:
        self.sequence += 1
        parent_tool_use_id = _string(message.get("parent_tool_use_id"))
        subagent_id = _subagent_id(message)
        event_key = f"{self.run_id}:{message_index}:{block_index if block_index is not None else 'message'}"
        event_id = f"trace_{hashlib.sha256(event_key.encode('utf-8')).hexdigest()[:24]}"
        return AgentTraceEvent(
            event_id=event_id,
            run_id=self.run_id,
            sequence=self.sequence,
            message_index=message_index,
            block_index=block_index,
            kind=kind,
            source_event=source_event,
            scope="subagent" if parent_tool_use_id else "main",
            parent_tool_use_id=parent_tool_use_id,
            subagent_id=subagent_id,
            payload={**_message_context(message), **payload},
        )


def project_agent_trace(run_id: str, messages: list[JsonObject]) -> list[AgentTraceEvent]:
    projector = AgentTraceProjector(run_id)
    events: list[AgentTraceEvent] = []
    for message in messages:
        events.extend(projector.project_message(message))
    return events


def _source_event(message: JsonObject) -> str:
    explicit = _string(message.get("event"))
    if explicit:
        return explicit
    type_name = _string(message.get("type")) or _string(message.get("role"))
    subtype = _string(message.get("subtype"))
    if type_name and subtype:
        return f"{type_name}:{subtype}"
    return type_name or "SdkMessage"


def _is_transport_only(source_event: str, message: JsonObject) -> bool:
    return (
        source_event == "StreamEvent"
        or source_event == "SystemMessage:thinking_tokens"
        or (source_event.startswith("SystemMessage") and message.get("subtype") == "thinking_tokens")
    )


def _project_content_block(block: JsonObject) -> tuple[AgentTraceKind, JsonObject]:
    thinking = block.get("thinking")
    if isinstance(thinking, str):
        return "thinking", {"thinking": thinking}
    text = block.get("text")
    if isinstance(text, str):
        return "text", {"text": text}
    if "name" in block and "input" in block:
        return (
            "tool_use",
            {
                "tool_name": block.get("name"),
                "tool_use_id": block.get("id"),
                "input": block.get("input"),
            },
        )
    if "tool_use_id" in block:
        return (
            "tool_result",
            {
                "tool_use_id": block.get("tool_use_id"),
                "content": block.get("content"),
                "is_error": block.get("is_error"),
            },
        )
    return "content_block", {"block": _without_opaque_signature(block)}


def _project_message(source_event: str, message: JsonObject) -> tuple[AgentTraceKind, JsonObject]:
    if source_event.startswith("HookEventMessage"):
        return "hook", _message_body(message)
    if source_event.startswith(("TaskStartedMessage", "TaskProgressMessage", "TaskNotificationMessage")):
        return "task", _message_body(message)
    if source_event.startswith("SystemMessage"):
        return "system", _message_body(message)
    if source_event.startswith("ResultMessage"):
        return "result", _message_body(message)
    content = message.get("content")
    if isinstance(content, str):
        return "text", {"text": content}
    return "sdk_message", _message_body(message)


def _message_context(message: JsonObject) -> JsonObject:
    context: JsonObject = {}
    for key in ("uuid", "session_id", "message_id", "model", "stop_reason", "subtype"):
        if key in message and message[key] is not None:
            context[key] = message[key]
    return context


def _message_body(message: JsonObject) -> JsonObject:
    return {key: value for key, value in message.items() if key not in {"event", "content", "parent_tool_use_id", "signature"}}


def _without_opaque_signature(block: JsonObject) -> JsonObject:
    return {key: value for key, value in block.items() if key != "signature"}


def _subagent_id(message: JsonObject) -> str | None:
    for key in ("subagent_id", "task_id", "agent_id"):
        value = _string(message.get(key))
        if value:
            return value
    return None


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
