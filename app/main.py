from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.routers.agent_governance import create_agent_governance_router
from app.routers.agent_jobs import create_agent_jobs_router
from app.routers.agents import create_agents_router
from app.routers.improvements import create_improvements_router, create_improvement_relations_router
from app.routers.improvement_content import create_improvement_content_router
from app.routers.automation import create_automation_router
from app.routers.assets import create_assets_router
from app.routers.scenario_packs import create_scenario_packs_router
from app.routers.catalog import create_catalog_router
from app.routers.chat import create_chat_router
from app.routers.config import create_config_router
from app.routers.core import create_core_router
from app.routers.error_handlers import register_error_handlers
from app.routers.eval import create_eval_router
from app.routers.feedback_batches import create_feedback_batches_router
from app.routers.feedback_cases import create_feedback_cases_router
from app.routers.feedback_workbench import create_feedback_workbench_router
from app.routers.openai import create_openai_router
from app.routers.optimization import create_optimization_router
from app.routers.regression_assets import create_regression_assets_router
from app.routers.sessions import create_sessions_router
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_profiles import build_profiles
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.logging_config import configure_runtime_logging
from app.runtime.runtime_db import make_session_factory, runtime_db_path_from_data_dir
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import get_settings, runtime_settings_log_message
from app.runtime.stores.agent_registry_store import AgentRegistryStore
from app.runtime.stores.improvement_store import ImprovementStore
from app.runtime.stores.improvement_content_store import ImprovementContentStore
from app.services.improvement_governor_service import ImprovementGovernorService
from app.runtime.stores.automation_policy_store import AutomationPolicyStore
from app.runtime.stores.asset_store import AssetStore
from app.runtime.stores.scenario_pack_store import ScenarioPackStore
from app.runtime.stores.feedback_store import FeedbackStore
from app.services.agent_governance import AgentGovernanceService
from app.services.execution_application import ExecutionApplicationService
from app.version import APP_VERSION

