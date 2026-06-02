from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends

from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.schemas import (
    ChatRequest,
    OpenAIChatCompletionChoice,
    OpenAIChatCompletionRequest,
    OpenAIChatCompletionResponse,
    OpenAIChatMessage,
)
from app.runtime.settings import AppSettings


def create_openai_router(
    *,
    settings: AppSettings,
    runtime: ClaudeRuntime,
    require_api_key: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["openai-compatible"], dependencies=[Depends(require_api_key)])

    @router.post(
        "/chat/completions",
        response_model=OpenAIChatCompletionResponse,
        summary="Run a non-streaming OpenAI-compatible chat completion",
        description="Maps OpenAI-style messages into one Claude Agent prompt. Agent-specific controls should use /api/chat.",
    )
    async def openai_chat_completions(req: OpenAIChatCompletionRequest) -> OpenAIChatCompletionResponse:
        prompt_parts = []
        for msg in req.messages:
            prompt_parts.append(f"{msg.role}: {msg.content}")
        chat_req = ChatRequest(
            message="\n".join(prompt_parts),
            model=req.model,
            max_turns=req.max_turns,
            metadata=req.metadata,
        )
        result = await runtime.run(chat_req)
        return OpenAIChatCompletionResponse(
            id=result.session_id,
            model=req.model or settings.agent_model,
            choices=[
                OpenAIChatCompletionChoice(
                    message=OpenAIChatMessage(role="assistant", content=result.answer or "")
                )
            ],
            usage=result.usage,
        )

    return router
