from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends

from app.runtime.errors import NotFoundError
from app.runtime.improvement_content_schemas import (
    AttributionResponse,
    AttributionUpsertRequest,
    ImprovementFeedbackCreateRequest,
    ImprovementFeedbackResponse,
    NormalizedFeedbackResponse,
    NormalizedFeedbackUpsertRequest,
)
from app.runtime.stores.improvement_content_store import (
    AttributionRecord,
    ImprovementContentStore,
    ImprovementFeedbackRecord,
    NormalizedFeedbackRecord,
)
from app.runtime.stores.improvement_store import ImprovementStore


def _nf_response(r: NormalizedFeedbackRecord) -> NormalizedFeedbackResponse:
    return NormalizedFeedbackResponse(
        normalized_feedback_id=r.normalized_feedback_id, improvement_id=r.improvement_id, problem=r.problem,
        possible_reason=r.possible_reason, possible_object=r.possible_object, impact=r.impact,
        suggestion=r.suggestion, user_quote=r.user_quote, status=r.status, created_at=r.created_at, updated_at=r.updated_at,
    )


def _fb_response(r: ImprovementFeedbackRecord) -> ImprovementFeedbackResponse:
    return ImprovementFeedbackResponse(
        feedback_id=r.feedback_id, improvement_id=r.improvement_id, agent_id=r.agent_id, summary=r.summary,
        source=r.source, status=r.status, raw_text=r.raw_text, run_id=r.run_id, session_id=r.session_id, created_at=r.created_at,
    )


def _attr_response(r: AttributionRecord) -> AttributionResponse:
    return AttributionResponse(
        attribution_id=r.attribution_id, improvement_id=r.improvement_id, summary=r.summary,
        responsibility_boundary=list(r.responsibility_boundary), evidence=list(r.evidence),
        status=r.status, created_at=r.created_at, updated_at=r.updated_at,
    )


def create_improvement_content_router(
    *,
    improvement_store: ImprovementStore,
    content_store: ImprovementContentStore,
    require_api_key: Callable,
) -> APIRouter:
    """改进事项内容子资源（v2.7 §4/§6 P3）：系统理解 NormalizedFeedback + 归因 Attribution。"""
    router = APIRouter(prefix="/api", tags=["improvements"], dependencies=[Depends(require_api_key)])

    def _require(improvement_id: str) -> None:
        if improvement_store.get_improvement(improvement_id) is None:
            raise NotFoundError(f"ImprovementItem not found: {improvement_id}")

    @router.get("/improvements/{improvement_id}/feedbacks", response_model=list[ImprovementFeedbackResponse], summary="List source feedbacks of an improvement (404 if unknown)")
    async def list_feedbacks(improvement_id: str) -> list[ImprovementFeedbackResponse]:
        _require(improvement_id)
        return [_fb_response(r) for r in content_store.list_feedbacks(improvement_id)]

    @router.post("/improvements/{improvement_id}/feedbacks", response_model=ImprovementFeedbackResponse, status_code=201, summary="Add a source feedback to an improvement (§8.4)")
    async def add_feedback(improvement_id: str, req: ImprovementFeedbackCreateRequest) -> ImprovementFeedbackResponse:
        item = improvement_store.get_improvement(improvement_id)
        if item is None:
            raise NotFoundError(f"ImprovementItem not found: {improvement_id}")
        return _fb_response(content_store.create_feedback(
            improvement_id, agent_id=item.agent_id, summary=req.summary, source=req.source,
            raw_text=req.raw_text, run_id=req.run_id, session_id=req.session_id,
        ))

    @router.put("/improvements/{improvement_id}/normalized-feedback", response_model=NormalizedFeedbackResponse, summary="Upsert system understanding (NormalizedFeedback)")
    async def upsert_nf(improvement_id: str, req: NormalizedFeedbackUpsertRequest) -> NormalizedFeedbackResponse:
        _require(improvement_id)
        return _nf_response(content_store.upsert_normalized_feedback(
            improvement_id, problem=req.problem, possible_reason=req.possible_reason, possible_object=req.possible_object,
            impact=req.impact, suggestion=req.suggestion, user_quote=req.user_quote,
        ))

    @router.get("/improvements/{improvement_id}/normalized-feedback", response_model=NormalizedFeedbackResponse, summary="Get system understanding (404 if none)")
    async def get_nf(improvement_id: str) -> NormalizedFeedbackResponse:
        record = content_store.get_normalized_feedback(improvement_id)
        if record is None:
            raise NotFoundError(f"No normalized feedback for improvement: {improvement_id}")
        return _nf_response(record)

    @router.post("/improvements/{improvement_id}/normalized-feedback/confirm", response_model=NormalizedFeedbackResponse, summary="Confirm system understanding")
    async def confirm_nf(improvement_id: str) -> NormalizedFeedbackResponse:
        return _nf_response(content_store.set_normalized_feedback_status(improvement_id, status="confirmed"))

    @router.put("/improvements/{improvement_id}/attribution", response_model=AttributionResponse, summary="Upsert attribution (text + responsibility boundary + evidence)")
    async def upsert_attr(improvement_id: str, req: AttributionUpsertRequest) -> AttributionResponse:
        _require(improvement_id)
        return _attr_response(content_store.upsert_attribution(
            improvement_id, summary=req.summary, responsibility_boundary=req.responsibility_boundary, evidence=req.evidence,
        ))

    @router.get("/improvements/{improvement_id}/attribution", response_model=AttributionResponse, summary="Get attribution (404 if none)")
    async def get_attr(improvement_id: str) -> AttributionResponse:
        record = content_store.get_attribution(improvement_id)
        if record is None:
            raise NotFoundError(f"No attribution for improvement: {improvement_id}")
        return _attr_response(record)

    @router.post("/improvements/{improvement_id}/attribution/confirm", response_model=AttributionResponse, summary="Confirm attribution")
    async def confirm_attr(improvement_id: str) -> AttributionResponse:
        return _attr_response(content_store.set_attribution_status(improvement_id, status="confirmed"))

    return router
