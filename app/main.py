from __future__ import annotations

import asyncio
import hmac
import logging
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from scripts.bootstrap_runtime_volume import load_runtime_env

from app.openapi_contract import install_openapi_contract
from app.routers.agent_change_set_regression import create_agent_change_set_regression_router
from app.routers.agent_config_files import create_agent_config_files_router
from app.routers.agent_governance import create_agent_governance_router
from app.routers.agent_jobs import create_agent_jobs_router
from app.routers.agent_workspace_packages import create_agent_workspace_packages_router
from app.routers.agents import create_agents_router
from app.routers.assets import create_assets_router
from app.routers.catalog import create_catalog_router
from app.routers.chat import create_chat_router
from app.routers.claude_user_input import create_claude_user_input_router
from app.routers.config import create_config_router
from app.routers.conversations import create_conversations_router
from app.routers.core import create_core_router, refresh_runtime_dependency_versions
from app.routers.error_handlers import register_error_handlers
from app.routers.eval import create_eval_router
from app.routers.feedback_cases import create_feedback_cases_router
from app.routers.feedback_workbench import create_feedback_workbench_router
from app.routers.improvement_content import create_improvement_content_router
from app.routers.improvement_execution import create_improvement_execution_router
from app.routers.improvement_feedback_ops import create_improvement_feedback_ops_router
from app.routers.improvements import create_improvement_relations_router, create_improvements_router
from app.routers.langfuse_traces import create_langfuse_traces_router
from app.routers.openai import create_openai_router
from app.routers.responses import create_responses_router
from app.routers.sessions import create_sessions_router
from app.routers.settings import create_settings_router
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_job_types import AgentJobType
from app.runtime.agent_profile_resolver import resolve_business_profile
from app.runtime.agent_profiles import agents_requiring_web_hitl, build_profiles, discover_seeded_business_agents, seed_business_agent_ids
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.claude_user_input_service import ClaudeUserInputService
from app.runtime.logging_config import configure_runtime_logging
from app.runtime.runtime_db import make_session_factory, runtime_db_path_from_data_dir
from app.runtime.runtime_recovery import RUNTIME_RECOVERY_INTERVAL_SECONDS
from app.runtime.sdk_session_migration import ensure_sdk_store_ready
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import get_settings, runtime_settings_log_message, validate_hitl_single_api_process
from app.runtime.stores.agent_registry_store import AgentRegistryStore
from app.runtime.stores.asset_store import AssetStore
from app.runtime.stores.claude_user_input_store import ClaudeUserInputStore
from app.runtime.stores.feedback_store import FeedbackStore
from app.runtime.stores.improvement_content_store import ImprovementContentStore
from app.runtime.stores.improvement_store import ImprovementStore
from app.runtime.stores.runtime_settings_store import RuntimeSettingsStore
from app.services.agent_governance import AgentGovernanceService
from app.services.agent_version_maintenance import is_agent_version_maintenance_active
from app.services.improvement_execution_service import ImprovementExecutionService
from app.services.improvement_governor_service import ImprovementGovernorService
from app.services.workspace_execution_applier import WorkspaceExecutionApplier
from app.version import APP_VERSION

settings = get_settings()
runtime_env = dict(load_runtime_env(settings.settings_env_file)) if settings.settings_env_file else dict(os.environ)
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
    agent_version_provider=None,  # #24-C/D：下方装配 per-agent 解析器（依赖 agent_governance._store_for）。
    runtime_version=APP_VERSION,
    enable_debug_evidence=settings.enable_feedback_debug_evidence,
)
runtime_db_session_factory = make_session_factory(runtime_db_path_from_data_dir(settings.data_dir))
claude_user_input_store = ClaudeUserInputStore(runtime_db_session_factory)
claude_user_input_service = ClaudeUserInputService(
    claude_user_input_store,
    timeout_seconds=settings.hitl_timeout_seconds,
)
runtime = ClaudeRuntime(
    settings,
    session_store,
    feedback_store,
    agent_version_store,
    user_input_service=claude_user_input_service,
    runtime_env=runtime_env,
)
feedback_store.set_langfuse_trace_fetcher(runtime.fetch_langfuse_trace)
agent_governance = AgentGovernanceService(
    feedback_store=feedback_store,
    agent_version_store=agent_version_store,
    runtime_mode=settings.runtime_volume_mode,
    runtime_env=runtime_env,
)
agent_registry_store = AgentRegistryStore(runtime_db_session_factory)
runtime.business_profile_resolver = lambda agent_id: resolve_business_profile(settings, agent_registry_store, agent_id)
# 缺陷④：版本治理懒建版本库前校验业务 Agent 已注册，杜绝幽灵 Agent（main-agent 恒有效）。
agent_governance.agent_exists = lambda aid: agent_registry_store.get_agent(aid) is not None
feedback_store.agent_exists = agent_governance.agent_exists
runtime.agent_version_maintenance_provider = lambda agent_id: is_agent_version_maintenance_active(
    session_factory=runtime_db_session_factory,
    store_for=agent_governance._store_for,
    agent_id=agent_id,
)


