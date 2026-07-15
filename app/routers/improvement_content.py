from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends

from app.runtime.errors import BusinessRuleViolation, NotFoundError
from app.runtime.improvement_content_schemas import (
    AttributionResponse,
    AttributionUpsertRequest,
    ImprovementFeedbackCreateRequest,
    ImprovementFeedbackResponse,
    NormalizedFeedbackResponse,
    NormalizedFeedbackUpsertRequest,
    OptimizationChange,
    OptimizationPlanResponse,
    OptimizationPlanUpsertRequest,
)
from app.runtime.stores.improvement_content_store import (
    AttributionRecord,
    ImprovementContentStore,
    ImprovementFeedbackRecord,
    NormalizedFeedbackRecord,
    OptimizationPlanRecord,
)
from app.runtime.stores.improvement_store import ImprovementStore
from app.services.improvement_governor_service import ImprovementGovernorService


def _nf_response(r: NormalizedFeedbackRecord) -> NormalizedFeedbackResponse:
    return NormalizedFeedbackResponse(
        normalized_feedback_id=r.normalized_feedback_id,
        improvement_id=r.improvement_id,
        problem=r.problem,
        possible_reason=r.possible_reason,
        possible_object=r.possible_object,
        impact=r.impact,
        suggestion=r.suggestion,
        user_quote=r.user_quote,
        status=r.status,
        created_at=r.created_at,
        updated_at=r.updated_at,
        generated_by=r.generated_by,
        generation_trace_id=r.generation_trace_id,
        generation_trace_url=r.generation_trace_url,
    )


def _fb_response(r: ImprovementFeedbackRecord) -> ImprovementFeedbackResponse:
    return ImprovementFeedbackResponse(
        feedback_id=r.feedback_id,
        improvement_id=r.improvement_id,
        agent_id=r.agent_id,
        summary=r.summary,
        source=r.source,
        status=r.status,
        raw_text=r.raw_text,
        run_id=r.run_id,
        session_id=r.session_id,
        agent_version_id=r.agent_version_id,
        scenario=r.scenario,
        task_id=r.task_id,
        alert_id=r.alert_id,
        case_id=r.case_id,
        created_at=r.created_at,
    )


