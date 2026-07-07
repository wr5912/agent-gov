from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Query

from app.routers.error_helpers import ensure_found, require_request
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.json_types import JsonObject
from app.runtime.message_utils import extract_answer_from_messages
from app.runtime.response_schemas.agent_job_response_schemas import AgentJobResponse
from app.runtime.schemas import (
    AgentRunResponse,
    AssetProvenanceImprovement,
    AssetProvenanceResponse,
    FeedbackEvalCaseGenerateRequest,
    FeedbackSignalCreateRequest,
    FeedbackSignalReassignRequest,
    FeedbackSignalResponse,
    FeedbackSourceResponse,
    FeedbackSourceUpdateRequest,
    PendingCorrelationResolveRequest,
    PendingCorrelationResponse,
    SocEventIngestRequest,
    SocEventIngestResponse,
    SocEventResponse,
)
from app.runtime.stores.feedback_store import FeedbackStore
from app.runtime.stores.improvement_store import ImprovementStore


def create_feedback_workbench_router(
    *,
    feedback_store: FeedbackStore,
    improvement_store: ImprovementStore,
    runtime: ClaudeRuntime,
    require_api_key: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["feedback"], dependencies=[Depends(require_api_key)])
    _register_agent_run_routes(router, feedback_store)
    _register_feedback_signal_routes(router, feedback_store, improvement_store)
    _register_soc_event_routes(router, feedback_store)
    _register_pending_correlation_routes(router, feedback_store)
    _register_feedback_source_routes(router, feedback_store, runtime)
    return router


def _register_agent_run_routes(router: APIRouter, feedback_store: FeedbackStore) -> None:

    @router.get(
        "/agent-runs",
        response_model=list[AgentRunResponse],
        response_model_exclude_none=True,
        response_model_exclude_defaults=True,
        summary="List Agent run records used by feedback evidence packages",
    )
    async def list_agent_runs(
        run_id: str | None = None,
        session_id: str | None = None,
        alert_id: str | None = None,
        case_id: str | None = None,
        agent_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        include_messages: bool = Query(
            default=False,
            description="Return full SDK messages and reconstructed assistant answer for Playground session restore.",
        ),
    ) -> list[JsonObject]:
        runs = feedback_store.list_runs(run_id=run_id, session_id=session_id, alert_id=alert_id, case_id=case_id, agent_id=agent_id, limit=limit)
        return [_agent_run_response_payload(run, include_messages=include_messages) for run in runs]


def _agent_run_response_payload(run: JsonObject, *, include_messages: bool) -> JsonObject:
    payload = dict(run)
    raw_messages = payload.get("messages")
    messages = raw_messages if isinstance(raw_messages, list) else []
    if not include_messages:
        payload.pop("messages", None)
        payload.pop("answer", None)
        return payload
    payload["messages"] = [message for message in messages if isinstance(message, dict)]
    if not isinstance(payload.get("answer"), str) or not str(payload.get("answer")).strip():
        answer = extract_answer_from_messages(payload["messages"])
        if answer:
            payload["answer"] = answer
    return payload