# #24-C/D：单一 per-agent 版本解析器——复用 agent_governance._store_for 缓存按 agent_id 路由到各业务 Agent
# 自己的 GitAgentVersionStore（repository_dir=该 Agent workspace）。FeedbackStore 版本 stamping 与
# ClaudeRuntime 运行版本归属共用它，杜绝非 main Agent 的版本/基线/执行门落到 main 库（issue #24 C/D）。
def _resolve_agent_version_id(agent_id: Optional[str]) -> Optional[str]:
    return agent_governance._store_for(agent_id or "main-agent").current_version_id()


feedback_store.agent_version_provider = _resolve_agent_version_id
improvement_store = ImprovementStore(runtime_db_session_factory)
improvement_content_store = ImprovementContentStore(runtime_db_session_factory)
improvement_governor_service = ImprovementGovernorService(
    improvement_store=improvement_store,
    content_store=improvement_content_store,
    run_profile_json=lambda **kwargs: runtime._run_profile_json(**kwargs),
    data_dir=settings.data_dir,
    format_normalized_feedback=lambda raw_text: runtime._format_agent_text(
        job_type=str(AgentJobType.NORMALIZED_FEEDBACK), raw_text=raw_text, job_input={"raw_feedback": raw_text}
    ),
    find_run_by_id=lambda run_id: feedback_store.find_run(run_id=run_id),
)
asset_store = AssetStore(runtime_db_session_factory)
runtime_settings_store = RuntimeSettingsStore(runtime_db_session_factory)
execution_application = WorkspaceExecutionApplier()
improvement_execution_service = ImprovementExecutionService(
    improvement_store=improvement_store,
    content_store=improvement_content_store,
    agent_governance=agent_governance,
    execution_app=execution_application,
    run_profile_json=lambda **kwargs: runtime._run_profile_json(**kwargs),
)
bearer_auth = HTTPBearer(auto_error=False)
api_key_credentials = Security(bearer_auth)


def _reconcile_runtime_orphans() -> None:
    reconciled_turn_count = 0
    while batch := session_store.reconcile_expired_turns(limit=100):
        reconciled_turn_count += len(batch)
    if reconciled_turn_count:
        logger.warning("reconciled expired SDK session turns: %s", reconciled_turn_count)
    recovered_provisions = agent_registry_store.recover_incomplete_provisions()
    if recovered_provisions:
        logger.warning("recovered expired business Agent provisions: %s", recovered_provisions)
    orphan_eval_runs = feedback_store.reconcile_orphan_eval_runs()
    if orphan_eval_runs:
        logger.warning("reconciled expired EvalRuns: %s", orphan_eval_runs)
    regression_reconciliation = agent_governance.reconcile_regression_runs()
    if any(regression_reconciliation.values()):
        logger.warning("reconciled interrupted Agent change set regressions: %s", regression_reconciliation)
    release_reconciliation = agent_governance.reconcile_release_operations()
    if any(release_reconciliation.values()):
        logger.warning("reconciled interrupted Agent release rollback/restore operations: %s", release_reconciliation)
    execution_reconciliation = improvement_execution_service.reconcile_expired_executions()
    if any(execution_reconciliation.values()):
        logger.warning("reconciled expired improvement executions: %s", execution_reconciliation)


async def _runtime_orphan_recovery_loop() -> None:
    while True:
        await asyncio.sleep(RUNTIME_RECOVERY_INTERVAL_SECONDS)
        try:
            await asyncio.to_thread(_reconcile_runtime_orphans)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("runtime orphan reconciliation failed")


