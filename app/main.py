from __future__ import annotations

import asyncio
import json

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.runtime.ag_ui import PublishNotificationRequest, RunAgentInput, notification_event
from app.runtime.agent_loader import discover_agents, discover_skills
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.config_mapping import build_config_mapping
from app.runtime.notification_store import InMemoryNotificationStore, NotificationRecord
from app.runtime.schemas import (
    AgentInfo,
    ChatRequest,
    ChatResponse,
    ConfigMappingResponse,
    OpenAIChatCompletionChoice,
    OpenAIChatCompletionRequest,
    OpenAIChatCompletionResponse,
    OpenAIChatMessage,
    SessionInfo,
    SkillInfo,
)
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import get_settings

settings = get_settings()
session_store = LocalSessionStore(settings.session_dir)
runtime = ClaudeRuntime(settings, session_store)
notification_store = InMemoryNotificationStore()
bearer_auth = HTTPBearer(auto_error=False)

app = FastAPI(
    title="Claude Agent Runtime API",
    version="0.1.0",
    description="A thin Dockerized API control plane for Claude Agent SDK / Claude Code configurations.",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    openapi_tags=[
        {"name": "health", "description": "Service status and documentation discovery."},
        {"name": "chat", "description": "Claude Agent task execution endpoints."},
        {"name": "ag-ui", "description": "AG-UI run and proactive notification event streams."},
        {"name": "catalog", "description": "Discover configured subagents and skills."},
        {"name": "config", "description": "Inspect Claude Code configuration mapping inside the container."},
        {"name": "sessions", "description": "List and delete API session mappings."},
        {"name": "openai-compatible", "description": "Minimal non-streaming OpenAI-compatible shim."},
    ],
    swagger_ui_parameters={"displayRequestDuration": True, "docExpansion": "none"},
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def require_api_key(credentials: HTTPAuthorizationCredentials | None = Security(bearer_auth)) -> None:
    if not settings.api_key:
        return
    if not credentials or credentials.scheme.lower() != "bearer" or credentials.credentials != settings.api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")


@app.get("/", include_in_schema=False)
async def root() -> dict[str, object]:
    return {
        "name": "Claude Agent Runtime API",
        "health": "/health",
        "docs": app.docs_url,
        "redoc": app.redoc_url,
        "openapi": app.openapi_url,
    }


@app.get(
    "/health",
    tags=["health"],
    summary="Check service health and discover API documentation URLs",
)
async def health() -> dict[str, object]:
    return {
        "status": "ok",
        "api_host": settings.api_host,
        "api_port": settings.api_port,
        "host_port": settings.host_port,
        "workspace_dir": str(settings.workspace_dir),
        "data_dir": str(settings.data_dir),
        "claude_root": str(settings.claude_root),
        "claude_home": str(settings.claude_home),
        "claude_config_mode": settings.claude_config_mode,
        "claude_config_dir": str(settings.resolved_claude_config_dir) if settings.resolved_claude_config_dir else None,
        "claude_global_config_file": str(settings.claude_global_config_file),
        "setting_sources_effective": settings.setting_sources,
        "model": settings.agent_model,
        "default_agent": settings.default_agent,
        "default_skills_mode": settings.default_skills_mode,
        "provider_api_url_configured": bool(settings.provider_api_url),
        "provider_api_key_configured": bool(settings.provider_api_key),
        "programmatic_agents": settings.enable_programmatic_agents,
        "langfuse_enabled": settings.langfuse_enabled,
        "langfuse_base_url": settings.langfuse_base_url,
        "langfuse_otel_endpoint_configured": bool(settings.langfuse_otel_endpoint),
        "langfuse_public_key_configured": bool(settings.langfuse_public_key),
        "langfuse_secret_key_configured": bool(settings.langfuse_secret_key),
        "langfuse_otel_signals": settings.langfuse_otel_signals,
        "docs": {
            "swagger": app.docs_url,
            "redoc": app.redoc_url,
            "openapi": app.openapi_url,
        },
    }


@app.post(
    "/api/chat",
    response_model=ChatResponse,
    dependencies=[Depends(require_api_key)],
    tags=["chat"],
    summary="Run a Claude Agent task and return the full result",
    description="Runs one Claude Agent SDK query using defaults from docker/.env and optional per-request overrides.",
)
async def chat(req: ChatRequest) -> ChatResponse:
    result = await runtime.run(req)
    return ChatResponse(**result)


@app.get(
    "/api/config",
    response_model=ConfigMappingResponse,
    dependencies=[Depends(require_api_key)],
    tags=["config"],
    summary="Inspect Claude Code configuration mapping",
    description="Returns path, mount, scope, load, and git-policy metadata without exposing sensitive file contents.",
)
async def config_mapping() -> ConfigMappingResponse:
    return build_config_mapping(settings)


@app.post(
    "/api/chat/stream",
    dependencies=[Depends(require_api_key)],
    tags=["chat"],
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


@app.post(
    "/api/ag-ui",
    dependencies=[Depends(require_api_key)],
    tags=["ag-ui"],
    summary="Run an Agent task as an AG-UI event stream",
    description="Accepts a RunAgentInput-compatible body and streams AG-UI BaseEvent objects as SSE data.",
)
async def ag_ui_run(req: RunAgentInput) -> StreamingResponse:
    async def event_stream():
        async for item in runtime.stream_ag_ui(req):
            yield _sse_event(item)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post(
    "/api/ag-ui/notifications",
    dependencies=[Depends(require_api_key)],
    tags=["ag-ui"],
    summary="Publish an Agent-initiated AG-UI custom notification",
    description="Development and worker-facing endpoint for adding lightweight proactive notifications to the SSE queue.",
)
async def publish_ag_ui_notification(req: PublishNotificationRequest) -> dict[str, object]:
    record = notification_store.publish(
        name=req.name,
        value=req.value,
        notification_id=req.notification_id,
        workspace_id=req.workspace_id,
        user_id=req.user_id,
    )
    return {"ok": True, "event": _notification_sse_payload(record)}


@app.get(
    "/api/ag-ui/notifications",
    dependencies=[Depends(require_api_key)],
    tags=["ag-ui"],
    summary="Subscribe to Agent-initiated AG-UI custom notifications",
    description="Streams lightweight proactive notification events. Use Last-Event-ID or cursor to recover missed items.",
)
async def ag_ui_notifications(
    cursor: str | None = Query(default=None),
    workspace_id: str | None = Query(default=None, alias="workspaceId"),
    user_id: str | None = Query(default=None, alias="userId"),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    start_cursor = cursor or last_event_id

    async def event_stream():
        last_seen = start_cursor
        while True:
            records = notification_store.list_after(
                last_seen,
                workspace_id=workspace_id,
                user_id=user_id,
            )
            for record in records:
                last_seen = record.notification_id
                yield _sse_event(_notification_sse_payload(record), event_id=record.notification_id)
            await asyncio.sleep(15)
            yield ": heartbeat\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _notification_sse_payload(record: NotificationRecord) -> dict[str, object]:
    return notification_event(
        notification_id=record.notification_id,
        name=record.name,
        value=record.value,
        created_at=record.created_at,
    )


def _sse_event(item: dict[str, object], event_id: str | None = None) -> str:
    event_type = item.get("type")
    data = json.dumps(item, ensure_ascii=False)
    lines = []
    if event_id:
        lines.append(f"id: {event_id}")
    if isinstance(event_type, str):
        lines.append(f"event: {event_type}")
    lines.append(f"data: {data}")
    return "\n".join(lines) + "\n\n"


@app.post(
    "/v1/chat/completions",
    response_model=OpenAIChatCompletionResponse,
    dependencies=[Depends(require_api_key)],
    tags=["openai-compatible"],
    summary="Run a non-streaming OpenAI-compatible chat completion",
    description="Maps OpenAI-style messages into one Claude Agent prompt. Agent-specific controls should use /api/chat.",
)
async def openai_chat_completions(req: OpenAIChatCompletionRequest) -> OpenAIChatCompletionResponse:
    # Minimal OpenAI-compatible shim for non-streaming chat.
    # It maps all prior messages into a single prompt and delegates to /api/chat.
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
        id=result["session_id"],
        model=req.model or settings.agent_model,
        choices=[
            OpenAIChatCompletionChoice(
                message=OpenAIChatMessage(role="assistant", content=result.get("answer") or "")
            )
        ],
        usage=result.get("usage"),
    )


@app.get(
    "/api/agents",
    response_model=list[AgentInfo],
    dependencies=[Depends(require_api_key)],
    tags=["catalog"],
    summary="List configured Claude subagents",
)
async def list_agents() -> list[AgentInfo]:
    return [
        AgentInfo(
            name=item["name"],
            path=item["path"],
            description=item.get("description"),
            model=item.get("model"),
            tools=item.get("tools") or [],
            skills=item.get("skills") or [],
        )
        for item in discover_agents(settings.workspace_dir, settings.claude_home)
    ]


@app.get(
    "/api/skills",
    response_model=list[SkillInfo],
    dependencies=[Depends(require_api_key)],
    tags=["catalog"],
    summary="List configured Claude skills",
)
async def list_skills() -> list[SkillInfo]:
    return [
        SkillInfo(
            name=item["name"],
            path=item["path"],
            description=item.get("description"),
        )
        for item in discover_skills(settings.workspace_dir, settings.claude_home)
    ]


@app.get(
    "/api/sessions",
    response_model=list[SessionInfo],
    dependencies=[Depends(require_api_key)],
    tags=["sessions"],
    summary="List API session mappings",
)
async def list_sessions() -> list[SessionInfo]:
    return [SessionInfo(**session.__dict__) for session in session_store.list()]


@app.delete(
    "/api/sessions/{session_id}",
    dependencies=[Depends(require_api_key)],
    tags=["sessions"],
    summary="Delete one API session mapping",
)
async def delete_session(session_id: str) -> dict[str, object]:
    deleted = session_store.delete(session_id)
    return {"deleted": deleted, "session_id": session_id}
