from __future__ import annotations

import json
from contextlib import asynccontextmanager
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
    FeedbackAnalysisJobResponse,
    FeedbackCaseCreateRequest,
    FeedbackCaseResponse,
    FeedbackSignalCreateRequest,
    FeedbackSignalResponse,
    OpenAIChatCompletionChoice,
    OpenAIChatCompletionRequest,
    OpenAIChatCompletionResponse,
    OpenAIChatMessage,
    OptimizationProposalReviewRequest,
    OptimizationProposalReviewResponse,
    OptimizationTaskCreateRequest,
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
    agent_version_provider=agent_version_store.current_version_id,
    runtime_version="0.2.0",
    enable_debug_evidence=settings.enable_feedback_debug_evidence,
)
runtime = ClaudeRuntime(settings, session_store, feedback_store, agent_version_store)
feedback_store.set_langfuse_trace_fetcher(runtime.fetch_langfuse_trace)
bearer_auth = HTTPBearer(auto_error=False)


@asynccontextmanager
async def lifespan(_: FastAPI):
    agent_version_store.ensure_bootstrap()
    yield


app = FastAPI(
    title="Claude Agent Runtime API",
    version="0.2.0",
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
