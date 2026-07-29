from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass

from .async_iterators import close_async_iterator
from .json_types import JsonObject
from .managed_claude_events import AgentGovControlEvent, ManagedClaudeEvent

MANAGED_RUN_RESPONSE_HEADER_DESCRIPTIONS: Mapping[str, str] = {
    "X-AgentGov-Run-Id": "Backend-owned run id used by the exact-run cancellation endpoint.",
    "X-AgentGov-Session-Id": "Backend-owned AgentGov session id that owns the run.",
}
MANAGED_RUN_RESPONSE_HEADER_NAMES = tuple(MANAGED_RUN_RESPONSE_HEADER_DESCRIPTIONS)


@dataclass(frozen=True)
class ManagedRunMetadata:
    run_id: str
    session_id: str


class PreparedManagedEventStream:
    def __init__(
        self,
        *,
        source: AsyncIterator[ManagedClaudeEvent],
        first_events: tuple[ManagedClaudeEvent, ...],
        metadata: ManagedRunMetadata | None,
    ) -> None:
        self.source = source
        self.first_events = first_events
        self.metadata = metadata
        self._consumed = False
        self._closed = False
        self._close_lock = asyncio.Lock()

    async def iter_events(self) -> AsyncIterator[ManagedClaudeEvent]:
        if self._consumed:
            raise RuntimeError("Managed Runtime stream can only be consumed once")
        self._consumed = True
        try:
            for event in self.first_events:
                yield event
            async for event in self.source:
                yield event
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            await close_async_iterator(self.source)


async def prepare_managed_event_stream(
    source: AsyncIterator[ManagedClaudeEvent],
) -> PreparedManagedEventStream:
    try:
        first_event = await anext(source)
        if not isinstance(first_event, AgentGovControlEvent) or first_event.name != "session":
            raise RuntimeError("Managed Runtime did not expose its run handle before streaming")
        run_id = first_event.data.get("run_id")
        session_id = first_event.data.get("session_id")
        if not isinstance(run_id, str) or not run_id or not isinstance(session_id, str) or not session_id:
            raise RuntimeError("Managed Runtime session event is missing run_id or session_id")
        return PreparedManagedEventStream(
            source=source,
            first_events=(first_event,),
            metadata=ManagedRunMetadata(run_id=run_id, session_id=session_id),
        )
    except Exception as exc:
        await close_async_iterator(source)
        return PreparedManagedEventStream(
            source=_empty_managed_source(),
            first_events=(
                AgentGovControlEvent(name="error", data=_stream_start_error(exc)),
                AgentGovControlEvent(name="done", data={}),
            ),
            metadata=None,
        )
    except BaseException:
        await close_async_iterator(source)
        raise


def managed_run_response_headers(
    metadata: ManagedRunMetadata | None,
    *,
    cache_control: str = "no-store",
) -> Mapping[str, str]:
    headers = {
        "Cache-Control": cache_control,
        "X-Accel-Buffering": "no",
    }
    if metadata is not None:
        headers.update(
            {
                "X-AgentGov-Run-Id": metadata.run_id,
                "X-AgentGov-Session-Id": metadata.session_id,
            }
        )
    return headers


async def _empty_managed_source() -> AsyncIterator[ManagedClaudeEvent]:
    if False:
        yield AgentGovControlEvent(name="done", data={})


def _stream_start_error(exc: Exception) -> JsonObject:
    detail = f"{exc.__class__.__name__}: {exc}" if str(exc) else exc.__class__.__name__
    error_code = getattr(exc, "error_code", None)
    data: JsonObject = {
        "error_code": error_code if isinstance(error_code, str) and error_code else "STREAM_SOURCE_ERROR",
        "errors": [detail],
    }
    error_details = getattr(exc, "error_details", None)
    if isinstance(error_details, dict):
        data.update(error_details)
    return data
