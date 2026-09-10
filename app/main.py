from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from types import MappingProxyType
from typing import Optional

from fastapi import FastAPI, HTTPException, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles

from app.agent_testing.router import create_agent_testing_router
from app.agent_testing.schedule import AgentTestScheduleService, AgentTestScheduleStore
from app.agent_testing.service import AgentTestingService
from app.agent_testing.store import AgentTestingStore
from app.api_mode import ApiModeGateMiddleware
from app.openapi_contract import install_openapi_contract
from app.routers.agent_config_files import create_agent_config_files_router
from app.routers.agent_governance import create_agent_governance_router
from app.routers.agent_jobs import create_agent_jobs_router
from app.routers.agent_workspace_packages import create_agent_workspace_packages_router
from app.routers.agents import create_agents_router
from app.routers.assets import create_assets_router
from app.routers.catalog import create_catalog_router
from app.routers.config import create_config_router
from app.routers.core import create_core_router
from app.routers.error_handlers import register_error_handlers
from app.routers.feedback_cases import create_feedback_cases_router
from app.routers.feedback_workbench import create_feedback_workbench_router
from app.routers.improvement_content import create_improvement_content_router
from app.routers.improvement_execution import create_improvement_execution_router
from app.routers.improvement_feedback_ops import create_improvement_feedback_ops_router
from app.routers.improvements import create_improvement_relations_router, create_improvements_router
from app.routers.langfuse_traces import create_langfuse_traces_router
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.agent_job_types import AgentJobType
from app.runtime.agent_profiles import build_profiles, discover_business_agents
from app.runtime.integrations.runtime_langfuse import RuntimeLangfuseClient
from app.runtime.logging_config import configure_runtime_logging
from app.runtime.protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from app.runtime.runtime_db import make_session_factory, runtime_db_path_from_data_dir
from app.runtime.runtime_recovery import RUNTIME_RECOVERY_INTERVAL_SECONDS
from app.runtime.settings import get_settings, runtime_settings_log_message
from app.runtime.stores.agent_registry_store import AgentRegistryStore
from app.runtime.stores.asset_store import AssetStore
from app.runtime.stores.feedback_store import FeedbackStore
from app.runtime.stores.improvement_content_store import ImprovementContentStore
from app.runtime.stores.improvement_store import ImprovementStore
from app.runtime_gateway.client import AgentScopeRuntimeClient
from app.runtime_gateway.execution import AgentScopeExecutionService
from app.runtime_gateway.harness_snapshots import PublishedHarnessSnapshotStore
from app.runtime_gateway.provisioning import RuntimeAgentProvisioner, agent_payload_from_workspace
from app.runtime_gateway.router import (
    create_agent_run_router,
    create_internal_runtime_router,
    create_runtime_router,
    reconcile_runtime_gateway,
)
from app.runtime_gateway.store import RuntimeRunStore
from app.runtime_gateway.trace_reconciliation import reconcile_pending_traces
from app.services.agent_governance import AgentGovernanceService
from app.services.improvement_execution_service import ImprovementExecutionService
from app.services.improvement_governor_service import ImprovementGovernorService
from app.services.runtime_agent_deletion import RuntimeAgentDeletionService
from app.services.workspace_execution_applier import WorkspaceExecutionApplier
from app.version import APP_VERSION

settings = get_settings()
# Provider 与 MCP 凭据只属于 AgentScope Runtime；API 不读取或保留 Runtime env。
runtime_env = MappingProxyType({})
configure_runtime_logging(settings.log_level)
logger = logging.getLogger("uvicorn.error")

