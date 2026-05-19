from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.runtime.ag_ui import (
    PublishNotificationRequest,
    RunAgentInput,
    notification_event,
    run_input_to_chat_request,
)
from app.runtime.agent_loader import discover_agents, discover_skills
from app.runtime.agent_trace import InMemoryAgentTraceStore
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
agent_trace_store = InMemoryAgentTraceStore()
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
        {"name": "agent-trace", "description": "Independent Agent runtime trace event streams."},
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
        try:
            chat_req = run_input_to_chat_request(req)
            await agent_trace_store.publish(
                req.run_id,
                level="info",
                channel="agent",
                event_type="request",
                title="Agent 输入",
                payload=chat_req.model_dump(exclude_none=True),
            )
            async for item in runtime.stream_ag_ui(
                req,
                trace_sink=lambda runtime_item: _publish_trace_from_runtime_stream_item(req, runtime_item),
            ):
                yield _sse_event(item)
        except asyncio.CancelledError:
            await agent_trace_store.publish(
                req.run_id,
                level="warning",
                channel="workflow",
                event_type="run.cancelled",
                title="Agent 运行已取消",
                content="客户端已断开 AG-UI 请求，运行过程停止。",
            )
            raise
        except Exception as exc:
            await agent_trace_store.publish(
                req.run_id,
                level="error",
                channel="system",
                event_type="run.error",
                title="Agent 运行异常",
                content=f"{exc.__class__.__name__}: {exc}",
            )
            raise

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


@app.get(
    "/api/agent-trace",
    dependencies=[Depends(require_api_key)],
    tags=["agent-trace"],
    summary="Subscribe to the global Agent runtime trace stream",
    description="Streams all Agent runtime observability events. This channel stays open independently from AG-UI runs.",
)
async def agent_trace(
    cursor: int | None = Query(default=None),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    start_seq = _cursor_from_header(cursor, last_event_id)

    async def event_stream():
        last_seen = start_seq
        while True:
            events = await agent_trace_store.wait_for_global_events(last_seen)
            for event in events:
                last_seen = event.stream_seq
                yield _sse_event(
                    event.model_dump(by_alias=True, exclude_none=True),
                    event_id=str(event.stream_seq),
                    event_name="agent_trace",
                )
            if not events:
                yield ": heartbeat\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get(
    "/api/agent-runs/{run_id}/trace",
    dependencies=[Depends(require_api_key)],
    tags=["agent-trace"],
    summary="Subscribe to an independent Agent runtime trace stream",
    description="Streams Agent runtime observability events for one run id. This channel is separate from AG-UI.",
)
async def agent_run_trace(
    run_id: str,
    cursor: int | None = Query(default=None),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    start_seq = _cursor_from_header(cursor, last_event_id)

    async def event_stream():
        last_seen = start_seq
        while True:
            events = await agent_trace_store.wait_for_events(run_id, last_seen)
            for event in events:
                last_seen = event.seq
                yield _sse_event(
                    event.model_dump(by_alias=True, exclude_none=True),
                    event_id=str(event.seq),
                    event_name="agent_trace",
                )
            if not events:
                yield ": heartbeat\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _cursor_from_header(cursor: int | None, last_event_id: str | None) -> int | None:
    if cursor is not None:
        return cursor
    if not last_event_id:
        return None
    try:
        return int(last_event_id)
    except ValueError:
        return None


def _notification_sse_payload(record: NotificationRecord) -> dict[str, object]:
    return notification_event(
        notification_id=record.notification_id,
        name=record.name,
        value=record.value,
        created_at=record.created_at,
    )


async def _publish_trace_from_runtime_stream_item(req: RunAgentInput, item: dict[str, Any]) -> None:
    event_name = item.get("event")
    event_type = event_name if isinstance(event_name, str) and event_name else "message"
    data = item.get("data")
    content: str | None = None
    if event_type == "message" and isinstance(data, dict):
        text = data.get("text")
        if isinstance(text, str) and text:
            content = text
    await agent_trace_store.publish(
        req.run_id,
        level=_runtime_trace_level(event_type, data),
        channel="agent",
        event_type=event_type,
        title=f"runtime.{event_type}",
        content=content,
        payload={
            "event": event_type,
            "data": data,
        },
    )


def _runtime_trace_level(event_type: str, data: Any) -> str:
    if event_type == "error":
        return "error"
    if event_type == "done":
        return "info"
    if isinstance(data, dict):
        errors = data.get("errors")
        if isinstance(errors, list) and errors:
            return "error"
    return "info"


def _trace_from_ag_ui_event(item: dict[str, object]) -> dict[str, Any] | None:
    event_type = item.get("type")
    if event_type == "RUN_STARTED":
        return {
            "level": "info",
            "channel": "workflow",
            "event_type": "run.started",
            "title": "Agent 开始处理",
            "payload": {"threadId": item.get("threadId"), "runId": item.get("runId")},
        }
    if event_type == "RUN_FINISHED":
        return {
            "level": "info",
            "channel": "workflow",
            "event_type": "run.finished",
            "title": "Agent 运行完成",
            "payload": item.get("result"),
        }
    if event_type == "RUN_ERROR":
        return {
            "level": "error",
            "channel": "system",
            "event_type": "run.error",
            "title": "Agent 运行失败",
            "content": str(item.get("message") or "Runtime error"),
            "payload": {"code": item.get("code")},
        }
    if event_type == "TEXT_MESSAGE_START":
        return {
            "level": "info",
            "channel": "model",
            "event_type": "model.output.started",
            "title": "模型开始输出",
            "payload": item,
        }
    if event_type == "TEXT_MESSAGE_CONTENT":
        delta = item.get("delta")
        return {
            "level": "debug",
            "channel": "model",
            "event_type": "model.output.delta",
            "title": "模型输出片段",
            "content": delta if isinstance(delta, str) else None,
            "payload": item,
        }
    if event_type == "TEXT_MESSAGE_END":
        return {
            "level": "info",
            "channel": "model",
            "event_type": "model.output.finished",
            "title": "模型输出结束",
            "payload": item,
        }
    if event_type == "CUSTOM":
        return _trace_from_custom_ag_ui_event(item)
    return None


def _trace_from_custom_ag_ui_event(item: dict[str, object]) -> dict[str, Any]:
    name = item.get("name")
    value = item.get("value")
    if name == "ai_soc.agent.activity" and isinstance(value, dict):
        status = value.get("status")
        tool_name = value.get("toolName")
        title = _string_value(value.get("label")) or _string_value(value.get("kind")) or "Agent 活动"
        detail = _string_value(value.get("detail"))
        return {
            "level": "error" if status == "error" else "info",
            "channel": "tool" if isinstance(tool_name, str) and tool_name else "agent",
            "event_type": f"agent.activity.{status}" if isinstance(status, str) else "agent.activity",
            "title": title,
            "content": detail,
            "payload": value,
        }
    if name == "a2ui.message":
        return {
            "level": "info",
            "channel": "workflow",
            "event_type": "ui.surface.generated",
            "title": "生成可视化片段",
            "payload": value,
        }
    if isinstance(name, str) and name.startswith("ai_soc."):
        return {
            "level": "info",
            "channel": "workflow",
            "event_type": name,
            "title": "推送业务事件",
            "payload": value,
        }
    return {
        "level": "debug",
        "channel": "system",
        "event_type": "custom.event",
        "title": str(name or "自定义事件"),
        "payload": value,
    }


def _string_value(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _sse_event(
    item: dict[str, object],
    event_id: str | None = None,
    event_name: str | None = None,
) -> str:
    event_type = event_name or item.get("type")
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
