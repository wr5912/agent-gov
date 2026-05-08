from __future__ import annotations

import json
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from app.runtime.agent_loader import discover_agents, discover_skills
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.schemas import AgentInfo, ChatRequest, ChatResponse, SessionInfo, SkillInfo, OpenAIChatCompletionRequest, OpenAIChatCompletionResponse, OpenAIChatCompletionChoice, OpenAIChatMessage
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import get_settings

settings = get_settings()
session_store = LocalSessionStore(settings.session_dir)
runtime = ClaudeRuntime(settings, session_store)

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


def require_api_key(authorization: Annotated[str | None, Header()] = None) -> None:
    if not settings.api_key:
        return
    expected = f"Bearer {settings.api_key}"
    if authorization != expected:
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
        "claude_home": str(settings.claude_home),
        "claude_config_dir": str(settings.resolved_claude_config_dir),
        "model": settings.agent_model,
        "default_agent": settings.default_agent,
        "default_skills_mode": settings.default_skills_mode,
        "provider_api_url_configured": bool(settings.provider_api_url),
        "provider_api_key_configured": bool(settings.provider_api_key),
        "programmatic_agents": settings.enable_programmatic_agents,
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
    description="Runs one Claude Agent SDK query using defaults from .env and optional per-request overrides.",
)
async def chat(req: ChatRequest) -> ChatResponse:
    result = await runtime.run(req)
    return ChatResponse(**result)


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
