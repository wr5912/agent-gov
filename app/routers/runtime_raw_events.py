from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends
from fastapi.responses import Response, StreamingResponse

from app.runtime.agent_profile_resolver import resolve_business_profile
from app.runtime.runtime_raw_events import (
    RAW_EVENTS_MEDIA_TYPE,
    RuntimeRawEventsBackend,
    RuntimeRawEventsDisabledError,
    RuntimeRawEventsRequest,
    raw_event_response_headers,
)
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_registry_store import AgentRegistryStore

from .runtime_preflight import require_non_stream_hitl_free, require_stream_hitl_available


def create_runtime_raw_events_router(
    *,
    backend: RuntimeRawEventsBackend,
    settings: AppSettings,
    agent_registry_store: AgentRegistryStore,
    require_api_key: Callable,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/debug",
        tags=["debug"],
        dependencies=[Depends(require_api_key)],
    )

    @router.post(
        "/agent-runtime/raw-events",
        response_class=Response,
        summary="Run a managed Agent and return byte-exact native Runtime events",
        description=(
            "Starts a normal managed Agent turn, but returns the selected Runtime's native stdout bytes "
            "without JSON parsing, re-serialization, SSE framing, redaction, or AgentGov control events. "
            "HTTP chunk boundaries are not native event boundaries; concatenate response bytes for the exact stream. "
            "Profile and admission failures before headers use HTTP errors, including 400 for a non-runnable Agent. "
            "After streaming headers are sent, a late Runtime failure terminates the byte stream and cannot be "
            "reframed as an AgentGov JSON/SSE terminal event. "
            "This privileged debug surface is disabled by default and requires API_KEY when enabled."
        ),
    )
    async def runtime_raw_events(req: RuntimeRawEventsRequest) -> Response:
        if not settings.enable_agent_runtime_raw_events:
            raise RuntimeRawEventsDisabledError("Raw Agent Runtime events are disabled; set ENABLE_AGENT_RUNTIME_RAW_EVENTS=true with API_KEY configured")

        profile = resolve_business_profile(settings, agent_registry_store, req.agent_id)
        if req.stream:
            require_stream_hitl_available(profile, settings, surface="/api/debug/agent-runtime/raw-events")
        else:
            require_non_stream_hitl_free(profile, surface="/api/debug/agent-runtime/raw-events")
        prepared = await backend.start(req, profile=profile)
        headers = raw_event_response_headers(prepared.metadata)
        if req.stream:
            return StreamingResponse(
                prepared.iter_bytes(),
                media_type=RAW_EVENTS_MEDIA_TYPE,
                headers=headers,
            )
        try:
            content = await prepared.collect()
        finally:
            await prepared.aclose()
        return Response(
            content=content,
            media_type=RAW_EVENTS_MEDIA_TYPE,
            headers=headers,
        )

    return router