def _register_feedback_signal_routes(
    router: APIRouter,
    feedback_store: FeedbackStore,
    improvement_store: ImprovementStore,
) -> None:

    @router.post(
        "/feedback-signals",
        response_model=FeedbackSignalResponse,
        summary="Collect one feedback signal without attribution or proposal generation",
    )
    async def create_feedback_signal(req: FeedbackSignalCreateRequest) -> FeedbackSignalResponse:
        return feedback_store.create_signal(req)

    @router.get(
        "/feedback-signals",
        response_model=list[FeedbackSignalResponse],
        summary="List collected feedback signals",
    )
    async def list_feedback_signals(
        run_id: str | None = None,
        session_id: str | None = None,
        alert_id: str | None = None,
        case_id: str | None = None,
        source_type: str | None = None,
        agent_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[FeedbackSignalResponse]:
        return feedback_store.list_signals(
            run_id=run_id,
            session_id=session_id,
            alert_id=alert_id,
            case_id=case_id,
            source_type=source_type,
            agent_id=agent_id,
            limit=limit,
        )

    @router.get(
        "/feedback-signals/{signal_id}",
        response_model=FeedbackSignalResponse,
        summary="Get one feedback signal",
    )
    async def get_feedback_signal(signal_id: str) -> FeedbackSignalResponse:
        signal = feedback_store.find_signal(signal_id)
        return ensure_found(signal, "Feedback signal not found")

    @router.post(
        "/feedback-signals/{signal_id}/reassign-agent",
        response_model=FeedbackSignalResponse,
        summary="Reassign a feedback signal's owning agent (records an audit correction)",
    )
    async def reassign_feedback_signal_agent(signal_id: str, req: FeedbackSignalReassignRequest) -> FeedbackSignalResponse:
        # 管理员修正反馈归属；改写 agent_id 并保留 from/to/operator/reason 审计记录（AGV-025）。
        return feedback_store.reassign_signal_agent(signal_id, agent_id=req.agent_id, operator=req.operator, reason=req.reason).to_payload()

    @router.get(
        "/asset-registry/feedback/{feedback_case_id}",
        response_model=AssetProvenanceResponse,
        summary="Asset relationship provenance for one feedback case (agent, assets, version)",
    )
    async def feedback_asset_provenance(feedback_case_id: str) -> AssetProvenanceResponse:
        # AGV-022：从某次反馈追溯资产关系——影响了哪个 Agent、改了哪些资产、进入哪个版本。
        case = ensure_found(feedback_store.find_case(feedback_case_id), "Feedback case not found")
        agent_ids: list[str] = []
        for signal_id in case.get("signal_ids") or []:
            signal = feedback_store.find_signal(signal_id)
            agent_id = (signal or {}).get("agent_id")
            if agent_id and agent_id not in agent_ids:
                agent_ids.append(agent_id)
        improvements = [
            _asset_provenance_improvement(improvement_store, item)
            for item in improvement_store.list_improvements()
            if feedback_case_id in item.source_feedback_refs
        ]
        return AssetProvenanceResponse(feedback_case_id=feedback_case_id, agent_ids=agent_ids, improvements=improvements)


def _asset_provenance_improvement(
    improvement_store: ImprovementStore,
    item: object,
) -> AssetProvenanceImprovement:
    links = improvement_store.list_links(item.improvement_id)
    return AssetProvenanceImprovement(
        improvement_id=item.improvement_id,
        agent_id=item.agent_id,
        title=item.title,
        improvement_stage=item.improvement_stage,
        improvement_status=item.improvement_status,
        source_feedback_refs=list(item.source_feedback_refs),
        change_set_ids=[link.ref_id for link in links if link.kind == "change_set"],
    )


def _register_soc_event_routes(router: APIRouter, feedback_store: FeedbackStore) -> None:

    @router.post(
        "/soc-events",
        response_model=SocEventIngestResponse,
        summary="Collect one SOC event without attribution or proposal generation",
    )
    async def ingest_soc_event(req: SocEventIngestRequest) -> SocEventIngestResponse:
        return SocEventIngestResponse(**feedback_store.ingest_soc_event(req))

    @router.get(
        "/soc-events",
        response_model=list[SocEventResponse],
        summary="List collected SOC events",
    )
    async def list_soc_events(
        run_id: str | None = None,
        session_id: str | None = None,
        alert_id: str | None = None,
        case_id: str | None = None,
        event_type: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[SocEventResponse]:
        return feedback_store.list_events(
            run_id=run_id,
            session_id=session_id,
            alert_id=alert_id,
            case_id=case_id,
            event_type=event_type,
            limit=limit,
        )

    @router.get(
        "/soc-events/{event_id}",
        response_model=SocEventResponse,
        summary="Get one SOC event",
    )
    async def get_soc_event(event_id: str) -> SocEventResponse:
        event = feedback_store.find_event(event_id)
        return ensure_found(event, "SOC event not found")


def _register_pending_correlation_routes(router: APIRouter, feedback_store: FeedbackStore) -> None:

    @router.get(
        "/pending-correlations",
        response_model=list[PendingCorrelationResponse],
        summary="List pending feedback correlations",
    )
    async def list_pending_correlations(
        status: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[PendingCorrelationResponse]:
        return feedback_store.list_pending(status=status, limit=limit)

    @router.post(
        "/pending-correlations/{pending_id}/resolve",
        response_model=PendingCorrelationResponse,
        summary="Resolve one pending feedback correlation",
    )
    async def resolve_pending_correlation(pending_id: str, req: PendingCorrelationResolveRequest) -> PendingCorrelationResponse:
        resolved = feedback_store.resolve_pending(
            pending_id,
            run_id=req.run_id,
            session_id=req.session_id,
            alert_id=req.alert_id,
            case_id=req.case_id,
            comment=req.comment,
        )
        return ensure_found(resolved, "Pending correlation not found")


def _register_feedback_source_routes(router: APIRouter, feedback_store: FeedbackStore, runtime: ClaudeRuntime) -> None:

    @router.get(
        "/feedback-sources",
        response_model=list[FeedbackSourceResponse],
        summary="List unified feedback sources for the product workflow",
    )
    async def list_feedback_sources(limit: int = Query(default=500, ge=1, le=1000)) -> list[FeedbackSourceResponse]:
        return feedback_store.list_feedback_sources(limit=limit)

    @router.get(
        "/feedback-sources/{source_kind}/{source_id}",
        response_model=FeedbackSourceResponse,
        summary="Get one unified feedback source",
    )
    async def get_feedback_source(source_kind: str, source_id: str) -> FeedbackSourceResponse:
        source = feedback_store.find_feedback_source(source_kind, source_id)
        return ensure_found(source, "Feedback source not found")

    @router.patch(
        "/feedback-sources/{source_kind}/{source_id}",
        response_model=FeedbackSourceResponse,
        summary="Update developer annotations for one feedback source",
    )
    async def update_feedback_source(
        source_kind: str,
        source_id: str,
        req: FeedbackSourceUpdateRequest,
    ) -> FeedbackSourceResponse:
        source = feedback_store.update_feedback_source_annotation(source_kind, source_id, req.model_dump(exclude_unset=True))
        return ensure_found(source, "Feedback source not found")

    @router.post(
        "/feedback-sources/eval-cases/generate",
        response_model=AgentJobResponse,
        summary="Queue regression eval case generation for selected feedback sources",
    )
    async def generate_feedback_source_eval_cases(req: FeedbackEvalCaseGenerateRequest) -> AgentJobResponse:
        require_request(bool(req.source_refs), "source_refs is required")
        job = runtime.queue_eval_case_generation_job(
            source_refs=[item.model_dump(mode="json") for item in req.source_refs],
            force=req.force,
        )
        return ensure_found(job, "No feedback sources found for eval case generation")