settings = get_settings()
configure_runtime_logging(settings.log_level)
logger = logging.getLogger("uvicorn.error")
session_store = LocalSessionStore(settings.session_dir)
agent_version_store = GitAgentVersionStore(
    repository_dir=settings.agent_git_repository_dir,
    worktrees_dir=settings.agent_git_worktrees_dir,
    releases_dir=settings.agent_release_archives_dir,
    service_provider=settings.agent_git_service_provider,
    service_url=settings.agent_git_service_url,
    service_public_url=settings.agent_git_service_public_url,
    repository_name=settings.agent_git_repository_name,
    git_user_name=settings.agent_git_user_name,
    git_user_email=settings.agent_git_user_email,
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
agent_governance = AgentGovernanceService(
    feedback_store=feedback_store,
    agent_version_store=agent_version_store,
)
agent_registry_store = AgentRegistryStore(make_session_factory(runtime_db_path_from_data_dir(settings.data_dir)))
scenario_pack_store = ScenarioPackStore(make_session_factory(runtime_db_path_from_data_dir(settings.data_dir)))
improvement_store = ImprovementStore(make_session_factory(runtime_db_path_from_data_dir(settings.data_dir)))
improvement_content_store = ImprovementContentStore(make_session_factory(runtime_db_path_from_data_dir(settings.data_dir)))
improvement_governor_service = ImprovementGovernorService(
    improvement_store=improvement_store,
    content_store=improvement_content_store,
    run_profile_json=lambda **kwargs: runtime._run_profile_json(**kwargs),
)
automation_policy_store = AutomationPolicyStore(make_session_factory(runtime_db_path_from_data_dir(settings.data_dir)))
asset_store = AssetStore(make_session_factory(runtime_db_path_from_data_dir(settings.data_dir)))
execution_application = ExecutionApplicationService(
    settings=settings,
    feedback_store=feedback_store,
    agent_version_store=agent_version_store,
    agent_governance=agent_governance,
)
bearer_auth = HTTPBearer(auto_error=False)
api_key_credentials = Security(bearer_auth)


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info(runtime_settings_log_message(settings))
    agent_version_store.ensure_bootstrap()
    agent_registry_store.sync_business_agents(build_profiles(settings))
    yield


app = FastAPI(
    title="AgentGov API",
    version=APP_VERSION,
    description="A thin Dockerized API control plane for Claude Agent SDK / Claude Code configurations.",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    openapi_tags=[
        {"name": "health", "description": "Service status and documentation discovery."},
        {"name": "chat", "description": "Claude Agent task execution endpoints."},
        {"name": "catalog", "description": "Discover configured subagents and skills."},
        {"name": "agents", "description": "List registered business agents (governance objects)."},
        {"name": "improvements", "description": "Improvement items: the event-level governance work unit (v2.7)."},
        {"name": "automation", "description": "Automation policy and stage auto-advance orchestration (v2.7 W2)."},
        {"name": "assets", "description": "Governance asset registry and cross-agent inheritance (v2.7 W3)."},
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


def require_api_key(credentials: HTTPAuthorizationCredentials | None = api_key_credentials) -> None:
    if not settings.api_key:
        return
    if not credentials or credentials.scheme.lower() != "bearer" or credentials.credentials != settings.api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")


app.include_router(create_core_router(settings=settings, app=app, agent_version_store=agent_version_store))
app.include_router(
    create_chat_router(
        runtime=runtime,
        settings=settings,
        agent_registry_store=agent_registry_store,
        require_api_key=require_api_key,
    )
)
app.include_router(create_config_router(settings=settings, require_api_key=require_api_key))
app.include_router(create_catalog_router(settings=settings, require_api_key=require_api_key))
app.include_router(create_openai_router(settings=settings, runtime=runtime, require_api_key=require_api_key))
app.include_router(create_sessions_router(session_store=session_store, require_api_key=require_api_key))
app.include_router(
    create_agent_governance_router(
        agent_governance=agent_governance,
        feedback_store=feedback_store,
        runtime=runtime,
        require_api_key=require_api_key,
    )
)
app.include_router(
    create_agents_router(
        settings=settings,
        agent_registry_store=agent_registry_store,
        feedback_store=feedback_store,
        agent_governance=agent_governance,
        require_api_key=require_api_key,
    )
)
app.include_router(
    create_improvements_router(
        improvement_store=improvement_store,
        require_api_key=require_api_key,
    )
)
app.include_router(
    create_improvement_relations_router(
        improvement_store=improvement_store,
        require_api_key=require_api_key,
    )
)
app.include_router(
    create_improvement_content_router(
        improvement_store=improvement_store,
        content_store=improvement_content_store,
        governor_service=improvement_governor_service,
        require_api_key=require_api_key,
    )
)
app.include_router(
    create_automation_router(
        improvement_store=improvement_store,
        automation_policy_store=automation_policy_store,
        require_api_key=require_api_key,
    )
)
app.include_router(create_assets_router(asset_store=asset_store, require_api_key=require_api_key))
app.include_router(
    create_scenario_packs_router(
        scenario_pack_store=scenario_pack_store, feedback_store=feedback_store, require_api_key=require_api_key
    )
)
app.include_router(create_agent_jobs_router(feedback_store=feedback_store, require_api_key=require_api_key))
app.include_router(create_eval_router(feedback_store=feedback_store, runtime=runtime, require_api_key=require_api_key))
app.include_router(create_regression_assets_router(feedback_store=feedback_store, require_api_key=require_api_key))
app.include_router(create_feedback_cases_router(feedback_store=feedback_store, runtime=runtime, require_api_key=require_api_key))
app.include_router(
    create_feedback_batches_router(
        feedback_store=feedback_store,
        runtime=runtime,
        execution_application=execution_application,
        agent_governance=agent_governance,
        require_api_key=require_api_key,
    )
)
app.include_router(
    create_feedback_workbench_router(
        feedback_store=feedback_store,
        runtime=runtime,
        require_api_key=require_api_key,
    )
)
app.include_router(
    create_optimization_router(
        feedback_store=feedback_store,
        runtime=runtime,
        execution_application=execution_application,
        agent_governance=agent_governance,
        require_api_key=require_api_key,
    )
)