runtime_db_session_factory = make_session_factory(runtime_db_path_from_data_dir(settings.data_dir))
runtime_client = AgentScopeRuntimeClient(
    settings.agentscope_runtime_url,
    user_id=settings.agentscope_runtime_user_id,
    shared_secret=settings.runtime_shared_secret,
    timeout_seconds=settings.runtime_request_timeout_seconds,
)
run_store = RuntimeRunStore(runtime_db_session_factory)
langfuse_client = RuntimeLangfuseClient(settings)
harness_snapshots = PublishedHarnessSnapshotStore(settings.runtime_candidates_dir)

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
    workspace_dir=settings.default_workspace_dir,
    agent_version_provider=None,
    runtime_version=APP_VERSION,
    enable_debug_evidence=settings.enable_feedback_debug_evidence,
)
feedback_store.set_langfuse_trace_fetcher(langfuse_client.fetch_trace)
agent_governance = AgentGovernanceService(
    feedback_store=feedback_store,
    agent_version_store=agent_version_store,
    runtime_mode=settings.runtime_volume_mode,
    runtime_env=runtime_env,
)
agent_registry_store = AgentRegistryStore(runtime_db_session_factory)
agent_registry_store.deletion_pending = run_store.agent_deletion_pending
agent_governance.agent_exists = lambda agent_id: agent_registry_store.get_agent(agent_id) is not None
feedback_store.agent_exists = agent_governance.agent_exists


def _resolve_agent_version_id(agent_id: Optional[str]) -> Optional[str]:
    return agent_governance._store_for(agent_id or DEFAULT_BUSINESS_AGENT_ID).current_version_id()


feedback_store.agent_version_provider = _resolve_agent_version_id
provisioner = RuntimeAgentProvisioner(
    client=runtime_client,
    store=run_store,
    registry=agent_registry_store,
    version_store_for=agent_governance._store_for,
    read_version_store_for=agent_governance._store_for_read_only,
    snapshot_store=harness_snapshots,
)
runtime_execution = AgentScopeExecutionService(
    settings=settings,
    client=runtime_client,
    store=run_store,
    version_store_for=agent_governance._store_for,
    snapshot_store=harness_snapshots,
)

improvement_store = ImprovementStore(runtime_db_session_factory)
improvement_content_store = ImprovementContentStore(runtime_db_session_factory)
improvement_governor_service = ImprovementGovernorService(
    improvement_store=improvement_store,
    content_store=improvement_content_store,
    run_profile_json=runtime_execution.run_profile_json,
    data_dir=settings.data_dir,
    format_normalized_feedback=lambda raw_text: runtime_execution.format_agent_text(
        job_type=str(AgentJobType.NORMALIZED_FEEDBACK),
        raw_text=raw_text,
        job_input={"raw_feedback": raw_text},
    ),
    find_run_by_id=lambda run_id: feedback_store.find_run(run_id=run_id),
)
asset_store = AssetStore(runtime_db_session_factory)
agent_testing_store = AgentTestingStore(runtime_db_session_factory)
agent_test_schedule_store = AgentTestScheduleStore(runtime_db_session_factory)
improvement_execution_service = ImprovementExecutionService(
    improvement_store=improvement_store,
    content_store=improvement_content_store,
    agent_governance=agent_governance,
    execution_app=WorkspaceExecutionApplier(),
    run_profile_json=runtime_execution.run_profile_json,
)
agent_testing_service = AgentTestingService(
    store=agent_testing_store,
    store_for=agent_governance._store_for,
    agent_exists=lambda agent_id: agent_registry_store.get_agent(agent_id) is not None,
    get_change_set=agent_governance.get_change_set,
    run_candidate=runtime_execution.run_candidate,
    release_candidate=runtime_execution.release_candidate,
    artifacts_dir=settings.data_dir / ".agent-testing",
    api_base_url=f"http://127.0.0.1:{settings.api_port}",
    api_key=settings.api_key,
    run_timeout_seconds=settings.agent_test_run_timeout_seconds,
    list_agents=agent_registry_store.list_agents,
    schedule_reader=agent_test_schedule_store.get_schedule,
    schedule_list_reader=agent_test_schedule_store.schedules_for_agents,
)
agent_test_schedule_service = AgentTestScheduleService(
    store=agent_test_schedule_store,
    testing=agent_testing_service,
    agent_exists=lambda agent_id: agent_registry_store.get_agent(agent_id) is not None,
    agent_status=lambda agent_id: getattr(agent_registry_store.get_agent(agent_id), "status", None),
)
agent_governance.latest_passed_test_run = lambda agent_id, commit_sha: agent_testing_service.latest_passed_for_commit(
    agent_id=agent_id,
    commit_sha=commit_sha,
)
runtime_agent_deletion = RuntimeAgentDeletionService(
    client=runtime_client,
    store=run_store,
    registry=agent_registry_store,
    snapshots=harness_snapshots,
    data_dir=settings.data_dir,
    evict_agent_store=agent_governance.evict_agent_store,
)


