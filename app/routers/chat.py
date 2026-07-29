from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request

from app.runtime.agent_profile_resolver import resolve_business_profile
from app.runtime.async_iterators import close_async_iterator
from app.runtime.chat_stream_projector import ChatStreamProjector
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.http_disconnect import run_while_request_connected
from app.runtime.managed_claude_events import AgentGovControlEvent
from app.runtime.managed_streaming_response import ManagedStreamingResponse
from app.runtime.native_chat_stream import NativeChatSemanticProjector
from app.runtime.prepared_managed_stream import (
    PreparedManagedEventStream,
    managed_run_response_headers,
    prepare_managed_event_stream,
)
from app.runtime.schemas import ChatResponse
from app.runtime.settings import AppSettings
from app.runtime.speech_summary import build_speech_summary_envelope
from app.runtime.stores.agent_registry_store import AgentRegistryStore
from app.runtime.stream_request_schemas import AgentTargetedChatRequest, ChatStreamRequest
from app.sse_contracts import CHAT_STREAM_PATH, require_registered_sse_event

from .runtime_preflight import require_non_stream_hitl_free, require_stream_hitl_available


async def _iter_chat_sse(
    prepared: PreparedManagedEventStream,
    *,
    event_mode: Literal["raw", "semantic"],
) -> AsyncIterator[str]:
    chat_projector = ChatStreamProjector()
    semantic_projector = NativeChatSemanticProjector() if event_mode == "semantic" else None
    data_frame_seq = 0
    source = prepared.iter_events()
    try:
        async for managed_event in source:
            if isinstance(managed_event, AgentGovControlEvent) and managed_event.name == "speech_summary":
                data_frame_seq += 1
                require_registered_sse_event(CHAT_STREAM_PATH, "agentgov.speech_summary")
                envelope = build_speech_summary_envelope(
                    managed_event.data,
                    seq=data_frame_seq,
                )
                yield f"event: agentgov.speech_summary\ndata: {json.dumps(envelope, ensure_ascii=False)}\n\n"
                continue
            for item in chat_projector.project(managed_event):
                projected = semantic_projector.project(item) if semantic_projector is not None else [item]
                for frame in projected:
                    data_frame_seq += 1
                    event = frame.get("event", "message")
                    require_registered_sse_event(CHAT_STREAM_PATH, str(event))
                    data = json.dumps(frame.get("data"), ensure_ascii=False)
                    yield f"event: {event}\ndata: {data}\n\n"
    finally:
        await close_async_iterator(source)


def create_chat_router(
    *,
    runtime: ClaudeRuntime,
    settings: AppSettings,
    agent_registry_store: AgentRegistryStore,
    require_api_key: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["chat"], dependencies=[Depends(require_api_key)])

    @router.post(
        "/chat",
        response_model=ChatResponse,
        summary="Run a Claude Agent task and return the full result",
        description="Runs one Claude Agent SDK query. Requires a registered business agent_id.",
        deprecated=True,
    )
    async def chat(req: AgentTargetedChatRequest, request: Request) -> ChatResponse:
        profile = resolve_business_profile(settings, agent_registry_store, req.agent_id)
        require_non_stream_hitl_free(profile, surface="/api/chat")
        return await run_while_request_connected(
            request,
            runtime.run(req, profile=profile),
        )

    @router.post(
        "/chat/stream",
        summary="Run a Claude Agent task as server-sent events",
        description="Streams session, message, prompt_suggestion, result, error, and done events as text/event-stream. Requires a registered business agent_id.",
        deprecated=True,
    )
    async def chat_stream(
        req: ChatStreamRequest,
        event_mode: Literal["raw", "semantic"] = Query(
            default="raw",
            description=(
                "raw preserves the legacy AgentGov SSE projection of parsed SDK messages; it is not byte-exact Runtime stdout. "
                "semantic adds complete trace_event facts and suppresses transport noise."
            ),
        ),
    ) -> ManagedStreamingResponse:
        profile = resolve_business_profile(settings, agent_registry_store, req.agent_id)
        require_stream_hitl_available(profile, settings, surface="/api/chat/stream")

        prepared = await prepare_managed_event_stream(
            runtime.stream_events(
                req,
                profile=profile,
                with_speech_summary=req.with_speech_summary,
            )
        )

        return ManagedStreamingResponse(
            _iter_chat_sse(prepared, event_mode=event_mode),
            media_type="text/event-stream",
            headers=managed_run_response_headers(prepared.metadata),
        )

    return router
