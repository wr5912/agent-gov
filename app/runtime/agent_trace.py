from __future__ import annotations

import asyncio
import time
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


TraceLevel = Literal["debug", "info", "warning", "error"]
TraceChannel = Literal["agent", "tool", "workflow", "model", "system"]


class AgentTraceEvent(BaseModel):
    run_id: str = Field(alias="runId")
    seq: int
    stream_seq: int = Field(alias="streamSeq")
    timestamp: str
    level: TraceLevel = "info"
    channel: TraceChannel = "agent"
    type: str
    title: str
    content: Optional[str] = None
    duration_ms: Optional[int] = Field(default=None, alias="durationMs")
    payload: Optional[Any] = None

    model_config = {"populate_by_name": True}


class InMemoryAgentTraceStore:
    def __init__(self, *, max_events_per_run: int = 500, max_global_events: int = 1000) -> None:
        self._max_events_per_run = max_events_per_run
        self._max_global_events = max_global_events
        self._events: dict[str, list[AgentTraceEvent]] = {}
        self._global_events: list[AgentTraceEvent] = []
        self._next_seq: dict[str, int] = {}
        self._next_stream_seq = 1
        self._condition = asyncio.Condition()

    async def publish(
        self,
        run_id: str,
        *,
        level: TraceLevel = "info",
        channel: TraceChannel = "agent",
        event_type: str,
        title: str,
        content: str | None = None,
        duration_ms: int | None = None,
        payload: Any | None = None,
    ) -> AgentTraceEvent:
        async with self._condition:
            seq = self._next_seq.get(run_id, 1)
            self._next_seq[run_id] = seq + 1
            stream_seq = self._next_stream_seq
            self._next_stream_seq += 1
            event = AgentTraceEvent(
                runId=run_id,
                seq=seq,
                streamSeq=stream_seq,
                timestamp=_timestamp_iso(),
                level=level,
                channel=channel,
                type=event_type,
                title=title,
                content=_clip_string(content) if content else None,
                durationMs=duration_ms,
                payload=sanitize_trace_payload(payload),
            )
            run_events = self._events.setdefault(run_id, [])
            run_events.append(event)
            if len(run_events) > self._max_events_per_run:
                del run_events[: len(run_events) - self._max_events_per_run]
            self._global_events.append(event)
            if len(self._global_events) > self._max_global_events:
                del self._global_events[: len(self._global_events) - self._max_global_events]
            self._condition.notify_all()
            return event

    async def list_after(self, run_id: str, seq: int | None = None) -> list[AgentTraceEvent]:
        async with self._condition:
            return [
                event
                for event in self._events.get(run_id, [])
                if seq is None or event.seq > seq
            ]

    async def list_global_after(self, stream_seq: int | None = None) -> list[AgentTraceEvent]:
        async with self._condition:
            return [
                event
                for event in self._global_events
                if stream_seq is None or event.stream_seq > stream_seq
            ]

    async def wait_for_events(
        self,
        run_id: str,
        seq: int | None = None,
        *,
        timeout: float = 15.0,
    ) -> list[AgentTraceEvent]:
        async with self._condition:
            events = [
                event
                for event in self._events.get(run_id, [])
                if seq is None or event.seq > seq
            ]
            if events:
                return events
            try:
                await asyncio.wait_for(self._condition.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                return []
            return [
                event
                for event in self._events.get(run_id, [])
                if seq is None or event.seq > seq
            ]

    async def wait_for_global_events(
        self,
        stream_seq: int | None = None,
        *,
        timeout: float = 15.0,
    ) -> list[AgentTraceEvent]:
        async with self._condition:
            events = [
                event
                for event in self._global_events
                if stream_seq is None or event.stream_seq > stream_seq
            ]
            if events:
                return events
            try:
                await asyncio.wait_for(self._condition.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                return []
            return [
                event
                for event in self._global_events
                if stream_seq is None or event.stream_seq > stream_seq
            ]


SENSITIVE_KEY_FRAGMENTS = (
    "authorization",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "token",
    "secret",
    "password",
    "cookie",
    "set-cookie",
)


def sanitize_trace_payload(value: Any, *, depth: int = 0) -> Any:
    if value is None:
        return None
    if depth >= 4:
        return "[truncated]"
    if isinstance(value, str):
        return _clip_string(value)
    if isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, list):
        return [sanitize_trace_payload(item, depth=depth + 1) for item in value[:25]]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:50]:
            key_text = str(key)
            if _is_sensitive_key(key_text):
                result[key_text] = "[redacted]"
            else:
                result[key_text] = sanitize_trace_payload(item, depth=depth + 1)
        return result
    return _clip_string(str(value))


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(fragment in normalized for fragment in SENSITIVE_KEY_FRAGMENTS)


def _clip_string(value: str, *, limit: int = 1200) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}... [truncated {len(value) - limit} chars]"


def _timestamp_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