def _sync_business_agent_profiles() -> list[str]:
    profiles = build_profiles(settings)
    for profile in discover_business_agents(settings):
        try:
            agent_payload_from_workspace(profile.workspace_dir, display_name=profile.name)
        except Exception as exc:
            logger.warning(
                "skipped invalid AgentScope Harness during registry sync: agent_id=%s error=%s",
                profile.agent_id,
                exc,
            )
            continue
        profiles.setdefault(profile.name, profile)
    agent_registry_store.sync_business_agents(profiles)
    return sorted(agent_id for agent_id, profile in profiles.items() if profile.category == "business")


def _recover_agent_provisions() -> None:
    recovered_provisions = agent_registry_store.recover_incomplete_provisions()
    if recovered_provisions:
        logger.warning("recovered expired business Agent provisions: %s", recovered_provisions)


def _reconcile_governance_state() -> None:
    release_reconciliation = agent_governance.reconcile_release_operations()
    if any(release_reconciliation.values()):
        logger.warning("reconciled interrupted Agent release operations: %s", release_reconciliation)
    execution_reconciliation = improvement_execution_service.reconcile_expired_executions()
    if any(execution_reconciliation.values()):
        logger.warning("reconciled expired improvement executions: %s", execution_reconciliation)
    cleanup_reconciliation = agent_governance.reconcile_worktree_cleanups()
    if cleanup_reconciliation["completed"] or cleanup_reconciliation["failed"]:
        logger.info("worktree cleanup reconciliation: %s", cleanup_reconciliation)


async def _reconcile_runtime_gateway_state(*, include_fresh_intents: bool = False) -> None:
    report = await reconcile_runtime_gateway(
        client=runtime_client,
        store=run_store,
        include_fresh_intents=include_fresh_intents,
    )
    if any((report.session_intents_cleaned, report.session_intents_bound, report.runs_finalized, report.failures)):
        logger.warning("AgentScope Runtime gateway reconciliation: %s", report)


async def _recover_runtime_agent_deletions() -> None:
    for intent in run_store.recoverable_agent_deletions():
        try:
            with agent_governance.version_maintenance.lease(
                agent_id=intent.agent_id,
                kind="agent_delete_recovery",
                owner_id=f"recovery:{intent.intent_id}",
            ) as lease:
                result = await runtime_agent_deletion.resume(
                    intent.intent_id,
                    assert_maintenance_active=lease.assert_active,
                )
            if not result.cleanup_complete:
                logger.warning("AgentScope deletion cleanup remains pending: intent_id=%s", intent.intent_id)
        except Exception as exc:
            logger.warning(
                "AgentScope deletion recovery deferred: intent_id=%s error_type=%s",
                intent.intent_id,
                type(exc).__name__,
            )


async def _recover_ephemeral_runtime_resources(*, include_ready: bool) -> None:
    cleaned = await runtime_execution.reconcile_ephemeral_resources(include_ready=include_ready)
    if cleaned:
        logger.warning("reconciled ephemeral AgentScope resources: %s", cleaned)


async def _reconcile_runtime_traces() -> None:
    """在后台查询 Langfuse，不让临时不可用阻塞 API lifespan。"""

    try:
        report = await asyncio.to_thread(
            reconcile_pending_traces,
            store=run_store,
            trace_fetcher=langfuse_client.fetch_trace,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "AgentScope Trace reconciliation deferred: error_type=%s",
            type(exc).__name__,
        )
        return
    if report.completed or report.incomplete or report.failures:
        logger.warning("AgentScope Trace reconciliation: %s", report)


