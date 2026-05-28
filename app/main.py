from __future__ import annotations

import json
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.runtime.agent_version_store import AgentVersionStore
from app.runtime.agent_loader import discover_agents, discover_skills
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.config_mapping import build_config_mapping
from app.runtime.feedback_store import FeedbackStore
from app.runtime.schemas import (
    AgentInfo,
    AgentVersionRestoreRequest,
    AgentVersionRestoreResponse,
    AgentVersionSnapshotRequest,
    ChatRequest,
    ChatResponse,
    ConfigMappingResponse,
    AgentRunResponse,
    EvidencePackageFileResponse,
    EvidencePackageResponse,
    ExternalGovernanceItemResponse,
    ExternalGovernanceNotifyRequest,
    ExternalGovernanceWebhookResponse,
    FeedbackAnalysisJobResponse,
    FeedbackCaseCreateRequest,
    FeedbackCaseResponse,
    FeedbackEvalCaseGenerateRequest,
    FeedbackEvalDatasetSyncRequest,
    FeedbackEvalCaseUpdateRequest,
    FeedbackEvalRunCreateRequest,
    FeedbackOptimizationBatchAttributionRequest,
    FeedbackOptimizationBatchCreateRequest,
    FeedbackOptimizationBatchPlanGenerateRequest,
    FeedbackOptimizationBatchPlanReviewRequest,
    FeedbackOptimizationPlanTaskExecuteRequest,
    FeedbackProposalRegenerateRequest,
    FeedbackSignalCreateRequest,
    FeedbackSignalResponse,
    FeedbackSourceUpdateRequest,
    OpenAIChatCompletionChoice,
    OpenAIChatCompletionRequest,
    OpenAIChatCompletionResponse,
    OpenAIChatMessage,
    OptimizationProposalReviewRequest,
    OptimizationProposalReviewResponse,
    OptimizationExecutionApplyRequest,
    OptimizationExecutionCreateRequest,
    OptimizationTaskCreateRequest,
    OptimizationTaskMarkAppliedRequest,
    OptimizationTaskResponse,
    PendingCorrelationResolveRequest,
    SessionInfo,
    SkillInfo,
    SocEventIngestRequest,
    SocEventIngestResponse,
)
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import get_settings

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
    runtime_version="0.2.5",
    enable_debug_evidence=settings.enable_feedback_debug_evidence,
)
runtime = ClaudeRuntime(settings, session_store, feedback_store, agent_version_store)
feedback_store.set_langfuse_trace_fetcher(runtime.fetch_langfuse_trace)
bearer_auth = HTTPBearer(auto_error=False)
MAX_EXECUTION_WRITE_BYTES = 500_000


@asynccontextmanager
async def lifespan(_: FastAPI):
    agent_version_store.ensure_bootstrap()
    yield


app = FastAPI(
    title="Claude Agent Runtime API",
    version="0.2.5",
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


def require_api_key(credentials: HTTPAuthorizationCredentials | None = Security(bearer_auth)) -> None:
    if not settings.api_key:
        return
    if not credentials or credentials.scheme.lower() != "bearer" or credentials.credentials != settings.api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")


def _apply_execution_operations(operations: list[Any]) -> None:
    if not operations:
        raise ValueError("Execution plan has no operations")
    originals: dict[Path, bytes | None] = {}
    writes: list[tuple[Path, bytes]] = []
    for item in operations:
        if not isinstance(item, dict):
            raise ValueError("Execution operation must be an object")
        op = str(item.get("operation") or "")
        target_path = str(item.get("path") or "")
        dest = _safe_workspace_target(target_path)
        if not feedback_store.target_allowed(target_path):
            raise ValueError(f"Target path is not allowed: {target_path}")
        if op == "noop":
            continue
        if dest not in originals:
            originals[dest] = dest.read_bytes() if dest.exists() else None
        expected_sha = str(item.get("expected_sha256") or "").strip()
        if op in {"append_text", "replace_file"} and not expected_sha:
            raise ValueError(f"{op} operation requires expected_sha256: {target_path}")
        if expected_sha and originals[dest] is not None and hashlib.sha256(originals[dest] or b"").hexdigest() != expected_sha:
            raise ValueError(f"Target file changed before apply: {target_path}")
        if op == "append_text":
            append_text = item.get("append_text")
            if not isinstance(append_text, str):
                raise ValueError(f"append_text operation requires append_text: {target_path}")
            before = originals[dest]
            if before is None:
                raise ValueError(f"append_text target does not exist: {target_path}")
            try:
                data = (before.decode("utf-8") + append_text).encode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"append_text target is not UTF-8 text: {target_path}") from exc
        elif op in {"replace_file", "create_file"}:
            content = item.get("content")
            if not isinstance(content, str):
                raise ValueError(f"{op} operation requires content: {target_path}")
            if op == "create_file" and originals[dest] is not None:
                raise ValueError(f"create_file target already exists: {target_path}")
            data = content.encode("utf-8")
        else:
            raise ValueError(f"Unsupported operation: {op}")
        if len(data) > MAX_EXECUTION_WRITE_BYTES:
            raise ValueError(f"Execution write exceeds {MAX_EXECUTION_WRITE_BYTES} bytes: {target_path}")
        writes.append((dest, data))
    if not writes:
        raise ValueError("Execution plan has no writable operations")
    try:
        for dest, data in writes:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
    except Exception as exc:
        for dest, data in originals.items():
            if data is None:
                dest.unlink(missing_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
        raise ValueError(f"Execution apply failed and was rolled back: {exc}") from exc


def _safe_workspace_target(target_path: str) -> Path:
    if not target_path:
        raise ValueError("Target path is required")
    rel = Path(target_path)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"Unsafe target path: {target_path}")
    base = settings.main_workspace_dir.resolve()
    dest = (base / rel).resolve()
    if base != dest and base not in dest.parents:
        raise ValueError(f"Target path escapes main workspace: {target_path}")
    return dest


