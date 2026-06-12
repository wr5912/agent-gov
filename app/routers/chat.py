from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.runtime.agent_profiles import (
    MAIN_AGENT_PROFILE,
    AgentRuntimeProfile,
    build_business_agent_profile,
)
from app.runtime.business_agent_workspace import initialize_business_agent_workspace
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.errors import BusinessRuleViolation, NotFoundError
from app.runtime.schemas import ChatRequest, ChatResponse
from app.runtime.settings import AppSettings
from app.runtime.state_machines import AGENT_RUNNABLE_LIFECYCLE_STATES
from app.runtime.stores.agent_registry_store import AgentRegistryStore


def create_chat_router(
    *,
    runtime: ClaudeRuntime,
    settings: AppSettings,
    agent_registry_store: AgentRegistryStore,
    require_api_key: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["chat"], dependencies=[Depends(require_api_key)])

    def _resolve_business_profile(req: ChatRequest) -> Optional[AgentRuntimeProfile]:
        """把 req.agent_id 解析为业务 Agent profile；缺省或 main 时返回 None（运行时走 main agent）。

        只有已注册的业务 Agent 可被运行：未知 agent_id -> 404，治理 Agent -> 400（治理 Agent
        不经此用户入口运行）。运行前确保配置容器存在（幂等）。
        """
        agent_id = req.agent_id
        if not agent_id or agent_id == MAIN_AGENT_PROFILE:
            return None
        agent = agent_registry_store.get_agent(agent_id)
        if agent is None:
            raise NotFoundError(f"Business agent not found: {agent_id}")
        if agent.category != "business":
            raise BusinessRuleViolation(f"Agent is not a runnable business agent: {agent_id}")
        # AGV-020 criterion 3：archived/deprecated/draft 等非活跃 Agent 不参与新运行（仍可审计）。
        if agent.status not in AGENT_RUNNABLE_LIFECYCLE_STATES:
            raise BusinessRuleViolation(f"Agent {agent_id} is {agent.status}; not available for new runs")
        workspace = Path(agent.workspace_dir)
        initialize_business_agent_workspace(workspace, agent_id=agent.agent_id, name=agent.name)
        return build_business_agent_profile(settings, agent_id=agent.agent_id, workspace_dir=workspace)

    @router.post(
        "/chat",
        response_model=ChatResponse,
        summary="Run a Claude Agent task and return the full result",
        description="Runs one Claude Agent SDK query. Set agent_id to run a registered business agent; omit it to run the main agent.",
    )
    async def chat(req: ChatRequest) -> ChatResponse:
        profile = _resolve_business_profile(req)
        return await runtime.run(req, profile=profile)

    @router.post(
        "/chat/stream",
        summary="Run a Claude Agent task as server-sent events",
        description="Streams session, message, result, error, and done events as text/event-stream. Runs the main agent; for business agents use POST /chat.",
    )
    async def chat_stream(req: ChatRequest) -> StreamingResponse:
        async def event_stream():
            async for item in runtime.stream(req):
                event = item.get("event", "message")
                data = json.dumps(item.get("data"), ensure_ascii=False)
                yield f"event: {event}\ndata: {data}\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    return router
