from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict

from fastapi import APIRouter, Depends, Query

from app.routers.error_helpers import ensure_found
from app.runtime.json_types import JsonObject
from app.runtime.records.source_records import (
    FeedbackEventType,
    FeedbackSignalSourceType,
    FeedbackSourceKind,
)
from app.runtime.schemas import (
    AssetProvenanceImprovement,
    AssetProvenanceRelease,
    AssetProvenanceResponse,
    FeedbackEventIngestRequest,
    FeedbackEventIngestResponse,
    FeedbackEventResponse,
    FeedbackSignalCreateRequest,
    FeedbackSignalReassignRequest,
    FeedbackSignalResponse,
    FeedbackSourceResponse,
    FeedbackSourceUpdateRequest,
    PendingCorrelationResolveRequest,
    PendingCorrelationResponse,
)
from app.runtime.state_machines import PendingCorrelationStatus
from app.runtime.stores.feedback_store import FeedbackStore
from app.runtime.stores.improvement_store import ImprovementStore
from app.runtime_gateway.contracts import AgentRunResponse
from app.services.agent_release_provenance import released_versions_for_feedback_case


def create_feedback_workbench_router(
    *,
    feedback_store: FeedbackStore,
    improvement_store: ImprovementStore,
    require_api_key: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["feedback"], dependencies=[Depends(require_api_key)])
    _register_agent_run_list_route(router, feedback_store)
    _register_feedback_signal_routes(router, feedback_store)
    _register_feedback_provenance_route(router, feedback_store, improvement_store)
    _register_feedback_event_routes(router, feedback_store)
    _register_pending_correlation_routes(router, feedback_store)
    _register_feedback_source_routes(router, feedback_store)
    return router


def _register_agent_run_list_route(router: APIRouter, feedback_store: FeedbackStore) -> None:

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
        entity_type: str | None = None,
        entity_id: str | None = None,
        agent_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        before_created_at: str | None = Query(default=None, min_length=1, max_length=64),
        before_run_id: str | None = Query(default=None, min_length=1, max_length=128),
        include_messages: bool = Query(default=False, deprecated=True, description="Ignored; messages are owned by AgentScope."),
    ) -> list[JsonObject]:
        runs = feedback_store.list_runs(
            run_id=run_id,
            session_id=session_id,
            entity_type=entity_type,
            entity_id=entity_id,
            agent_id=agent_id,
            limit=limit,
            before_created_at=before_created_at,
            before_run_id=before_run_id,
        )
        del include_messages
        return runs


def _register_feedback_signal_routes(
    router: APIRouter,
    feedback_store: FeedbackStore,
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
        entity_type: str | None = None,
        entity_id: str | None = None,
        source_type: FeedbackSignalSourceType | None = None,
        agent_id: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[FeedbackSignalResponse]:
        return feedback_store.list_signals(
            run_id=run_id,
            session_id=session_id,
            entity_type=entity_type,
            entity_id=entity_id,
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


def _register_feedback_provenance_route(
    router: APIRouter,
    feedback_store: FeedbackStore,
    improvement_store: ImprovementStore,
) -> None:
    @router.get(
        "/asset-registry/feedback/{feedback_case_id}",
        response_model=AssetProvenanceResponse,
        summary="Asset relationship provenance for one feedback case (agent, assets, version)",
    )
    async def feedback_asset_provenance(feedback_case_id: str) -> AssetProvenanceResponse:
        case = ensure_found(feedback_store.find_case(feedback_case_id), "Feedback case not found")
        case_agent_id = case.get("agent_id")
        agent_ids = [case_agent_id] if isinstance(case_agent_id, str) and case_agent_id else []
        for signal_id in case.get("signal_ids") or []:
            signal = feedback_store.find_signal(signal_id)
            agent_id = (signal or {}).get("agent_id")
            if agent_id and agent_id not in agent_ids:
                agent_ids.append(agent_id)
        assigned_id = improvement_store.improvement_id_for_feedback_case(feedback_case_id)
        assigned_item = improvement_store.get_improvement(assigned_id) if assigned_id else None
        improvements = [_asset_provenance_improvement(improvement_store, assigned_item)] if assigned_item is not None else []
        return AssetProvenanceResponse(
            feedback_case_id=feedback_case_id,
            agent_ids=agent_ids,
            improvements=improvements,
            released_versions=[
                AssetProvenanceRelease(**asdict(reference)) for reference in released_versions_for_feedback_case(feedback_store.Session, feedback_case_id)
            ],
        )


def _register_feedback_event_routes(router: APIRouter, feedback_store: FeedbackStore) -> None:

    @router.post(
        "/feedback-events",
        response_model=FeedbackEventIngestResponse,
        summary="Collect one business event without attribution or proposal generation",
    )
    async def ingest_feedback_event(req: FeedbackEventIngestRequest) -> FeedbackEventIngestResponse:
        return FeedbackEventIngestResponse.model_validate(feedback_store.ingest_feedback_event(req).to_payload())

    @router.get(
        "/feedback-events",
        response_model=list[FeedbackEventResponse],
        summary="List collected business events",
    )
    async def list_feedback_events(
        run_id: str | None = None,
        session_id: str | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        event_type: FeedbackEventType | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[FeedbackEventResponse]:
        return feedback_store.list_events(
            run_id=run_id,
            session_id=session_id,
            entity_type=entity_type,
            entity_id=entity_id,
            event_type=event_type,
            limit=limit,
        )

    @router.get(
        "/feedback-events/{event_id}",
        response_model=FeedbackEventResponse,
        summary="Get one business event",
    )
    async def get_feedback_event(event_id: str) -> FeedbackEventResponse:
        event = feedback_store.find_event(event_id)
        return ensure_found(event, "business event not found")


def _register_pending_correlation_routes(router: APIRouter, feedback_store: FeedbackStore) -> None:

    @router.get(
        "/pending-correlations",
        response_model=list[PendingCorrelationResponse],
        summary="List pending feedback correlations",
    )
    async def list_pending_correlations(
        status: PendingCorrelationStatus | None = None,
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
            entities=req.entities,
            comment=req.comment,
        )
        return ensure_found(resolved, "Pending correlation not found")


def _register_feedback_source_routes(router: APIRouter, feedback_store: FeedbackStore) -> None:

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
    async def get_feedback_source(source_kind: FeedbackSourceKind, source_id: str) -> FeedbackSourceResponse:
        source = feedback_store.find_feedback_source(source_kind, source_id)
        return ensure_found(source, "Feedback source not found")

    @router.patch(
        "/feedback-sources/{source_kind}/{source_id}",
        response_model=FeedbackSourceResponse,
        summary="Update developer annotations for one feedback source",
    )
    async def update_feedback_source(
        source_kind: FeedbackSourceKind,
        source_id: str,
        req: FeedbackSourceUpdateRequest,
    ) -> FeedbackSourceResponse:
        source = feedback_store.update_feedback_source_annotation(source_kind, source_id, req.model_dump(exclude_unset=True))
        return ensure_found(source, "Feedback source not found")
