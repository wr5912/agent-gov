from __future__ import annotations

import json
from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from app.runtime.agent_profile_resolver import resolve_business_profile
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.managed_claude_events import (
    AgentGovControlEvent,
    AgentGovHeartbeatEvent,
    ClaudeSdkMessageEvent,
    sdk_message_event_name,
    sdk_message_to_json,
)
from app.runtime.schemas import ChatRequest
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_registry_store import AgentRegistryStore

_CONTROL_EVENT_NAMES = {
    "claude_user_input_required": "agentgov.confirmation.requested",
    "claude_user_input_resolved": "agentgov.confirmation.resolved",
}


def _sse(event_name: str, data: object) -> str:
    return f"event: {event_name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def create_claude_sdk_events_router(
    *,
    runtime: ClaudeRuntime,
    settings: AppSettings,
    agent_registry_store: AgentRegistryStore,
    require_api_key: Callable,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/agent-runtime",
        tags=["claude-sdk-events"],
        dependencies=[Depends(require_api_key)],
    )

    @router.post(
        "/sdk-events",
        summary="Run a managed Claude Agent SDK turn and stream native SDK messages",
        description=(
            "Each official Claude Agent SDK yield is emitted once as claude.sdk.<ClassName> with a "
            "mechanical dataclass-to-JSON payload. AgentGov-owned lifecycle events use agentgov.*. "
            "This contract follows the pinned Claude Agent SDK; it is not a UI-shaped or byte-exact CLI stream."
        ),
    )
    async def sdk_events(req: ChatRequest) -> StreamingResponse:
        if not (req.agent_id and req.agent_id.strip()):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="agent_id is required and must identify a registered business agent",
            )
        profile = resolve_business_profile(settings, agent_registry_store, req.agent_id)

        async def event_stream():
            async for event in runtime.stream_events(req, profile=profile):
                if isinstance(event, ClaudeSdkMessageEvent):
                    yield _sse(
                        sdk_message_event_name(event.message),
                        sdk_message_to_json(event.message),
                    )
                    continue
                if isinstance(event, AgentGovHeartbeatEvent):
                    yield f": keepalive run_id={event.run_id} timestamp={event.timestamp}\n\n"
                    continue
                if isinstance(event, AgentGovControlEvent):
                    event_name = _CONTROL_EVENT_NAMES.get(event.name, f"agentgov.{event.name}")
                    yield _sse(event_name, event.data)
                    continue
                raise TypeError(f"Unsupported managed Claude event: {event.__class__.__name__}")

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )

    return router