async def _governance_recovery_loop() -> None:
    while True:
        await asyncio.sleep(RUNTIME_RECOVERY_INTERVAL_SECONDS)
        try:
            await asyncio.to_thread(_recover_agent_provisions)
            await asyncio.to_thread(_reconcile_governance_state)
            await _reconcile_runtime_gateway_state()
            await _reconcile_runtime_traces()
            await _recover_ephemeral_runtime_resources(include_ready=False)
            await _recover_runtime_agent_deletions()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("governance state reconciliation failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info(runtime_settings_log_message(settings))
    _recover_agent_provisions()
    registered_agents = _sync_business_agent_profiles()
    logger.info("business agent registry synced: %s", registered_agents)
    await _reconcile_runtime_gateway_state(include_fresh_intents=True)
    interrupted_runs = run_store.reconcile_after_restart()
    if interrupted_runs:
        logger.warning("reconciled interrupted AgentScope runs: %s", len(interrupted_runs))
    await _recover_ephemeral_runtime_resources(include_ready=True)
    await _recover_runtime_agent_deletions()
    _reconcile_governance_state()
    test_recovery = agent_testing_service.recover()
    if any(test_recovery.values()):
        logger.warning("recovered Agent test runs: %s", test_recovery)
    schedule_recovery = agent_test_schedule_service.recover()
    if any(schedule_recovery.values()):
        logger.info("recovered Agent test schedule events: %s", schedule_recovery)

    initial_trace_reconciliation_task = asyncio.create_task(
        _reconcile_runtime_traces(),
        name="initial-trace-reconciliation",
    )
    governance_recovery_task = asyncio.create_task(_governance_recovery_loop(), name="governance-state-recovery")
    test_schedule_task = asyncio.create_task(agent_test_schedule_service.run_forever(), name="agent-test-scheduler")
    try:
        yield
    finally:
        for task in (initial_trace_reconciliation_task, governance_recovery_task, test_schedule_task):
            task.cancel()
        for task in (initial_trace_reconciliation_task, governance_recovery_task, test_schedule_task):
            with suppress(asyncio.CancelledError):
                await task
        await agent_testing_service.aclose()
        await runtime_execution.close()
        await runtime_client.close()


app = FastAPI(
    title="AgentGov API",
    version=APP_VERSION,
    description="AgentGov control plane backed exclusively by AgentScope Runtime.",
    docs_url=None,
    redoc_url=None,
    openapi_url="/openapi.json",
    openapi_tags=[
        {"name": "health", "description": "Service and AgentScope Runtime health."},
        {"name": "runtime", "description": "AgentScope-native Session, Chat, Message, Status, and SSE endpoints."},
        {"name": "agent-runs", "description": "AgentGov run lifecycle, feedback, and trace references."},
        {"name": "catalog", "description": "Discover governed subagents and skills."},
        {"name": "agents", "description": "Registered business agents and governed Workspace packages."},
        {"name": "config", "description": "Inspect and edit AgentScope Harness assets."},
        {"name": "feedback", "description": "Feedback, attribution, and governance workflow endpoints."},
        {"name": "improvements", "description": "Controlled Agent Harness improvement lifecycle."},
        {"name": "assets", "description": "Governance asset registry and cross-agent inheritance."},
        {"name": "traces", "description": "Read-only Langfuse trace lookup by OTel trace_id."},
    ],
    lifespan=lifespan,
    swagger_ui_parameters={"displayRequestDuration": True, "docExpansion": "none"},
)

_STATIC_DOCS_DIR = Path(__file__).resolve().parent / "static" / "docs"
_STATIC_DOCS_MOUNT = "/static/docs"
app.mount(_STATIC_DOCS_MOUNT, StaticFiles(directory=_STATIC_DOCS_DIR), name="static-docs")


@app.get("/docs", include_in_schema=False)
async def swagger_ui_html() -> HTMLResponse:
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
        "X-AgentGov-Run-Id",
        "X-AgentGov-Session-Id",
    ],
)
app.add_middleware(
    ApiModeGateMiddleware,
    mode=settings.api_mode,
    acceptance_identity=settings.acceptance_identity,
    acceptance_api_key=settings.acceptance_api_key,
    state_file=settings.api_gate_state_file,
)

