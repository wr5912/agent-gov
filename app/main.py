from __future__ import annotations

import json
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.runtime.agent_loader import discover_agents, discover_skills
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.config_mapping import build_config_mapping
from app.runtime.feedback_store import FeedbackStore
from app.runtime.schemas import (
    AgentInfo,
    ChatRequest,
    ChatResponse,
    ConfigMappingResponse,
    FeedbackCreateRequest,
    FeedbackEventIngestRequest,
    FeedbackEventIngestResponse,
    FeedbackQueryResponse,
    FeedbackResponse,
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
feedback_store = FeedbackStore(settings.feedback_dir, settings.optimization_proposals_dir)
runtime = ClaudeRuntime(settings, session_store, feedback_store)
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
        {"name": "catalog", "description": "Discover configured subagents and skills."},
        {"name": "config", "description": "Inspect Claude Code configuration mapping inside the container."},
        {"name": "feedback", "description": "Feedback loop, attribution, and optimization proposal endpoints."},
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
    "/api/feedback",
    response_model=FeedbackResponse,
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Create feedback for one Agent run",
)
async def create_feedback(req: FeedbackCreateRequest) -> FeedbackResponse:
    return FeedbackResponse(**feedback_store.create_feedback(req))


@app.post(
    "/api/feedback/events",
    response_model=FeedbackEventIngestResponse,
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Ingest one SOC workflow feedback event",
)
async def ingest_feedback_event(req: FeedbackEventIngestRequest) -> FeedbackEventIngestResponse:
    return FeedbackEventIngestResponse(**feedback_store.ingest_event(req))


@app.get(
    "/api/feedback",
    response_model=FeedbackQueryResponse,
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Query feedback records and attributions",
)
async def list_feedback(
    run_id: str | None = None,
    session_id: str | None = None,
    alert_id: str | None = None,
    case_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> FeedbackQueryResponse:
    return FeedbackQueryResponse(
        **feedback_store.query(
            run_id=run_id,
            session_id=session_id,
            alert_id=alert_id,
            case_id=case_id,
            limit=limit,
        )
    )


@app.get(
    "/api/optimization-proposals",
    response_model=list[dict[str, Any]],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="List pending feedback-driven optimization proposals",
)
async def list_optimization_proposals(
    run_id: str | None = None,
    session_id: str | None = None,
    alert_id: str | None = None,
    case_id: str | None = None,
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    return feedback_store.list_proposals(
        run_id=run_id,
        session_id=session_id,
        alert_id=alert_id,
        case_id=case_id,
        status=status,
        limit=limit,
    )


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
