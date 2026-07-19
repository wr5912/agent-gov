from __future__ import annotations

import json
from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from app.runtime.agent_profile_resolver import resolve_business_profile
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.schemas import ChatRequest, ChatResponse
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_registry_store import AgentRegistryStore


def _require_agent_id(req: ChatRequest) -> None:
    """两个原生 chat 入口都要求显式有效 agent_id，不使用默认业务 Agent。"""
    if not (req.agent_id and req.agent_id.strip()):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
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
    )
    async def chat(req: ChatRequest) -> ChatResponse:
        _require_agent_id(req)
        profile = resolve_business_profile(settings, agent_registry_store, req.agent_id)
        return await runtime.run(req, profile=profile)

    @router.post(
        "/chat/stream",
        summary="Run a Claude Agent task as server-sent events",
        description="Streams session, message, prompt_suggestion, result, error, and done events as text/event-stream. Requires a registered business agent_id.",
    )
    async def chat_stream(req: ChatRequest) -> StreamingResponse:
        _require_agent_id(req)
        profile = resolve_business_profile(settings, agent_registry_store, req.agent_id)

        async def event_stream():
            async for item in runtime.stream(req, profile=profile):
                event = item.get("event", "message")
                data = json.dumps(item.get("data"), ensure_ascii=False)
                yield f"event: {event}\ndata: {data}\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    return router
