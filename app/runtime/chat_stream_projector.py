from __future__ import annotations

from collections.abc import AsyncIterator

from .async_iterators import close_async_iterator
from .json_types import JsonObject
from .managed_claude_events import (
    AgentGovControlEvent,
    AgentGovHeartbeatEvent,
    ClaudeSdkMessageEvent,
    ManagedClaudeEvent,
    sdk_message_to_json,
    stream_delta,
)
from .message_utils import extract_assistant_text_snapshot, extract_text, message_event_name


class ChatStreamProjector:
    """The legacy Chat endpoint's own SDK-to-Chat projection.

    It intentionally does not serve as a parent/base normalizer for Responses or the
    SDK-native endpoint.
    """

    def project(self, event: ManagedClaudeEvent) -> list[JsonObject]:
        if isinstance(event, AgentGovHeartbeatEvent):
            return [
                {
                    "event": "heartbeat",
                    "data": {"run_id": event.run_id, "timestamp": event.timestamp},
                }
            ]
        if isinstance(event, AgentGovControlEvent):
            return [
                {
                    "event": event.name,
                    "data": "[DONE]" if event.name == "done" else event.data,
                }
            ]
        if not isinstance(event, ClaudeSdkMessageEvent):
            raise TypeError(f"Unsupported managed Claude event: {event.__class__.__name__}")

        message = event.message
        raw = sdk_message_to_json(message)
        event_name = message_event_name(message)
        delta = stream_delta(message)
        if delta is not None:
            delta_kind, text = delta
            # 保留旧 Chat 文本 delta 的 ``StreamEvent`` 名称；新增的 thinking /
            # tool-input delta 使用显式 subtype，避免把兼容入口变成新共享协议。
            projected_event = "StreamEvent" if delta_kind == "text_delta" else f"StreamEvent:{delta_kind}"
            return [
                {
                    "event": "message",
                    "data": {
                        "event": projected_event,
                        "text": text,
                        "text_kind": "delta",
                        "raw": raw,
                    },
                }
            ]
        if message.__class__.__name__ == "StreamEvent":
            return [
                {
                    "event": "message",
                    "data": {
                        "event": "StreamEvent",
                        "text": "",
                        "text_kind": "transport",
                        "raw": raw,
                    },
                }
            ]

        snapshot = extract_assistant_text_snapshot(message)
        text = snapshot if snapshot is not None else extract_text(message)
        text_kind = "metric" if getattr(message, "subtype", None) == "thinking_tokens" else "snapshot"
        return [
            {
                "event": "message",
                "data": {
                    "event": event_name,
                    "text": text,
                    "text_kind": text_kind,
                    "raw": {**raw, "event": event_name},
                },
            }
        ]


async def iter_chat_frames(source: AsyncIterator[ManagedClaudeEvent]) -> AsyncIterator[JsonObject]:
    projector = ChatStreamProjector()
    try:
        async for event in source:
            for frame in projector.project(event):
                yield frame
    finally:
        await close_async_iterator(source)
