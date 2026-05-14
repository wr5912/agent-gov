from __future__ import annotations

import time
import uuid
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .schemas import ChatRequest


class AgUiMessage(BaseModel):
    id: Optional[str] = None
    role: str
    content: Optional[str] = None
    name: Optional[str] = None
    tool_calls: Optional[list[dict[str, Any]]] = Field(default=None, alias="toolCalls")
    tool_call_id: Optional[str] = Field(default=None, alias="toolCallId")

    model_config = {"populate_by_name": True, "extra": "allow"}


class RunAgentInput(BaseModel):
    thread_id: str = Field(default_factory=lambda: f"thread-{uuid.uuid4()}", alias="threadId")
    run_id: str = Field(default_factory=lambda: f"run-{uuid.uuid4()}", alias="runId")
    parent_run_id: Optional[str] = Field(default=None, alias="parentRunId")
    state: Any = Field(default_factory=dict)
    messages: list[AgUiMessage] = Field(default_factory=list)
    tools: list[dict[str, Any]] = Field(default_factory=list)
    context: list[dict[str, Any]] = Field(default_factory=list)
    forwarded_props: dict[str, Any] = Field(default_factory=dict, alias="forwardedProps")

    model_config = {"populate_by_name": True, "extra": "allow"}


class PublishNotificationRequest(BaseModel):
    name: str
    value: dict[str, Any] = Field(default_factory=dict)
    notification_id: Optional[str] = Field(default=None, alias="notificationId")
    workspace_id: Optional[str] = Field(default=None, alias="workspaceId")
    user_id: Optional[str] = Field(default=None, alias="userId")

    model_config = {"populate_by_name": True, "extra": "forbid"}


def run_input_to_chat_request(req: RunAgentInput) -> ChatRequest:
    message = _message_from_run_input(req)
    forwarded = req.forwarded_props or {}
    metadata = _metadata_from_run_input(req)
    return ChatRequest(
        message=message,
        session_id=req.thread_id,
        agent=_string_or_none(forwarded.get("agent")),
        skills=_string_list_or_none(forwarded.get("skills")),
        skills_mode=_string_or_none(forwarded.get("skillsMode")),
        allowed_tools=_string_list_or_none(forwarded.get("allowedTools")),
        disallowed_tools=_string_list_or_none(forwarded.get("disallowedTools")),
        max_turns=_int_or_none(forwarded.get("maxTurns")),
        model=_string_or_none(forwarded.get("model")),
        permission_mode=_string_or_none(forwarded.get("permissionMode")),
        system_append=_string_or_none(forwarded.get("systemAppend")),
        metadata=metadata,
    )


def run_started_event(req: RunAgentInput) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "RUN_STARTED",
        "threadId": req.thread_id,
        "runId": req.run_id,
        "timestamp": _timestamp_ms(),
    }
    if req.parent_run_id:
        event["parentRunId"] = req.parent_run_id
    return event


def run_finished_event(req: RunAgentInput, result: Any = None) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "RUN_FINISHED",
        "threadId": req.thread_id,
        "runId": req.run_id,
        "timestamp": _timestamp_ms(),
        "outcome": {"type": "success"},
    }
    if result is not None:
        event["result"] = result
    return event


def run_error_event(req: RunAgentInput, message: str, code: str = "runtime-error") -> dict[str, Any]:
    return {
        "type": "RUN_ERROR",
        "threadId": req.thread_id,
        "runId": req.run_id,
        "message": message,
        "code": code,
        "timestamp": _timestamp_ms(),
    }


def text_message_start_event(message_id: str, role: Literal["assistant"] = "assistant") -> dict[str, Any]:
    return {
        "type": "TEXT_MESSAGE_START",
        "messageId": message_id,
        "role": role,
        "timestamp": _timestamp_ms(),
    }


def text_message_content_event(message_id: str, delta: str) -> dict[str, Any]:
    return {
        "type": "TEXT_MESSAGE_CONTENT",
        "messageId": message_id,
        "delta": delta,
        "timestamp": _timestamp_ms(),
    }


def text_message_end_event(message_id: str) -> dict[str, Any]:
    return {
        "type": "TEXT_MESSAGE_END",
        "messageId": message_id,
        "timestamp": _timestamp_ms(),
    }


def custom_event(name: str, value: Any) -> dict[str, Any]:
    return {
        "type": "CUSTOM",
        "name": name,
        "value": value,
        "timestamp": _timestamp_ms(),
    }


def notification_event(
    *,
    notification_id: str,
    name: str,
    value: dict[str, Any],
    created_at: str,
) -> dict[str, Any]:
    payload = dict(value)
    payload.setdefault("notificationId", notification_id)
    payload.setdefault("createdAt", created_at)
    event = custom_event(name, payload)
    event["notificationId"] = notification_id
    return event


def _message_from_run_input(req: RunAgentInput) -> str:
    forwarded_message = req.forwarded_props.get("message") if isinstance(req.forwarded_props, dict) else None
    if isinstance(forwarded_message, str) and forwarded_message.strip():
        return forwarded_message.strip()

    for message in reversed(req.messages):
        if message.role == "user" and isinstance(message.content, str) and message.content.strip():
            return message.content.strip()

    for message in reversed(req.messages):
        if isinstance(message.content, str) and message.content.strip():
            return message.content.strip()

    return ""


def _metadata_from_run_input(req: RunAgentInput) -> dict[str, Any]:
    metadata = req.forwarded_props.get("metadata") if isinstance(req.forwarded_props, dict) else None
    if not isinstance(metadata, dict):
        metadata = {}
    return {
        **metadata,
        "ag_ui": {
            "threadId": req.thread_id,
            "runId": req.run_id,
            "parentRunId": req.parent_run_id,
            "state": req.state,
            "context": req.context,
        },
    }


def _timestamp_ms() -> int:
    return int(time.time() * 1000)


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _string_list_or_none(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    result = [item for item in value if isinstance(item, str) and item]
    return result or None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None
