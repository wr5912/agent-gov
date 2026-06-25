from __future__ import annotations

from typing import Callable, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, field_validator

from app.runtime.agent_profile_resolver import resolve_business_profile
from app.runtime.agent_profiles import MAIN_AGENT_PROFILE
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_registry_store import AgentRegistryStore
from app.runtime.stores.runtime_settings_store import RuntimeSettingsStore


class OpenAICompatAgentConfig(BaseModel):
    """/v1/chat/completions 的出口 Agent 配置。

    显式区分两个状态：从未配置（``configured=False``，走隐式 main 默认）与运营者显式配置（含
    显式选 ``main-agent``）。``effective_agent_id`` 是 /v1 实际运行的 Agent（配置值，或默认 main）。
    """

    agent_id: Optional[str] = None
    configured: bool = False
    effective_agent_id: str = MAIN_AGENT_PROFILE


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
            effective_agent_id=agent_id or MAIN_AGENT_PROFILE,
        )

    @router.get(
        "/settings/openai-compat-agent",
        response_model=OpenAICompatAgentConfig,
        summary="Get the /v1/chat/completions 出口 Agent (configured=False means never set -> default main)",
    )
    async def get_openai_compat_agent() -> OpenAICompatAgentConfig:
        return _current()

    @router.put(
        "/settings/openai-compat-agent",
        response_model=OpenAICompatAgentConfig,
        summary="Set the /v1 出口 Agent (main-agent is an explicit choice; validated: unknown 404, non-business 400)",
    )
    async def set_openai_compat_agent(req: OpenAICompatAgentUpdate) -> OpenAICompatAgentConfig:
        # 强校验：main-agent 或已注册 runnable 业务 Agent，否则 404/400（复用与 /api/chat 同一 resolver）。
        resolve_business_profile(settings, agent_registry_store, req.agent_id)
        runtime_settings_store.set_openai_compat_agent_id(req.agent_id)
        return _current()

    @router.delete(
        "/settings/openai-compat-agent",
        response_model=OpenAICompatAgentConfig,
        summary="Reset the /v1 出口 Agent to unconfigured (back to the implicit main default)",
    )
    async def reset_openai_compat_agent() -> OpenAICompatAgentConfig:
        runtime_settings_store.clear_openai_compat_agent_id()
        return _current()

    return router
