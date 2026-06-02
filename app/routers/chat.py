from __future__ import annotations

import json
from typing import Callable

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.schemas import ChatRequest, ChatResponse


def create_chat_router(*, runtime: ClaudeRuntime, require_api_key: Callable) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["chat"], dependencies=[Depends(require_api_key)])

    @router.post(
        "/chat",
        response_model=ChatResponse,
        summary="Run a Claude Agent task and return the full result",
        description="Runs one Claude Agent SDK query using defaults from docker/.env and optional per-request overrides.",
    )
    async def chat(req: ChatRequest) -> ChatResponse:
        return await runtime.run(req)

    @router.post(
        "/chat/stream",
        summary="Run a Claude Agent task as server-sent events",
        description="Streams session, message, result, error, and done events as text/event-stream.",
    )
    async def chat_stream(req: ChatRequest) -> StreamingResponse:
        async def event_stream():
            async for item in runtime.stream(req):
                event = item.get("event", "message")
                data = json.dumps(item.get("data"), ensure_ascii=False)
                yield f"event: {event}\ndata: {data}\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    return router
