from __future__ import annotations

from collections.abc import Callable
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, field_validator

from app.runtime.agent_profile_resolver import resolve_business_profile
from app.runtime.protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_registry_store import AgentRegistryStore
from app.runtime.stores.runtime_settings_store import RuntimeSettingsStore


class OpenAICompatAgentConfig(BaseModel):
    """/v1/chat/completions 的出口 Agent 配置。

    显式区分两个状态：从未配置（``configured=False``，走默认业务 Agent）与运营者显式配置。
    ``effective_agent_id`` 是 /v1 实际运行的业务 Agent。
    """

    agent_id: Optional[str] = None
    configured: bool = False
    effective_agent_id: str = DEFAULT_BUSINESS_AGENT_ID


class OpenAICompatAgentUpdate(BaseModel):
    agent_id: str

    @field_validator("agent_id")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("agent_id must not be blank")
        return value.strip()


def create_settings_router(
    *,
    settings: AppSettings,
    agent_registry_store: AgentRegistryStore,
    runtime_settings_store: RuntimeSettingsStore,
    require_api_key: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["settings"], dependencies=[Depends(require_api_key)])

    def _current() -> OpenAICompatAgentConfig:
        agent_id = runtime_settings_store.get_openai_compat_agent_id()
        return OpenAICompatAgentConfig(
            agent_id=agent_id,
            configured=agent_id is not None,
            effective_agent_id=agent_id or DEFAULT_BUSINESS_AGENT_ID,
        )

    @router.get(
        "/settings/openai-compat-agent",
        response_model=OpenAICompatAgentConfig,
        summary="Get the /v1 出口业务 Agent (configured=False means use the default business Agent)",
    )
    async def get_openai_compat_agent() -> OpenAICompatAgentConfig:
        return _current()

    @router.put(
        "/settings/openai-compat-agent",
        response_model=OpenAICompatAgentConfig,
        summary="Set the /v1 出口业务 Agent (validated: unknown 404, non-business 400)",
    )
    async def set_openai_compat_agent(req: OpenAICompatAgentUpdate) -> OpenAICompatAgentConfig:
        # 强校验：必须是已注册且 runnable 的业务 Agent，否则 404/400。
        resolve_business_profile(settings, agent_registry_store, req.agent_id)
        runtime_settings_store.set_openai_compat_agent_id(req.agent_id)
        return _current()

    @router.delete(
        "/settings/openai-compat-agent",
        response_model=OpenAICompatAgentConfig,
        summary="Reset the /v1 出口业务 Agent to the default business Agent",
    )
    async def reset_openai_compat_agent() -> OpenAICompatAgentConfig:
        runtime_settings_store.clear_openai_compat_agent_id()
        return _current()

    return router