def _apply_ready_execution_job(task_id: str, execution_job_id: str, *, note: str | None = None) -> dict[str, Any]:
    task = feedback_store.find_task(task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Optimization task not found")
    if task.get("applied_agent_version_id"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Task is already applied")
    job = feedback_store.get_execution_job(execution_job_id)
    if not job or job.get("optimization_task_id") != task_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Execution job not found")
    if job.get("status") != "ready":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Execution job is not ready")
    plan = job.get("validated_output_json")
    if not isinstance(plan, dict):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Execution job has no validated plan")
    baseline_version_id = str(job.get("baseline_agent_version_id") or task.get("baseline_agent_version_id") or "")
    current_version_id = agent_version_store.current_version_id()
    if baseline_version_id and current_version_id != baseline_version_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Current Agent version differs from execution baseline")
    pre_version = agent_version_store.create_snapshot(
        reason="pre_execution",
        source_proposal_ids=[str(item) for item in task.get("proposal_ids") or [] if item],
        note=note or f"执行优化任务 {task_id} 前快照。",
    )
    try:
        _apply_execution_operations(plan.get("operations") or [])
    except ValueError as exc:
        feedback_store.fail_execution_job(execution_job_id, "EXECUTION_APPLY_FAILED", str(exc))
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    applied_version = agent_version_store.create_snapshot(
        reason="execution_optimizer_applied",
        source_proposal_ids=[str(item) for item in task.get("proposal_ids") or [] if item],
        note=note or f"execution-optimizer 应用任务 {task_id}。",
        parent_version_id=str(pre_version.get("agent_version_id")),
    )
    applied_diff = agent_version_store.diff_versions(str(pre_version["agent_version_id"]), str(applied_version["agent_version_id"]))
    updated = feedback_store.mark_execution_job_applied(
        execution_job_id,
        pre_execution_version=pre_version,
        applied_agent_version=applied_version,
        applied_diff=applied_diff,
    )
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Execution job not found")
    return {"execution_job": updated, "optimization_task": feedback_store.find_task(task_id), "applied_diff": applied_diff}


def _batch_plan_task(batch: dict[str, Any] | None, plan_task_id: str) -> dict[str, Any] | None:
    plan = batch.get("optimization_plan") if isinstance((batch or {}).get("optimization_plan"), dict) else None
    for item in (plan or {}).get("tasks") or []:
        if isinstance(item, dict) and str(item.get("plan_task_id") or "") == plan_task_id:
            return item
    return None


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
        "runtime_db_backend": "sqlite",
        "runtime_db_path": str(settings.runtime_db_path),
        "legacy_file_store_enabled": False,
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
        "feedback_debug_evidence": settings.enable_feedback_debug_evidence,
        "agent_version_id": agent_version_store.current_version_id(),
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


@app.get(
    "/api/agent-runs",
    response_model=list[AgentRunResponse],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="List Agent run records used by feedback evidence packages",
)
async def list_agent_runs(
    run_id: str | None = None,
    session_id: str | None = None,
    alert_id: str | None = None,
    case_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    return feedback_store.list_runs(run_id=run_id, session_id=session_id, alert_id=alert_id, case_id=case_id, limit=limit)


@app.post(
    "/api/feedback-signals",
    response_model=FeedbackSignalResponse,
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Collect one feedback signal without attribution or proposal generation",
)
async def create_feedback_signal(req: FeedbackSignalCreateRequest) -> dict[str, Any]:
    try:
        return feedback_store.create_signal(req)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@app.get(
    "/api/feedback-signals",
    response_model=list[FeedbackSignalResponse],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="List collected feedback signals",
)
async def list_feedback_signals(
    run_id: str | None = None,
    session_id: str | None = None,
    alert_id: str | None = None,
    case_id: str | None = None,
    source_type: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    return feedback_store.list_signals(
        run_id=run_id,
        session_id=session_id,
        alert_id=alert_id,
        case_id=case_id,
        source_type=source_type,
        limit=limit,
    )


@app.get(
    "/api/feedback-signals/{signal_id}",
    response_model=FeedbackSignalResponse,
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Get one feedback signal",
)
async def get_feedback_signal(signal_id: str) -> dict[str, Any]:
    signal = feedback_store.find_signal(signal_id)
    if not signal:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback signal not found")
    return signal


@app.post(
    "/api/soc-events",
    response_model=SocEventIngestResponse,
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Collect one SOC event without attribution or proposal generation",
)
async def ingest_soc_event(req: SocEventIngestRequest) -> SocEventIngestResponse:
    return SocEventIngestResponse(**feedback_store.ingest_soc_event(req))


@app.get(
    "/api/soc-events",
    response_model=list[dict[str, Any]],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="List collected SOC events",
)
async def list_soc_events(
    run_id: str | None = None,
    session_id: str | None = None,
    alert_id: str | None = None,
    case_id: str | None = None,
    event_type: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    return feedback_store.list_events(
        run_id=run_id,
        session_id=session_id,
        alert_id=alert_id,
        case_id=case_id,
        event_type=event_type,
        limit=limit,
    )


@app.get(
    "/api/soc-events/{event_id}",
    response_model=dict[str, Any],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Get one SOC event",
)
async def get_soc_event(event_id: str) -> dict[str, Any]:
    event = feedback_store.find_event(event_id)
    if not event:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SOC event not found")
    return event


@app.get(
    "/api/pending-correlations",
    response_model=list[dict[str, Any]],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="List pending feedback correlations",
)
async def list_pending_correlations(
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    return feedback_store.list_pending(status=status_filter, limit=limit)


@app.post(
    "/api/pending-correlations/{pending_id}/resolve",
    response_model=dict[str, Any],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Resolve one pending feedback correlation",
)
async def resolve_pending_correlation(pending_id: str, req: PendingCorrelationResolveRequest) -> dict[str, Any]:
    resolved = feedback_store.resolve_pending(
        pending_id,
        run_id=req.run_id,
        session_id=req.session_id,
        alert_id=req.alert_id,
        case_id=req.case_id,
        comment=req.comment,
    )
    if not resolved:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pending correlation not found")
    return resolved


@app.get(
    "/api/feedback-sources",
    response_model=list[dict[str, Any]],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="List unified feedback sources for the product workflow",
)
async def list_feedback_sources(limit: int = Query(default=500, ge=1, le=1000)) -> list[dict[str, Any]]:
    return feedback_store.list_feedback_sources(limit=limit)


@app.get(
    "/api/feedback-sources/{source_kind}/{source_id}",
    response_model=dict[str, Any],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Get one unified feedback source",
)
async def get_feedback_source(source_kind: str, source_id: str) -> dict[str, Any]:
    try:
        source = feedback_store.find_feedback_source(source_kind, source_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if not source:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback source not found")
    return source


@app.patch(
    "/api/feedback-sources/{source_kind}/{source_id}",
    response_model=dict[str, Any],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Update developer annotations for one feedback source",
)
async def update_feedback_source(
    source_kind: str,
    source_id: str,
    req: FeedbackSourceUpdateRequest,
) -> dict[str, Any]:
    try:
        source = feedback_store.update_feedback_source_annotation(source_kind, source_id, req.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if not source:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback source not found")
    return source


@app.post(
    "/api/feedback-sources/eval-cases/generate",
    response_model=dict[str, Any],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Generate default regression eval cases for selected feedback sources",
)
async def generate_feedback_source_eval_cases(req: FeedbackEvalCaseGenerateRequest) -> dict[str, Any]:
    if not req.source_refs:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="source_refs is required")
    return feedback_store.generate_eval_cases_for_sources(
        [item.model_dump(mode="json") for item in req.source_refs],
        force=req.force,
    )


@app.get(
    "/api/feedback-optimization-batches",
    response_model=list[dict[str, Any]],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="List feedback optimization batches",
)
async def list_feedback_optimization_batches(
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    return feedback_store.list_optimization_batches(status=status_filter, limit=limit)


@app.post(
    "/api/feedback-optimization-batches",
    response_model=dict[str, Any],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Create one optimization batch from selected feedback sources",
)
async def create_feedback_optimization_batch(req: FeedbackOptimizationBatchCreateRequest) -> dict[str, Any]:
    if not req.source_refs:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="source_refs is required")
    batch = feedback_store.create_optimization_batch(
        [item.model_dump(mode="json") for item in req.source_refs],
        title=req.title,
        priority=req.priority,
    )
    if not batch:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No selected feedback source can create an optimization batch")
    return batch


@app.get(
    "/api/feedback-optimization-batches/{batch_id}",
    response_model=dict[str, Any],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Get one feedback optimization batch",
)
async def get_feedback_optimization_batch(batch_id: str) -> dict[str, Any]:
    batch = feedback_store.find_optimization_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback optimization batch not found")
    return batch


@app.post(
    "/api/feedback-optimization-batches/{batch_id}/attribution-jobs",
    response_model=dict[str, Any],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Run attribution jobs for all feedback cases in one optimization batch",
)
async def run_feedback_optimization_batch_attribution(
    batch_id: str,
    req: FeedbackOptimizationBatchAttributionRequest | None = None,
) -> dict[str, Any]:
    return await _run_feedback_optimization_batch_attribution(batch_id, req or FeedbackOptimizationBatchAttributionRequest())


async def _run_feedback_optimization_batch_attribution(
    batch_id: str,
    req: FeedbackOptimizationBatchAttributionRequest,
) -> dict[str, Any]:
    batch = feedback_store.find_optimization_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback optimization batch not found")
    if req.force:
        try:
            batch = feedback_store.reset_batch_attribution(batch_id) or batch
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    jobs: list[dict[str, Any]] = []
    for feedback_case_id in batch.get("feedback_case_ids") or []:
        job = await runtime.run_attribution_job(str(feedback_case_id), force=req.force)
        if job:
            jobs.append(job)
    updated = feedback_store.record_batch_attribution_jobs(batch_id, jobs)
    return {"batch": updated, "jobs": jobs}


@app.post(
    "/api/feedback-optimization-batches/{batch_id}/optimization-plan",
    response_model=dict[str, Any],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Generate one aggregated optimization plan from batch attribution results",
)
async def generate_feedback_optimization_batch_plan(
    batch_id: str,
    req: FeedbackOptimizationBatchPlanGenerateRequest | None = None,
) -> dict[str, Any]:
    try:
        batch = await runtime.run_batch_optimization_plan(
            batch_id,
            regeneration_instruction=(req or FeedbackOptimizationBatchPlanGenerateRequest()).regeneration_instruction,
            force=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if not batch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback optimization batch not found")
    return batch


@app.post(
    "/api/feedback-optimization-batches/{batch_id}/optimization-plan/approve",
    response_model=dict[str, Any],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Execute one batch optimization plan, generate an execution plan, and apply controlled changes",
)
async def approve_feedback_optimization_batch_plan(
    batch_id: str,
    req: FeedbackOptimizationBatchPlanReviewRequest,
) -> dict[str, Any]:
    try:
        approved = feedback_store.approve_batch_optimization_plan(batch_id, comment=req.comment)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if not approved:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Optimization plan cannot be approved")
    task = approved["optimization_task"]
    execution_job = await runtime.run_execution_job(task["optimization_task_id"], force=True)
    if not execution_job:
        feedback_store.record_batch_execution_result(batch_id, optimization_task=feedback_store.find_task(task["optimization_task_id"]))
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Execution optimizer could not generate a plan")
    apply_result = None
    if execution_job.get("status") == "ready":
        apply_result = _apply_ready_execution_job(
            task["optimization_task_id"],
            execution_job["execution_job_id"],
            note=f"执行优化批次 {batch_id} 时由 execution-optimizer 自动应用。",
        )
    batch = feedback_store.record_batch_execution_result(
        batch_id,
        execution_job=execution_job,
        optimization_task=feedback_store.find_task(task["optimization_task_id"]),
        applied=apply_result,
    )
    return {
        "batch": batch,
        "optimization_task": feedback_store.find_task(task["optimization_task_id"]),
        "execution_job": execution_job,
        "apply_result": apply_result,
    }


@app.post(
    "/api/feedback-optimization-batches/{batch_id}/optimization-plan/reject",
    response_model=dict[str, Any],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Reject one batch optimization plan",
)
async def reject_feedback_optimization_batch_plan(
    batch_id: str,
    req: FeedbackOptimizationBatchPlanReviewRequest,
) -> dict[str, Any]:
    batch = feedback_store.reject_batch_optimization_plan(batch_id, comment=req.comment)
    if not batch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback optimization batch or plan not found")
    return batch


@app.post(
    "/api/feedback-optimization-batches/{batch_id}/optimization-plan/tasks/{plan_task_id}/execute",
    response_model=dict[str, Any],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Execute one task from a batch optimization plan",
)
async def execute_feedback_optimization_plan_task(
    batch_id: str,
    plan_task_id: str,
    req: FeedbackOptimizationPlanTaskExecuteRequest,
) -> dict[str, Any]:
    batch = feedback_store.find_optimization_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback optimization batch not found")
    plan = batch.get("optimization_plan") if isinstance(batch.get("optimization_plan"), dict) else None
    plan_task = next(
        (
            item
            for item in (plan or {}).get("tasks") or []
            if isinstance(item, dict) and str(item.get("plan_task_id") or "") == plan_task_id
        ),
        None,
    )
    if not plan_task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Optimization plan task not found")
    execution_kind = str(plan_task.get("execution_kind") or "")
    if execution_kind == "external_webhook":
        if not req.webhook_alias:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="webhook_alias is required for external tasks")
        try:
            result = feedback_store.notify_batch_plan_task_external(batch_id, plan_task_id, webhook_alias=req.webhook_alias)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        if not result:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Optimization plan task not found")
        return result
    if execution_kind != "workspace_execution":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Optimization plan task requires manual review")

    try:
        prepared = feedback_store.prepare_batch_plan_task_execution(
            batch_id,
            plan_task_id,
            comment=f"执行优化批次 {batch_id} 的任务 {plan_task_id}",
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if not prepared:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Optimization plan task not found")
    task = prepared["optimization_task"]
    apply_result = None
    execution_job = None
    if task.get("applied_agent_version_id"):
        batch = feedback_store.record_batch_plan_task_execution_result(batch_id, plan_task_id, optimization_task=task)
        return {"batch": batch, "optimization_task": task, "plan_task": _batch_plan_task(batch, plan_task_id), "execution_job": None, "apply_result": None}

    execution_job = await runtime.run_execution_job(task["optimization_task_id"], force=req.force)
    if not execution_job:
        batch = feedback_store.record_batch_plan_task_execution_result(
            batch_id,
            plan_task_id,
            optimization_task=feedback_store.find_task(task["optimization_task_id"]),
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Execution optimizer could not generate a plan")
    if execution_job.get("status") == "ready":
        apply_result = _apply_ready_execution_job(
            task["optimization_task_id"],
            execution_job["execution_job_id"],
            note=f"执行优化批次 {batch_id} 的任务 {plan_task_id} 时由 execution-optimizer 自动应用。",
        )
    batch = feedback_store.record_batch_plan_task_execution_result(
        batch_id,
        plan_task_id,
        execution_job=execution_job,
        optimization_task=feedback_store.find_task(task["optimization_task_id"]),
        applied=apply_result,
    )
    return {
        "batch": batch,
        "optimization_task": feedback_store.find_task(task["optimization_task_id"]),
        "plan_task": _batch_plan_task(batch, plan_task_id),
        "execution_job": execution_job,
        "apply_result": apply_result,
    }


@app.post(
    "/api/feedback-optimization-batches/{batch_id}/regression-runs",
    response_model=dict[str, Any],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Run regression validation for all active eval cases in one optimization batch",
)
async def run_feedback_optimization_batch_regression(batch_id: str) -> dict[str, Any]:
    batch = feedback_store.find_optimization_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback optimization batch not found")
    task_id = str(batch.get("optimization_task_id") or "")
    task = feedback_store.find_task(task_id)
    if not task or not task.get("applied_agent_version_id"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Batch optimization must be applied before regression validation")
    eval_case_ids = [str(item) for item in batch.get("eval_case_ids") or [] if item]
    if not eval_case_ids:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No eval cases found for this batch")
    result = await runtime.run_feedback_eval(
        eval_case_ids=eval_case_ids,
        optimization_task_id=task_id,
        source="optimization_batch_regression",
    )
    if not result:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Regression run could not be started")
    batch = feedback_store.record_batch_regression_result(batch_id, result)
    return {"batch": batch, "eval_run": result}


@app.get(
    "/api/feedback-cases",
    response_model=list[FeedbackCaseResponse],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="List feedback disposition cases",
)
async def list_feedback_cases(
    status: str | None = None,
    q: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    return feedback_store.list_cases(status=status, q=q, limit=limit)


@app.get(
    "/api/feedback-cases/{feedback_case_id}",
    response_model=FeedbackCaseResponse,
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Get one feedback disposition case",
)
async def get_feedback_case(feedback_case_id: str) -> dict[str, Any]:
    feedback_case = feedback_store.find_case(feedback_case_id)
    if not feedback_case:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback case not found")
    return feedback_case


@app.post(
    "/api/feedback-cases",
    response_model=FeedbackCaseResponse,
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Create one feedback disposition case from feedback signals",
)
async def create_feedback_case(req: FeedbackCaseCreateRequest) -> dict[str, Any]:
    if not req.source_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="source_ids is required")
    feedback_case = feedback_store.create_case(source_ids=req.source_ids, title=req.title, priority=req.priority)
    if not feedback_case:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback source not found")
    return feedback_case


@app.post(
    "/api/feedback-cases/{feedback_case_id}/evidence-packages",
    response_model=EvidencePackageResponse,
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Create one immutable evidence package for a feedback case",
)
async def create_evidence_package(feedback_case_id: str) -> dict[str, Any]:
    evidence_package = feedback_store.create_evidence_package(feedback_case_id)
    if not evidence_package:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback case not found")
    return evidence_package


@app.get(
    "/api/evidence-packages/{evidence_package_id}",
    response_model=EvidencePackageResponse,
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Get one evidence package manifest",
)
async def get_evidence_package(evidence_package_id: str) -> dict[str, Any]:
    evidence_package = feedback_store.get_evidence_package(evidence_package_id)
    if not evidence_package:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Evidence package not found")
    return evidence_package


@app.get(
    "/api/evidence-packages/{evidence_package_id}/files/{file_name}",
    response_model=EvidencePackageFileResponse,
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Get one evidence package JSON file",
)
async def get_evidence_package_file(evidence_package_id: str, file_name: str) -> dict[str, Any]:
    evidence_file = feedback_store.get_evidence_package_file(evidence_package_id, file_name)
    if not evidence_file:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Evidence package file not found")
    return evidence_file


@app.post(
    "/api/feedback-cases/{feedback_case_id}/attribution-jobs",
    response_model=FeedbackAnalysisJobResponse,
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Run one attribution job for a feedback case",
)
async def create_attribution_job(feedback_case_id: str) -> dict[str, Any]:
    job = await runtime.run_attribution_job(feedback_case_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback case not found or missing evidence")
    return job


@app.post(
    "/api/feedback-cases/{feedback_case_id}/attribution-jobs/regenerate",
    response_model=FeedbackAnalysisJobResponse,
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Force regenerate one attribution job for a feedback case",
)
async def regenerate_attribution_job(feedback_case_id: str) -> dict[str, Any]:
    job = await runtime.run_attribution_job(feedback_case_id, force=True)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback case not found or missing evidence")
    return job


@app.post(
    "/api/feedback-cases/{feedback_case_id}/proposal-jobs",
    response_model=FeedbackAnalysisJobResponse,
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Run one optimization proposal job for a feedback case",
)
async def create_proposal_job(feedback_case_id: str) -> dict[str, Any]:
    job = await runtime.run_proposal_job(feedback_case_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback case not found or missing attribution")
    return job


@app.post(
    "/api/feedback-cases/{feedback_case_id}/proposal-jobs/regenerate",
    response_model=FeedbackAnalysisJobResponse,
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Force regenerate one optimization proposal job and supersede unused existing proposals",
)
async def regenerate_proposal_job(feedback_case_id: str, req: FeedbackProposalRegenerateRequest | None = None) -> dict[str, Any]:
    job = await runtime.run_proposal_job(
        feedback_case_id,
        force=True,
        regeneration_instruction=req.regeneration_instruction if req else None,
    )
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback case not found or missing attribution")
    return job


@app.get(
    "/api/feedback-analysis/jobs/{job_id}",
    response_model=FeedbackAnalysisJobResponse,
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Get one feedback analysis job",
)
async def get_feedback_analysis_job(job_id: str) -> dict[str, Any]:
    job = feedback_store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback analysis job not found")
    return job


@app.get(
    "/api/feedback-analysis/jobs/{job_id}/attribution",
    response_model=dict[str, Any],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Get one attribution job validated output",
)
async def get_attribution_output(job_id: str) -> dict[str, Any]:
    output = feedback_store.get_job_output(job_id, "attribution")
    if not output:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attribution output not found")
    return output


@app.get(
    "/api/feedback-analysis/jobs/{job_id}/proposal",
    response_model=dict[str, Any],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Get one proposal job validated output",
)
async def get_proposal_output(job_id: str) -> dict[str, Any]:
    output = feedback_store.get_job_output(job_id, "proposal")
    if not output:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Proposal output not found")
    return output


@app.post(
    "/api/feedback-analysis/jobs/{job_id}/proposal/revalidate",
    response_model=FeedbackAnalysisJobResponse,
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Revalidate one proposal job raw output without rerunning the Agent",
)
async def revalidate_proposal_output(job_id: str) -> dict[str, Any]:
    job = feedback_store.revalidate_proposal_job(job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Proposal job raw output not found")
    return job


@app.get(
    "/api/agent-versions/main/current",
    response_model=dict[str, Any],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Get current Agent managed configuration version",
)
async def current_agent_version() -> dict[str, Any]:
    return agent_version_store.ensure_bootstrap()


@app.get(
    "/api/agent-versions/main",
    response_model=list[dict[str, Any]],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="List Agent managed configuration versions",
)
async def list_agent_versions(limit: int = Query(default=100, ge=1, le=500)) -> list[dict[str, Any]]:
    agent_version_store.ensure_bootstrap()
    return agent_version_store.list_versions(limit=limit)


@app.post(
    "/api/agent-versions/main/snapshots",
    response_model=dict[str, Any],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Create one Agent managed configuration snapshot",
)
async def create_agent_version_snapshot(req: AgentVersionSnapshotRequest) -> dict[str, Any]:
    return agent_version_store.create_snapshot(
        reason=req.reason or "manual_snapshot",
        source_proposal_ids=req.source_proposal_ids,
        note=req.note,
    )


@app.post(
    "/api/agent-versions/main/{version_id}/rollback",
    response_model=AgentVersionRestoreResponse,
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Restore one Agent managed configuration version",
)
async def restore_agent_version(version_id: str, req: AgentVersionRestoreRequest) -> AgentVersionRestoreResponse:
    try:
        result = agent_version_store.restore_version(version_id, note=req.note)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if not result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent version not found")
    return AgentVersionRestoreResponse(**result)


@app.get(
    "/api/agent-versions/main/diff",
    response_model=dict[str, Any],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Diff two Agent managed configuration versions",
)
async def diff_agent_versions(from_version_id: str, to_version_id: str) -> dict[str, Any]:
    diff = agent_version_store.diff_versions(from_version_id, to_version_id)
    if not diff:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent version not found")
    return diff


@app.get(
    "/api/agent-versions/main/file-diff",
    response_model=dict[str, Any],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Diff one file between two Agent managed configuration versions",
)
async def diff_agent_version_file(from_version_id: str, to_version_id: str, path: str) -> dict[str, Any]:
    diff = agent_version_store.diff_version_file(from_version_id, to_version_id, path)
    if not diff:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent version or file path not found")
    return diff


@app.get(
    "/api/agent-versions/main/{version_id}",
    response_model=dict[str, Any],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Get one Agent version manifest",
)
async def get_agent_version(version_id: str) -> dict[str, Any]:
    manifest = agent_version_store.get_manifest(version_id)
    if not manifest:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent version not found")
    return manifest


@app.get(
    "/api/optimization-proposals",
    response_model=list[dict[str, Any]],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="List pending feedback-driven optimization proposals",
)
async def list_optimization_proposals(
    feedback_case_id: str | None = None,
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    return feedback_store.list_proposals(feedback_case_id=feedback_case_id, status=status, limit=limit)


@app.get(
    "/api/optimization-proposals/{proposal_id}",
    response_model=dict[str, Any],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Get one feedback-driven optimization proposal",
)
async def get_optimization_proposal(proposal_id: str) -> dict[str, Any]:
    proposal = feedback_store.find_proposal(proposal_id)
    if not proposal:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Proposal not found")
    return proposal


@app.post(
    "/api/optimization-proposals/{proposal_id}/approve",
    response_model=OptimizationProposalReviewResponse,
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Approve one feedback-driven optimization proposal",
)
async def approve_optimization_proposal(
    proposal_id: str,
    req: OptimizationProposalReviewRequest,
) -> OptimizationProposalReviewResponse:
    result = feedback_store.review_proposal(proposal_id, action="approve", comment=req.comment)
    if not result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Proposal not found")
    return OptimizationProposalReviewResponse(**result)


@app.post(
    "/api/optimization-proposals/{proposal_id}/reject",
    response_model=OptimizationProposalReviewResponse,
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Reject one feedback-driven optimization proposal",
)
async def reject_optimization_proposal(
    proposal_id: str,
    req: OptimizationProposalReviewRequest,
) -> OptimizationProposalReviewResponse:
    result = feedback_store.review_proposal(proposal_id, action="reject", comment=req.comment)
    if not result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Proposal not found")
    return OptimizationProposalReviewResponse(**result)


@app.post(
    "/api/optimization-proposals/{proposal_id}/request-more-analysis",
    response_model=OptimizationProposalReviewResponse,
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Request more analysis for one feedback-driven optimization proposal",
)
async def request_more_analysis_for_proposal(
    proposal_id: str,
    req: OptimizationProposalReviewRequest,
) -> OptimizationProposalReviewResponse:
    result = feedback_store.review_proposal(proposal_id, action="request_more_analysis", comment=req.comment)
    if not result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Proposal not found")
    return OptimizationProposalReviewResponse(**result)


@app.get(
    "/api/optimization-tasks",
    response_model=list[OptimizationTaskResponse],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="List feedback-driven optimization tasks",
)
async def list_optimization_tasks(
    feedback_case_id: str | None = None,
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    return feedback_store.list_tasks(feedback_case_id=feedback_case_id, status=status, limit=limit)


@app.get(
    "/api/optimization-tasks/{task_id}",
    response_model=OptimizationTaskResponse,
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Get one feedback-driven optimization task",
)
async def get_optimization_task(task_id: str) -> dict[str, Any]:
    task = feedback_store.find_task(task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Optimization task not found")
    return task


@app.get(
    "/api/external-governance-webhooks",
    response_model=list[ExternalGovernanceWebhookResponse],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="List configured external governance webhook aliases",
)
async def list_external_governance_webhooks() -> list[dict[str, Any]]:
    try:
        return feedback_store.list_external_webhooks()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@app.get(
    "/api/external-governance-items",
    response_model=list[ExternalGovernanceItemResponse],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="List external governance items derived from external guidance",
)
async def list_external_governance_items(
    feedback_case_id: str | None = None,
    proposal_job_id: str | None = None,
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    return feedback_store.list_external_governance_items(
        feedback_case_id=feedback_case_id,
        proposal_job_id=proposal_job_id,
        status=status,
        limit=limit,
    )


@app.post(
    "/api/external-governance-items/{external_item_id}/notify",
    response_model=ExternalGovernanceItemResponse,
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Notify one configured external system about an external governance item",
)
async def notify_external_governance_item(
    external_item_id: str,
    req: ExternalGovernanceNotifyRequest,
) -> dict[str, Any]:
    try:
        result = feedback_store.notify_external_governance_item(external_item_id, webhook_alias=req.webhook_alias)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if not result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="External governance item not found")
    return result


@app.post(
    "/api/optimization-tasks/{task_id}/execution-jobs",
    response_model=dict[str, Any],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Generate one controlled execution plan for an optimization task",
)
async def create_optimization_execution_job(task_id: str, req: OptimizationExecutionCreateRequest) -> dict[str, Any]:
    job = await runtime.run_execution_job(task_id, force=req.force)
    if not job:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Optimization task cannot generate an execution plan")
    return job


@app.get(
    "/api/optimization-tasks/{task_id}/execution-jobs",
    response_model=list[dict[str, Any]],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="List controlled execution plans for one optimization task",
)
async def list_optimization_execution_jobs(task_id: str, limit: int = Query(default=100, ge=1, le=500)) -> list[dict[str, Any]]:
    if not feedback_store.find_task(task_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Optimization task not found")
    return feedback_store.list_execution_jobs(task_id, limit=limit)


@app.post(
    "/api/optimization-tasks/{task_id}/execution-jobs/{execution_job_id}/apply",
    response_model=dict[str, Any],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Apply one reviewed controlled execution plan",
)
async def apply_optimization_execution_job(
    task_id: str,
    execution_job_id: str,
    req: OptimizationExecutionApplyRequest,
) -> dict[str, Any]:
    if not req.confirm:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="confirm must be true")
    return _apply_ready_execution_job(task_id, execution_job_id, note=req.note)


@app.post(
    "/api/optimization-tasks/{task_id}/mark-applied",
    response_model=OptimizationTaskResponse,
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Mark one optimization task as manually applied and snapshot the main Agent version",
)
async def mark_optimization_task_applied(
    task_id: str,
    req: OptimizationTaskMarkAppliedRequest,
) -> dict[str, Any]:
    task = feedback_store.find_task(task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Optimization task not found")
    if task.get("applied_agent_version_id"):
        return task
    if task.get("status") not in {"pending_execution", "failed", "needs_human_review"}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Task cannot be marked applied from current status")
    version = agent_version_store.create_snapshot(
        reason="proposal_applied",
        source_proposal_ids=[str(item) for item in task.get("proposal_ids") or [] if item],
        note=req.note or f"优化任务 {task_id} 已人工应用，创建主智能体版本快照。",
    )
    updated = feedback_store.mark_task_applied(task_id, agent_version=version, note=req.note)
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Optimization task not found")
    return updated


@app.post(
    "/api/optimization-tasks/{task_id}/regression-runs",
    response_model=dict[str, Any],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Run manual regression validation for one optimization task",
)
async def create_optimization_task_regression_run(
    task_id: str,
    req: FeedbackEvalRunCreateRequest,
) -> dict[str, Any]:
    task = feedback_store.find_task(task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Optimization task not found")
    if not task.get("applied_agent_version_id"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Task must be marked applied before regression validation")
    if task.get("feedback_case_id"):
        feedback_store.sync_feedback_eval_cases(feedback_case_id=str(task["feedback_case_id"]))
    eval_case_ids = list(req.eval_case_ids or [])
    if not eval_case_ids and task.get("feedback_case_id"):
        eval_case_ids = [
            item["eval_case_id"]
            for item in feedback_store.list_eval_cases(
                status="active",
                source_feedback_case_id=str(task["feedback_case_id"]),
                limit=100,
            )
        ]
    if not eval_case_ids:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No active eval cases found for this task")
    result = await runtime.run_feedback_eval(
        eval_case_ids=eval_case_ids,
        optimization_task_id=task_id,
        source="manual_task_regression",
    )
    if not result:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Regression run could not be started")
    return result


@app.get(
    "/api/optimization-tasks/{task_id}/regression-runs",
    response_model=list[dict[str, Any]],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="List regression validation runs for one optimization task",
)
async def list_optimization_task_regression_runs(
    task_id: str,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    return feedback_store.list_eval_runs(optimization_task_id=task_id, limit=limit)


@app.post(
    "/api/optimization-proposals/{proposal_id}/tasks",
    response_model=OptimizationTaskResponse,
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Create one feedback-driven optimization task",
)
async def create_optimization_task(proposal_id: str, req: OptimizationTaskCreateRequest) -> dict[str, Any]:
    if req.proposal_id and req.proposal_id != proposal_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="proposal_id path/body mismatch")
    task = feedback_store.create_task(
        proposal_id=proposal_id,
        execution_mode=req.execution_mode,
        comment=req.comment,
    )
    if not task:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Proposal is missing, not approved, or not actionable")
    return task


@app.post(
    "/api/eval-datasets/feedback/sync",
    response_model=dict[str, Any],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Sync processed feedback cases into reusable eval cases",
)
async def sync_feedback_eval_dataset(req: FeedbackEvalDatasetSyncRequest) -> dict[str, Any]:
    return feedback_store.sync_feedback_eval_cases(feedback_case_id=req.feedback_case_id, limit=req.limit)


@app.get(
    "/api/eval-cases",
    response_model=list[dict[str, Any]],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="List feedback-derived eval cases",
)
async def list_eval_cases(
    status_filter: str | None = Query(default=None, alias="status"),
    source_feedback_case_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    return feedback_store.list_eval_cases(status=status_filter, source_feedback_case_id=source_feedback_case_id, limit=limit)


@app.patch(
    "/api/eval-cases/{eval_case_id}",
    response_model=dict[str, Any],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Update one feedback-derived eval case",
)
async def update_eval_case(eval_case_id: str, req: FeedbackEvalCaseUpdateRequest) -> dict[str, Any]:
    try:
        updated = feedback_store.update_eval_case(eval_case_id, req.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Eval case not found")
    return updated


@app.post(
    "/api/eval-runs",
    response_model=dict[str, Any],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Run a manual feedback dataset evaluation against the current main Agent",
)
async def create_eval_run(req: FeedbackEvalRunCreateRequest) -> dict[str, Any]:
    if not req.eval_case_ids:
        feedback_store.sync_feedback_eval_cases(limit=500)
    result = await runtime.run_feedback_eval(
        eval_case_ids=req.eval_case_ids or None,
        optimization_task_id=req.optimization_task_id,
        source="manual_feedback_dataset",
    )
    if not result:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No active eval cases found")
    return result


@app.get(
    "/api/eval-runs",
    response_model=list[dict[str, Any]],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="List feedback dataset eval runs",
)
async def list_eval_runs(
    optimization_task_id: str | None = None,
    agent_version_id: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    return feedback_store.list_eval_runs(
        optimization_task_id=optimization_task_id,
        agent_version_id=agent_version_id,
        status=status_filter,
        limit=limit,
    )


@app.get(
    "/api/eval-runs/{eval_run_id}",
    response_model=dict[str, Any],
    dependencies=[Depends(require_api_key)],
    tags=["feedback"],
    summary="Get one feedback dataset eval run",
)
async def get_eval_run(eval_run_id: str) -> dict[str, Any]:
    eval_run = feedback_store.get_eval_run(eval_run_id)
    if not eval_run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Eval run not found")
    return eval_run


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
