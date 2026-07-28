from __future__ import annotations

import json
from collections.abc import Callable
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from app.runtime.agent_profile_resolver import resolve_business_profile
from app.runtime.chat_stream_projector import ChatStreamProjector
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.managed_claude_events import AgentGovControlEvent
from app.runtime.native_chat_stream import NativeChatSemanticProjector
from app.runtime.schemas import ChatRequest, ChatResponse
from app.runtime.settings import AppSettings
from app.runtime.speech_summary import build_speech_summary_envelope
from app.runtime.stores.agent_registry_store import AgentRegistryStore
from app.runtime.stream_request_schemas import ChatStreamRequest

from .runtime_preflight import require_non_stream_hitl_free, require_stream_hitl_available


def _require_agent_id(req: ChatRequest) -> None:
    """两个原生 chat 入口都要求显式有效 agent_id，不使用默认业务 Agent。"""
    if not (req.agent_id and req.agent_id.strip()):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="agent_id is required and must identify a registered business agent",
        )


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
    async def chat(req: ChatRequest) -> ChatResponse:
        _require_agent_id(req)
        profile = resolve_business_profile(settings, agent_registry_store, req.agent_id)
        require_non_stream_hitl_free(profile, surface="/api/chat")
        return await runtime.run(req, profile=profile)

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
    ) -> StreamingResponse:
        _require_agent_id(req)
        profile = resolve_business_profile(settings, agent_registry_store, req.agent_id)
        require_stream_hitl_available(profile, settings, surface="/api/chat/stream")

        async def event_stream():
            chat_projector = ChatStreamProjector()
            semantic_projector = NativeChatSemanticProjector() if event_mode == "semantic" else None
            data_frame_seq = 0
            async for managed_event in runtime.stream_events(
                req,
                profile=profile,
                with_speech_summary=req.with_speech_summary,
            ):
                if isinstance(managed_event, AgentGovControlEvent) and managed_event.name == "speech_summary":
                    data_frame_seq += 1
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
                        data = json.dumps(frame.get("data"), ensure_ascii=False)
                        yield f"event: {event}\ndata: {data}\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )

    return router