async def _refresh_model_provider_readiness() -> None:
    summary = await asyncio.to_thread(runtime.model_provider_router.refresh_readiness)
    log = logger.info if summary.get("status") == "ready" else logger.warning
    log(
        "event=model_provider.readiness status=%s error_code=%s reason=%s probe=%s duration_ms=%s action=%s",
        summary.get("status"),
        summary.get("error_code"),
        summary.get("reason"),
        summary.get("probe"),
        summary.get("duration_ms"),
        summary.get("action"),
    )


async def _refresh_runtime_dependency_snapshot() -> None:
    versions = await asyncio.to_thread(refresh_runtime_dependency_versions)
    logger.info(
        "event=runtime.dependencies_refreshed claude_agent_sdk=%s bundled_claude_code_cli=%s",
        versions.claude_agent_sdk,
        versions.bundled_claude_code_cli,
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info(runtime_settings_log_message(settings))
    validate_hitl_single_api_process(settings)
    cancelled = claude_user_input_service.cancel_orphan_waiting_requests(reason="service_restarted")
    if cancelled:
        logger.info("cancelled orphan Claude user-input requests: %s", len(cancelled))
    agent_version_store.ensure_bootstrap()
    # 预制 profile（main-agent + governor）优先，再用磁盘发现补充 seed 预置的其它业务 Agent，
    # 使运行卷 data/business-agents/* 下落盘的多业务 Agent 与 main-agent 走同一注册/路由/治理抽象。
    profiles = build_profiles(settings)
    for profile in discover_seeded_business_agents(settings):
        profiles.setdefault(profile.name, profile)
    # #26：以 seed 目录为准标 origin（seed 声明式基线禁删 / user 可 tombstone 删除）；sync 跳过 tombstone 不复活。
    agent_registry_store.sync_business_agents(profiles, seed_agent_ids=seed_business_agent_ids())
    logger.info(
        "business agent registry synced: %s",
        sorted(agent_id for agent_id, profile in profiles.items() if profile.category == "business"),
    )
    _reconcile_runtime_orphans()
    cleanup_summary = agent_governance.reconcile_worktree_cleanups()
    if cleanup_summary["completed"] or cleanup_summary["failed"]:
        logger.info("worktree cleanup reconciliation: %s", cleanup_summary)
    for session in session_store.list():
        if not session.sdk_session_id or session.sdk_store_ready_at is not None or session.active_run_id:
            continue
        profile = profiles.get(session.agent_id or "")
        if profile is None:
            logger.error("legacy SDK session %s has no resolvable owning Agent", session.session_id)
            continue
        try:
            await ensure_sdk_store_ready(
                session_store,
                session,
                workspace_dir=profile.workspace_dir,
                claude_config_dir=profile.claude_config_dir,
            )
        except Exception as exc:
            # 迁移失败保持 fail closed；请求路径会返回明确的 runtime unavailable，绝不回退本地读取。
            logger.error("legacy SDK session migration failed for %s: %s", session.session_id, exc.__class__.__name__)
    # 原生 project settings 含 ask 的 Agent 在 HITL 关闭时执行能力不可用；运行时统一 fail-loud。
    if not settings.enable_claude_web_hitl:
        requiring = agents_requiring_web_hitl(profiles)
        if requiring:
            logger.warning(
                "业务 Agent %s 的 project settings 含 permissions.ask，但 ENABLE_CLAUDE_WEB_HITL=false："
                "其响应处置执行能力不可用（ask 型工具将被 fail-loud 拒绝），如需执行处置请开启 web HITL。",
                requiring,
            )
    runtime.model_provider_router.mark_readiness_checking()
    provider_probe_task = asyncio.create_task(
        _refresh_model_provider_readiness(),
        name="model-provider-readiness",
    )
    dependency_probe_task = asyncio.create_task(
        _refresh_runtime_dependency_snapshot(),
        name="runtime-dependency-snapshot",
    )
    recovery_task = asyncio.create_task(_runtime_orphan_recovery_loop(), name="runtime-orphan-recovery")
    try:
        yield
    finally:
        for task in (provider_probe_task, dependency_probe_task, recovery_task):
            task.cancel()
        for task in (provider_probe_task, dependency_probe_task, recovery_task):
            with suppress(asyncio.CancelledError):
                await task


app = FastAPI(
    title="AgentGov API",
    version=APP_VERSION,
    description="A thin Dockerized API control plane for Claude Agent SDK / Claude Code configurations.",
    # 文档 UI 资源自托管：默认 docs_url/redoc_url 生成的 HTML 硬依赖公网 CDN
    # （cdn.jsdelivr.net、fonts.googleapis.com），与「必需工作流不得依赖远程服务」
    # 冲突，且内网浏览器取不到时页面永远停在空白。改由下方 _STATIC_DOCS_DIR 提供。
    docs_url=None,
    redoc_url=None,
    openapi_url="/openapi.json",
    openapi_tags=[
        {"name": "health", "description": "Service status and documentation discovery."},
        {"name": "chat", "description": "Claude Agent task execution endpoints."},
        {"name": "catalog", "description": "Discover configured subagents and skills."},
        {"name": "agents", "description": "List registered business agents (governance objects)."},
        {"name": "improvements", "description": "Improvement items: the event-level governance work unit (四阶段改进治理)."},
        {"name": "assets", "description": "Governance asset registry and cross-agent inheritance (四阶段改进治理 W3)."},
        {"name": "config", "description": "Inspect Claude Code configuration mapping inside the container."},
        {"name": "feedback", "description": "Feedback loop, attribution, and optimization proposal endpoints."},
        {"name": "sessions", "description": "List and delete API session mappings."},
        {"name": "openai-compatible", "description": "Minimal non-streaming OpenAI-compatible shim."},
        {"name": "openai-responses", "description": "Canonical OpenAI Responses-first surface (POST /v1/responses, retrieve)."},
        {"name": "openai-conversations", "description": "OpenAI Conversations surface (create/list/get/delete + items, projected from SDK transcript)."},
    ],
    lifespan=lifespan,
    swagger_ui_parameters={"displayRequestDuration": True, "docExpansion": "none"},
)

# 与 app 包同级发布：容器由 Dockerfile 的 `COPY app /app/app` 一并带入，本机调试直接从
# 仓库解析，两种模式路径一致，不需要按 RUNTIME_CONTAINER 分叉。
_STATIC_DOCS_DIR = Path(__file__).resolve().parent / "static" / "docs"
_STATIC_DOCS_MOUNT = "/static/docs"

app.mount(_STATIC_DOCS_MOUNT, StaticFiles(directory=_STATIC_DOCS_DIR), name="static-docs")


@app.get("/docs", include_in_schema=False)
async def swagger_ui_html() -> HTMLResponse:
    """自托管 Swagger UI。资源全部走本服务，离线与内网可用。"""

    return get_swagger_ui_html(
        openapi_url=app.openapi_url or "/openapi.json",
        title=f"{app.title} - Swagger UI",
        oauth2_redirect_url=app.swagger_ui_oauth2_redirect_url,
        swagger_js_url=f"{_STATIC_DOCS_MOUNT}/swagger-ui-bundle.js",
        swagger_css_url=f"{_STATIC_DOCS_MOUNT}/swagger-ui.css",
        swagger_favicon_url=f"{_STATIC_DOCS_MOUNT}/favicon.png",
        swagger_ui_parameters=app.swagger_ui_parameters,
    )


@app.get(app.swagger_ui_oauth2_redirect_url or "/docs/oauth2-redirect", include_in_schema=False)
async def swagger_ui_redirect() -> HTMLResponse:
    from fastapi.openapi.docs import get_swagger_ui_oauth2_redirect_html

    return get_swagger_ui_oauth2_redirect_html()


@app.get("/redoc", include_in_schema=False)
async def redoc_html() -> HTMLResponse:
    """自托管 ReDoc。with_google_fonts=False 去掉 fonts.googleapis.com 依赖。"""

    return get_redoc_html(
        openapi_url=app.openapi_url or "/openapi.json",
        title=f"{app.title} - ReDoc",
        redoc_js_url=f"{_STATIC_DOCS_MOUNT}/redoc.standalone.js",
        redoc_favicon_url=f"{_STATIC_DOCS_MOUNT}/favicon.png",
        with_google_fonts=False,
    )


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[
        "Content-Disposition",
        "X-Agent-Commit-SHA",
        "X-Workspace-Package-SHA256",
        "X-Workspace-Tree-SHA256",
    ],
)

