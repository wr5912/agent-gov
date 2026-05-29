from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.routers.agent_versions import create_agent_versions_router
from app.routers.catalog import create_catalog_router
from app.routers.chat import create_chat_router
from app.routers.config import create_config_router
from app.routers.core import create_core_router
from app.routers.eval import create_eval_router
from app.routers.error_handlers import register_error_handlers
from app.routers.feedback_batches import create_feedback_batches_router
from app.routers.feedback_cases import create_feedback_cases_router
from app.routers.feedback_workbench import create_feedback_workbench_router
from app.routers.openai import create_openai_router
from app.routers.optimization import create_optimization_router
from app.routers.sessions import create_sessions_router
from app.services.execution_application import ExecutionApplicationService
from app.runtime.agent_version_store import AgentVersionStore
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.stores.feedback_store import FeedbackStore
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import get_settings
from app.version import APP_VERSION

settings = get_settings()
session_store = LocalSessionStore(settings.session_dir)
agent_version_store = AgentVersionStore(
    versions_dir=settings.agent_versions_dir,
    workspace_dir=settings.main_workspace_dir,
    claude_root=settings.main_claude_root,
)
feedback_store = FeedbackStore(
    data_dir=settings.data_dir,
    workspace_dir=settings.main_workspace_dir,
    agent_version_provider=agent_version_store.current_version_id,
    runtime_version=APP_VERSION,
    enable_debug_evidence=settings.enable_feedback_debug_evidence,
)
runtime = ClaudeRuntime(settings, session_store, feedback_store, agent_version_store)
feedback_store.set_langfuse_trace_fetcher(runtime.fetch_langfuse_trace)
execution_application = ExecutionApplicationService(
    settings=settings,
    feedback_store=feedback_store,
    agent_version_store=agent_version_store,
)
bearer_auth = HTTPBearer(auto_error=False)


@asynccontextmanager
async def lifespan(_: FastAPI):
    agent_version_store.ensure_bootstrap()
    yield


app = FastAPI(
    title="Claude Agent Runtime API",
    version=APP_VERSION,
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
    lifespan=lifespan,
    swagger_ui_parameters={"displayRequestDuration": True, "docExpansion": "none"},
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_error_handlers(app)


def require_api_key(credentials: HTTPAuthorizationCredentials | None = Security(bearer_auth)) -> None:
    if not settings.api_key:
        return
    if not credentials or credentials.scheme.lower() != "bearer" or credentials.credentials != settings.api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")


app.include_router(create_core_router(settings=settings, app=app, agent_version_store=agent_version_store))
app.include_router(create_chat_router(runtime=runtime, require_api_key=require_api_key))
app.include_router(create_config_router(settings=settings, require_api_key=require_api_key))
app.include_router(create_catalog_router(settings=settings, require_api_key=require_api_key))
app.include_router(create_openai_router(settings=settings, runtime=runtime, require_api_key=require_api_key))
app.include_router(create_sessions_router(session_store=session_store, require_api_key=require_api_key))
app.include_router(create_agent_versions_router(agent_version_store=agent_version_store, require_api_key=require_api_key))
app.include_router(create_eval_router(feedback_store=feedback_store, runtime=runtime, require_api_key=require_api_key))
app.include_router(create_feedback_cases_router(feedback_store=feedback_store, runtime=runtime, require_api_key=require_api_key))
app.include_router(
    create_feedback_batches_router(
        feedback_store=feedback_store,
        runtime=runtime,
        execution_application=execution_application,
        require_api_key=require_api_key,
    )
)
app.include_router(
    create_feedback_workbench_router(
        feedback_store=feedback_store,
        require_api_key=require_api_key,
    )
)
app.include_router(
    create_optimization_router(
        feedback_store=feedback_store,
        runtime=runtime,
        execution_application=execution_application,
        require_api_key=require_api_key,
    )
)
