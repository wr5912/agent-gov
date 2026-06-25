from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, status

from app.runtime.agent_profile_resolver import resolve_business_profile
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.errors import BusinessRuleViolation, NotFoundError
from app.runtime.schemas import (
    ChatRequest,
    OpenAIChatCompletionChoice,
    OpenAIChatCompletionRequest,
    OpenAIChatCompletionResponse,
    OpenAIChatMessage,
)
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_registry_store import AgentRegistryStore
from app.runtime.stores.runtime_settings_store import RuntimeSettingsStore


def create_openai_router(
    *,
    settings: AppSettings,
    runtime: ClaudeRuntime,
    agent_registry_store: AgentRegistryStore,
    runtime_settings_store: RuntimeSettingsStore,
    require_api_key: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["openai-compatible"], dependencies=[Depends(require_api_key)])

    @router.post(
        "/chat/completions",
        response_model=OpenAIChatCompletionResponse,
        summary="Run a non-streaming OpenAI-compatible chat completion",
        description="Maps OpenAI-style messages into one Claude Agent prompt. OpenAI requests carry no agent_id; the target agent is operator-configured via /api/settings/openai-compat-agent (defaults to the main agent).",
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
        # /v1 出口 Agent 由运营者配置；未配置 -> main；配置了但已失效 -> fail-loud（不静默回 main）。
        configured_agent_id = runtime_settings_store.get_openai_compat_agent_id()
        try:
            profile = resolve_business_profile(settings, agent_registry_store, configured_agent_id)
        except (NotFoundError, BusinessRuleViolation) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Configured OpenAI-compat agent is unavailable: {exc}. Reconfigure via /api/settings/openai-compat-agent.",
            ) from exc
        result = await runtime.run(chat_req, profile=profile)
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