register_error_handlers(app)
bearer_auth = HTTPBearer(auto_error=False)
api_key_credentials = Security(bearer_auth)


def require_api_key(credentials: HTTPAuthorizationCredentials | None = api_key_credentials) -> None:
    if not settings.api_key:
        return
    if not credentials or credentials.scheme.lower() != "bearer" or not hmac.compare_digest(credentials.credentials, settings.api_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")


app.include_router(create_core_router(settings=settings, app=app, runtime_client=runtime_client))
app.include_router(
    create_runtime_router(
        client=runtime_client,
        store=run_store,
        provisioner=provisioner,
        model_type=settings.agentscope_model_type,
        credential_id=settings.agentscope_credential_id,
        model_name=settings.agentscope_model_name,
        model_parameters=settings.agentscope_model_parameters,
        require_api_key=require_api_key,
    )
)
app.include_router(
    create_agent_run_router(
        client=runtime_client,
        store=run_store,
        trace_fetcher=langfuse_client.fetch_trace,
        authorize_run=provisioner.authorize_run,
        require_api_key=require_api_key,
    )
)
app.include_router(create_internal_runtime_router(store=run_store, shared_secret=settings.runtime_shared_secret))
app.include_router(create_config_router(settings=settings, agent_registry_store=agent_registry_store, require_api_key=require_api_key))
app.include_router(
    create_agent_config_files_router(
        settings=settings,
        agent_registry_store=agent_registry_store,
        require_api_key=require_api_key,
        version_maintenance=agent_governance.version_maintenance,
    )
)
app.include_router(create_catalog_router(settings=settings, agent_registry_store=agent_registry_store, require_api_key=require_api_key))
app.include_router(create_agent_governance_router(agent_governance=agent_governance, require_api_key=require_api_key))
app.include_router(
    create_agents_router(
        settings=settings,
        agent_registry_store=agent_registry_store,
        feedback_store=feedback_store,
        improvement_store=improvement_store,
        agent_governance=agent_governance,
        agent_testing_store=agent_testing_store,
        runtime_deletion=runtime_agent_deletion,
        agent_test_schedule_service=agent_test_schedule_service,
        require_api_key=require_api_key,
        runtime_provisioner=provisioner,
    )
)
app.include_router(
    create_agent_workspace_packages_router(
        settings=settings,
        agent_registry_store=agent_registry_store,
        agent_governance=agent_governance,
        run_store=run_store,
        agent_testing=agent_testing_service,
        require_api_key=require_api_key,
    )
)
app.include_router(
    create_agent_testing_router(
        service=agent_testing_service,
        schedule_service=agent_test_schedule_service,
        require_api_key=require_api_key,
    )
)
app.include_router(create_improvements_router(improvement_store=improvement_store, require_api_key=require_api_key))
app.include_router(create_improvement_relations_router(improvement_store=improvement_store, require_api_key=require_api_key))
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
        agent_testing=agent_testing_service,
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
app.include_router(create_langfuse_traces_router(client=langfuse_client, require_api_key=require_api_key))
app.include_router(create_assets_router(asset_store=asset_store, require_api_key=require_api_key))
app.include_router(create_agent_jobs_router(feedback_store=feedback_store, require_api_key=require_api_key))
app.include_router(create_feedback_cases_router(feedback_store=feedback_store, require_api_key=require_api_key))
app.include_router(
    create_feedback_workbench_router(
        feedback_store=feedback_store,
        improvement_store=improvement_store,
        require_api_key=require_api_key,
    )
)

install_openapi_contract(app)
