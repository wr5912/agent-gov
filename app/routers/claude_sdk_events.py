from __future__ import annotations

import json
from collections.abc import Callable

from fastapi import APIRouter, Depends
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
from app.runtime.settings import AppSettings
from app.runtime.speech_summary import build_speech_summary_envelope
from app.runtime.stores.agent_registry_store import AgentRegistryStore
from app.runtime.stream_request_schemas import ClaudeSdkEventsRequest
from app.sse_contracts import CLAUDE_SDK_EVENTS_PATH, require_registered_sse_event

from .runtime_preflight import require_stream_hitl_available

_CONTROL_EVENT_NAMES = {
    "claude_user_input_required": "agentgov.confirmation.requested",
    "claude_user_input_resolved": "agentgov.confirmation.resolved",
}


def _sse(event_name: str, data: object) -> str:
    require_registered_sse_event(CLAUDE_SDK_EVENTS_PATH, event_name)
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
    async def sdk_events(req: ClaudeSdkEventsRequest) -> StreamingResponse:
        profile = resolve_business_profile(settings, agent_registry_store, req.agent_id)
        require_stream_hitl_available(profile, settings, surface="/api/agent-runtime/sdk-events")

        async def event_stream():
            data_frame_seq = 0
            async for event in runtime.stream_events(
                req,
                profile=profile,
                with_speech_summary=req.with_speech_summary,
            ):
                if isinstance(event, ClaudeSdkMessageEvent):
                    data_frame_seq += 1
                    yield _sse(
                        sdk_message_event_name(event.message),
                        sdk_message_to_json(event.message),
                    )
                    continue
                if isinstance(event, AgentGovHeartbeatEvent):
                    yield f": keepalive run_id={event.run_id} timestamp={event.timestamp}\n\n"
                    continue
                if isinstance(event, AgentGovControlEvent):
                    data_frame_seq += 1
                    if event.name == "speech_summary":
                        yield _sse(
                            "agentgov.speech_summary",
                            build_speech_summary_envelope(event.data, seq=data_frame_seq),
                        )
                        continue
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