def _attr_response(r: AttributionRecord) -> AttributionResponse:
    return AttributionResponse(
        attribution_id=r.attribution_id,
        improvement_id=r.improvement_id,
        summary=r.summary,
        responsibility_boundary=list(r.responsibility_boundary),
        evidence=list(r.evidence),
        counter_evidence=list(r.counter_evidence),
        uncertainty_factors=list(r.uncertainty_factors),
        verification_suggestions=list(r.verification_suggestions),
        status=r.status,
        generated_by=r.generated_by,
        generation_trace_id=r.generation_trace_id,
        generation_trace_url=r.generation_trace_url,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


def _opt_response(r: OptimizationPlanRecord) -> OptimizationPlanResponse:
    return OptimizationPlanResponse(
        optimization_plan_id=r.optimization_plan_id,
        improvement_id=r.improvement_id,
        summary=r.summary,
        changes=[OptimizationChange(target=c.get("target", ""), change=c.get("change", "")) for c in r.changes],
        risk_level=r.risk_level,
        status=r.status,
        generated_by=r.generated_by,
        generation_trace_id=r.generation_trace_id,
        generation_trace_url=r.generation_trace_url,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


def _require_confirmed(record: object | None, *, artifact: str, improvement_id: str) -> None:
    if record is None or getattr(record, "status", "") != "confirmed":
        raise BusinessRuleViolation(f"Confirmed {artifact} is required: {improvement_id}")


def _register_feedback_routes(router: APIRouter, *, improvement_store: ImprovementStore, content_store: ImprovementContentStore, require: Callable) -> None:
    @router.get(
        "/improvements/{improvement_id}/feedbacks",
        response_model=list[ImprovementFeedbackResponse],
        summary="List source feedbacks of an improvement (404 if unknown)",
    )
    async def list_feedbacks(improvement_id: str) -> list[ImprovementFeedbackResponse]:
        require(improvement_id)
        return [_fb_response(r) for r in content_store.list_feedbacks(improvement_id)]

    @router.post(
        "/improvements/{improvement_id}/feedbacks",
        response_model=ImprovementFeedbackResponse,
        status_code=201,
        summary="Add a source feedback to an improvement (§8.4)",
    )
    async def add_feedback(improvement_id: str, req: ImprovementFeedbackCreateRequest) -> ImprovementFeedbackResponse:
        item = improvement_store.get_improvement(improvement_id)
        if item is None:
            raise NotFoundError(f"ImprovementItem not found: {improvement_id}")
        return _fb_response(
            content_store.create_feedback(
                improvement_id,
                agent_id=item.agent_id,
                summary=req.summary,
                source=req.source,
                raw_text=req.raw_text,
                run_id=req.run_id,
                session_id=req.session_id,
                agent_version_id=req.agent_version_id,
                scenario=req.scenario,
                task_id=req.task_id,
                alert_id=req.alert_id,
                case_id=req.case_id,
            )
        )


def _register_nf_routes(router: APIRouter, *, improvement_store: ImprovementStore, content_store: ImprovementContentStore, require: Callable) -> None:
    @router.put(
        "/improvements/{improvement_id}/normalized-feedback",
        response_model=NormalizedFeedbackResponse,
        summary="Upsert system understanding (NormalizedFeedback)",
    )
    async def upsert_nf(improvement_id: str, req: NormalizedFeedbackUpsertRequest) -> NormalizedFeedbackResponse:
        require(improvement_id)
        record = content_store.upsert_normalized_feedback(
            improvement_id,
            problem=req.problem,
            possible_reason=req.possible_reason,
            possible_object=req.possible_object,
            impact=req.impact,
            suggestion=req.suggestion,
            user_quote=req.user_quote,
            advance_to_stage="triage",
        )
        return _nf_response(record)

    @router.get(
        "/improvements/{improvement_id}/normalized-feedback", response_model=NormalizedFeedbackResponse, summary="Get system understanding (404 if none)"
    )
    async def get_nf(improvement_id: str) -> NormalizedFeedbackResponse:
        record = content_store.get_normalized_feedback(improvement_id)
        if record is None:
            raise NotFoundError(f"No normalized feedback for improvement: {improvement_id}")
        return _nf_response(record)

    @router.post(
        "/improvements/{improvement_id}/normalized-feedback/confirm", response_model=NormalizedFeedbackResponse, summary="Confirm system understanding"
    )
    async def confirm_nf(improvement_id: str) -> NormalizedFeedbackResponse:
        record = content_store.set_normalized_feedback_status(
            improvement_id,
            status="confirmed",
            advance_to_stage="triage",
        )
        return _nf_response(record)


def _register_attr_routes(router: APIRouter, *, improvement_store: ImprovementStore, content_store: ImprovementContentStore, require: Callable) -> None:
    @router.put(
        "/improvements/{improvement_id}/attribution",
        response_model=AttributionResponse,
        summary="Upsert attribution (text + responsibility boundary + evidence)",
    )
    async def upsert_attr(improvement_id: str, req: AttributionUpsertRequest) -> AttributionResponse:
        require(improvement_id)
        _require_confirmed(
            content_store.get_normalized_feedback(improvement_id),
            artifact="normalized feedback",
            improvement_id=improvement_id,
        )
        record = content_store.upsert_attribution(
            improvement_id,
            summary=req.summary,
            responsibility_boundary=req.responsibility_boundary,
            evidence=req.evidence,
            advance_to_stage="attribution",
        )
        return _attr_response(record)

    @router.get("/improvements/{improvement_id}/attribution", response_model=AttributionResponse, summary="Get attribution (404 if none)")
    async def get_attr(improvement_id: str) -> AttributionResponse:
        record = content_store.get_attribution(improvement_id)
        if record is None:
            raise NotFoundError(f"No attribution for improvement: {improvement_id}")
        return _attr_response(record)

    @router.post("/improvements/{improvement_id}/attribution/confirm", response_model=AttributionResponse, summary="Confirm attribution")
    async def confirm_attr(improvement_id: str) -> AttributionResponse:
        _require_confirmed(
            content_store.get_normalized_feedback(improvement_id),
            artifact="normalized feedback",
            improvement_id=improvement_id,
        )
        record = content_store.set_attribution_status(
            improvement_id,
            status="confirmed",
            advance_to_stage="attribution",
        )
        return _attr_response(record)


def _register_governance_generation_routes(
    router: APIRouter,
    *,
    improvement_store: ImprovementStore,
    content_store: ImprovementContentStore,
    governor_service: ImprovementGovernorService,
    require: Callable,
) -> None:
    """Generate normalized feedback, attribution, and optimization plans through the governor."""

    @router.post(
        "/improvements/{improvement_id}/normalized-feedback/generate",
        response_model=NormalizedFeedbackResponse,
        summary="Organize feedback into title/problem via DSPy formatter (heuristic fallback)",
    )
    async def generate_nf(improvement_id: str) -> NormalizedFeedbackResponse:
        if improvement_store.get_improvement(improvement_id) is None:
            raise NotFoundError(f"ImprovementItem not found: {improvement_id}")
        record = await governor_service.generate_normalized_feedback(
            improvement_id,
            advance_to_stage="triage",
        )
        return _nf_response(record)

    @router.post(
        "/improvements/{improvement_id}/attribution/generate",
        response_model=AttributionResponse,
        summary="Generate attribution via governor LLM (heuristic fallback)",
    )
    async def generate_attr(improvement_id: str) -> AttributionResponse:
        if improvement_store.get_improvement(improvement_id) is None:
            raise NotFoundError(f"ImprovementItem not found: {improvement_id}")
        _require_confirmed(
            content_store.get_normalized_feedback(improvement_id),
            artifact="normalized feedback",
            improvement_id=improvement_id,
        )
        record = await governor_service.generate_attribution(
            improvement_id,
            advance_to_stage="attribution",
        )
        return _attr_response(record)

    @router.post(
        "/improvements/{improvement_id}/optimization-plan/generate",
        response_model=OptimizationPlanResponse,
        summary="Generate optimization plan via governor LLM (heuristic fallback)",
    )
    async def generate_opt(improvement_id: str) -> OptimizationPlanResponse:
        if improvement_store.get_improvement(improvement_id) is None:
            raise NotFoundError(f"ImprovementItem not found: {improvement_id}")
        _require_confirmed(
            content_store.get_attribution(improvement_id),
            artifact="attribution",
            improvement_id=improvement_id,
        )
        record = await governor_service.generate_optimization_plan(
            improvement_id,
            advance_to_stage="optimization",
        )
        return _opt_response(record)

def _register_opt_routes(router: APIRouter, *, improvement_store: ImprovementStore, content_store: ImprovementContentStore, require: Callable) -> None:
    @router.put(
        "/improvements/{improvement_id}/optimization-plan", response_model=OptimizationPlanResponse, summary="Upsert optimization plan (text + changes, §106)"
    )
    async def upsert_opt(improvement_id: str, req: OptimizationPlanUpsertRequest) -> OptimizationPlanResponse:
        require(improvement_id)
        _require_confirmed(
            content_store.get_attribution(improvement_id),
            artifact="attribution",
            improvement_id=improvement_id,
        )
        record = content_store.upsert_optimization_plan(
            improvement_id,
            summary=req.summary,
            changes=[c.model_dump() for c in req.changes],
            advance_to_stage="optimization",
        )
        return _opt_response(record)

    @router.get("/improvements/{improvement_id}/optimization-plan", response_model=OptimizationPlanResponse, summary="Get optimization plan (404 if none)")
    async def get_opt(improvement_id: str) -> OptimizationPlanResponse:
        record = content_store.get_optimization_plan(improvement_id)
        if record is None:
            raise NotFoundError(f"No optimization plan for improvement: {improvement_id}")
        return _opt_response(record)

    @router.post("/improvements/{improvement_id}/optimization-plan/confirm", response_model=OptimizationPlanResponse, summary="Confirm optimization plan")
    async def confirm_opt(improvement_id: str) -> OptimizationPlanResponse:
        record = content_store.set_optimization_plan_status(
            improvement_id,
            status="confirmed",
            advance_to_stage="optimization",
        )
        return _opt_response(record)


def create_improvement_content_router(
    *,
    improvement_store: ImprovementStore,
    content_store: ImprovementContentStore,
    governor_service: ImprovementGovernorService,
    require_api_key: Callable,
) -> APIRouter:
    """改进事项内容子资源（四阶段改进治理 §4/§6/§8/§106/§107 P3）：系统理解 / 归因 / 优化方案 / 执行记录 / 来源反馈。"""
    router = APIRouter(prefix="/api", tags=["improvements"], dependencies=[Depends(require_api_key)])

    def _require(improvement_id: str) -> None:
        if improvement_store.get_improvement(improvement_id) is None:
            raise NotFoundError(f"ImprovementItem not found: {improvement_id}")

    _register_feedback_routes(router, improvement_store=improvement_store, content_store=content_store, require=_require)
    _register_nf_routes(router, improvement_store=improvement_store, content_store=content_store, require=_require)
    _register_attr_routes(router, improvement_store=improvement_store, content_store=content_store, require=_require)
    _register_governance_generation_routes(
        router,
        improvement_store=improvement_store,
        content_store=content_store,
        governor_service=governor_service,
        require=_require,
    )
    _register_opt_routes(router, improvement_store=improvement_store, content_store=content_store, require=_require)
    return router