register_error_handlers(app)


def require_api_key(credentials: HTTPAuthorizationCredentials | None = api_key_credentials) -> None:
    if not settings.api_key:
        return
    if not credentials or credentials.scheme.lower() != "bearer" or not hmac.compare_digest(credentials.credentials, settings.api_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")


app.include_router(
    create_core_router(
        settings=settings,
        app=app,
        model_provider_router=runtime.model_provider_router,
    )
)
app.include_router(
    create_chat_router(
        runtime=runtime,
        settings=settings,
        agent_registry_store=agent_registry_store,
        require_api_key=require_api_key,
    )
)
app.include_router(
    create_claude_user_input_router(
        service=claude_user_input_service,
        require_api_key=require_api_key,
    )
)
app.include_router(
    create_config_router(
        settings=settings,
        agent_registry_store=agent_registry_store,
        require_api_key=require_api_key,
    )
)
app.include_router(
    create_agent_config_files_router(
        settings=settings,
        agent_registry_store=agent_registry_store,
        session_store=session_store,
        require_api_key=require_api_key,
        version_maintenance=agent_governance.version_maintenance,
    )
)
app.include_router(
    create_catalog_router(
        settings=settings,
        agent_registry_store=agent_registry_store,
        require_api_key=require_api_key,
    )
)
app.include_router(
    create_openai_router(
        settings=settings,
        runtime=runtime,
        agent_registry_store=agent_registry_store,
        runtime_settings_store=runtime_settings_store,
        require_api_key=require_api_key,
    )
)
app.include_router(
    create_responses_router(
        settings=settings,
        runtime=runtime,
        agent_registry_store=agent_registry_store,
        runtime_settings_store=runtime_settings_store,
        feedback_store=feedback_store,
        require_api_key=require_api_key,
    )
)
app.include_router(
    create_conversations_router(
        session_store=session_store,
        settings=settings,
        agent_registry_store=agent_registry_store,
        require_api_key=require_api_key,
    )
)
app.include_router(
    create_settings_router(
        settings=settings, agent_registry_store=agent_registry_store, runtime_settings_store=runtime_settings_store, require_api_key=require_api_key
    )
)
app.include_router(
    create_sessions_router(session_store=session_store, settings=settings, agent_registry_store=agent_registry_store, require_api_key=require_api_key)
)
app.include_router(
    create_agent_governance_router(
        agent_governance=agent_governance,
        require_api_key=require_api_key,
    )
)
app.include_router(
    create_agent_change_set_regression_router(
        agent_governance=agent_governance,
        runtime=runtime,
        require_api_key=require_api_key,
    )
)
app.include_router(
    create_agents_router(
        settings=settings,
        agent_registry_store=agent_registry_store,
        feedback_store=feedback_store,
        improvement_store=improvement_store,
        agent_governance=agent_governance,
        require_api_key=require_api_key,
    )
)
app.include_router(
    create_agent_workspace_packages_router(
        settings=settings,
        agent_registry_store=agent_registry_store,
        agent_governance=agent_governance,
        session_store=session_store,
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
    create_improvement_execution_router(
        improvement_store=improvement_store,
        content_store=improvement_content_store,
        governor_service=improvement_governor_service,
        execution_service=improvement_execution_service,
        require_api_key=require_api_key,
    )
)
app.include_router(
    create_improvement_feedback_ops_router(
        improvement_store=improvement_store,
        content_store=improvement_content_store,
        feedback_store=feedback_store,
        require_api_key=require_api_key,
    )
)
app.include_router(create_langfuse_traces_router(runtime=runtime, require_api_key=require_api_key))
app.include_router(create_assets_router(asset_store=asset_store, require_api_key=require_api_key))
app.include_router(create_agent_jobs_router(feedback_store=feedback_store, require_api_key=require_api_key))
app.include_router(create_eval_router(feedback_store=feedback_store, runtime=runtime, require_api_key=require_api_key))
app.include_router(create_feedback_cases_router(feedback_store=feedback_store, require_api_key=require_api_key))
app.include_router(
    create_feedback_workbench_router(
        feedback_store=feedback_store,
        improvement_store=improvement_store,
        require_api_key=require_api_key,
    )
)

install_openapi_contract(app)
