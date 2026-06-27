from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends

from app.runtime.errors import NotFoundError
from app.runtime.improvement_content_schemas import (
    AttributionResponse,
    AttributionUpsertRequest,
    ExecutionResponse,
    ExecutionUpsertRequest,
    ImprovementFeedbackCreateRequest,
    ImprovementFeedbackResponse,
    NormalizedFeedbackResponse,
    NormalizedFeedbackUpsertRequest,
    OptimizationChange,
    OptimizationPlanResponse,
    OptimizationPlanUpsertRequest,
    RegressionAssessmentResponse,
    RegressionCase,
)
from app.runtime.stores.improvement_content_store import (
    AttributionRecord,
    ExecutionRecord,
    ImprovementContentStore,
    ImprovementFeedbackRecord,
    NormalizedFeedbackRecord,
    OptimizationPlanRecord,
    RegressionAssessmentRecord,
)
from app.runtime.stores.improvement_store import ImprovementStore
from app.services.improvement_governor_service import ImprovementGovernorService
from app.services.improvement_execution_service import ImprovementExecutionService


def _nf_response(r: NormalizedFeedbackRecord) -> NormalizedFeedbackResponse:
    return NormalizedFeedbackResponse(
        normalized_feedback_id=r.normalized_feedback_id, improvement_id=r.improvement_id, problem=r.problem,
        possible_reason=r.possible_reason, possible_object=r.possible_object, impact=r.impact,
        suggestion=r.suggestion, user_quote=r.user_quote, status=r.status, created_at=r.created_at, updated_at=r.updated_at,
    )


def _fb_response(r: ImprovementFeedbackRecord) -> ImprovementFeedbackResponse:
    return ImprovementFeedbackResponse(
        feedback_id=r.feedback_id, improvement_id=r.improvement_id, agent_id=r.agent_id, summary=r.summary,
        source=r.source, status=r.status, raw_text=r.raw_text, run_id=r.run_id, session_id=r.session_id,
        agent_version_id=r.agent_version_id, scenario=r.scenario, task_id=r.task_id,
        alert_id=r.alert_id, case_id=r.case_id, created_at=r.created_at,
    )


def _attr_response(r: AttributionRecord) -> AttributionResponse:
    return AttributionResponse(
        attribution_id=r.attribution_id, improvement_id=r.improvement_id, summary=r.summary,
        responsibility_boundary=list(r.responsibility_boundary), evidence=list(r.evidence),
        counter_evidence=list(r.counter_evidence), uncertainty_factors=list(r.uncertainty_factors),
        verification_suggestions=list(r.verification_suggestions),
        status=r.status, generated_by=r.generated_by, created_at=r.created_at, updated_at=r.updated_at,
    )


def _opt_response(r: OptimizationPlanRecord) -> OptimizationPlanResponse:
    return OptimizationPlanResponse(
        optimization_plan_id=r.optimization_plan_id, improvement_id=r.improvement_id, summary=r.summary,
        changes=[OptimizationChange(target=c.get("target", ""), change=c.get("change", "")) for c in r.changes],
        risk_level=r.risk_level,
        status=r.status, generated_by=r.generated_by, created_at=r.created_at, updated_at=r.updated_at,
    )


def _reg_response(r: RegressionAssessmentRecord) -> RegressionAssessmentResponse:
    return RegressionAssessmentResponse(
        regression_assessment_id=r.regression_assessment_id, improvement_id=r.improvement_id, summary=r.summary,
        cases=[RegressionCase(prompt=str(c.get("prompt", "")), expected_behavior=str(c.get("expected_behavior", "")), checkpoints=[str(x) for x in (c.get("checkpoints") or [])]) for c in r.cases],
        suggested_gate_thresholds={str(k): str(v) for k, v in (r.suggested_gate_thresholds or {}).items()},
        status=r.status, generated_by=r.generated_by, created_at=r.created_at, updated_at=r.updated_at,
    )


def _exec_response(r: ExecutionRecord) -> ExecutionResponse:
    return ExecutionResponse(
        execution_id=r.execution_id, improvement_id=r.improvement_id, summary=r.summary,
        changes_applied=list(r.changes_applied), agent_version=r.agent_version,
        risk_level=r.risk_level, rollback_strategy=r.rollback_strategy, rollback_instructions=list(r.rollback_instructions),
        status=r.status, generated_by=r.generated_by, change_set_id=r.change_set_id,
        applied_agent_version_id=r.applied_agent_version_id, applied_diff=dict(r.applied_diff),
        created_at=r.created_at, updated_at=r.updated_at,
    )


def _register_feedback_routes(router: APIRouter, *, improvement_store: ImprovementStore, content_store: ImprovementContentStore, require: Callable) -> None:
    @router.get("/improvements/{improvement_id}/feedbacks", response_model=list[ImprovementFeedbackResponse], summary="List source feedbacks of an improvement (404 if unknown)")
    async def list_feedbacks(improvement_id: str) -> list[ImprovementFeedbackResponse]:
        require(improvement_id)
        return [_fb_response(r) for r in content_store.list_feedbacks(improvement_id)]

    @router.post("/improvements/{improvement_id}/feedbacks", response_model=ImprovementFeedbackResponse, status_code=201, summary="Add a source feedback to an improvement (§8.4)")
    async def add_feedback(improvement_id: str, req: ImprovementFeedbackCreateRequest) -> ImprovementFeedbackResponse:
        item = improvement_store.get_improvement(improvement_id)
        if item is None:
            raise NotFoundError(f"ImprovementItem not found: {improvement_id}")
        return _fb_response(content_store.create_feedback(
            improvement_id, agent_id=item.agent_id, summary=req.summary, source=req.source,
            raw_text=req.raw_text, run_id=req.run_id, session_id=req.session_id,
            agent_version_id=req.agent_version_id, scenario=req.scenario, task_id=req.task_id,
            alert_id=req.alert_id, case_id=req.case_id,
        ))


def _register_nf_routes(router: APIRouter, *, content_store: ImprovementContentStore, require: Callable) -> None:
    @router.put("/improvements/{improvement_id}/normalized-feedback", response_model=NormalizedFeedbackResponse, summary="Upsert system understanding (NormalizedFeedback)")
    async def upsert_nf(improvement_id: str, req: NormalizedFeedbackUpsertRequest) -> NormalizedFeedbackResponse:
        require(improvement_id)
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


def _register_attr_routes(router: APIRouter, *, content_store: ImprovementContentStore, require: Callable) -> None:
    @router.put("/improvements/{improvement_id}/attribution", response_model=AttributionResponse, summary="Upsert attribution (text + responsibility boundary + evidence)")
    async def upsert_attr(improvement_id: str, req: AttributionUpsertRequest) -> AttributionResponse:
        require(improvement_id)
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


def _register_governance_generation_routes(
    router: APIRouter,
    *,
    improvement_store: ImprovementStore,
    governor_service: ImprovementGovernorService,
    execution_service: ImprovementExecutionService,
    require: Callable,
) -> None:
    """§17.5：归因/方案由治理 Agent governor LLM 生成；执行由 governor 在隔离 worktree 自动 apply + 生成候选版本（均启发式兜底，见 service）。"""

    @router.post("/improvements/{improvement_id}/attribution/generate", response_model=AttributionResponse, summary="Generate attribution via governor LLM (heuristic fallback)")
    async def generate_attr(improvement_id: str) -> AttributionResponse:
        if improvement_store.get_improvement(improvement_id) is None:
            raise NotFoundError(f"ImprovementItem not found: {improvement_id}")
        return _attr_response(await governor_service.generate_attribution(improvement_id))

    @router.post("/improvements/{improvement_id}/optimization-plan/generate", response_model=OptimizationPlanResponse, summary="Generate optimization plan via governor LLM (heuristic fallback)")
    async def generate_opt(improvement_id: str) -> OptimizationPlanResponse:
        if improvement_store.get_improvement(improvement_id) is None:
            raise NotFoundError(f"ImprovementItem not found: {improvement_id}")
        return _opt_response(await governor_service.generate_optimization_plan(improvement_id))

    @router.post("/improvements/{improvement_id}/execution/apply", response_model=ExecutionResponse, summary="Governor executes confirmed plan on isolated worktree → candidate Agent version (heuristic fallback)")
    async def apply_exec(improvement_id: str) -> ExecutionResponse:
        if improvement_store.get_improvement(improvement_id) is None:
            raise NotFoundError(f"ImprovementItem not found: {improvement_id}")
        return _exec_response(await execution_service.generate_and_apply_execution(improvement_id))

    @router.post("/improvements/{improvement_id}/regression-assessment/generate", response_model=RegressionAssessmentResponse, summary="Generate regression test cases via governor (heuristic fallback)")
    async def generate_reg(improvement_id: str) -> RegressionAssessmentResponse:
        if improvement_store.get_improvement(improvement_id) is None:
            raise NotFoundError(f"ImprovementItem not found: {improvement_id}")
        return _reg_response(await governor_service.generate_regression_assessment(improvement_id))


def _register_opt_routes(router: APIRouter, *, content_store: ImprovementContentStore, require: Callable) -> None:
    @router.put("/improvements/{improvement_id}/optimization-plan", response_model=OptimizationPlanResponse, summary="Upsert optimization plan (text + changes, §106)")
    async def upsert_opt(improvement_id: str, req: OptimizationPlanUpsertRequest) -> OptimizationPlanResponse:
        require(improvement_id)
        return _opt_response(content_store.upsert_optimization_plan(
            improvement_id, summary=req.summary, changes=[c.model_dump() for c in req.changes],
        ))

    @router.get("/improvements/{improvement_id}/optimization-plan", response_model=OptimizationPlanResponse, summary="Get optimization plan (404 if none)")
    async def get_opt(improvement_id: str) -> OptimizationPlanResponse:
        record = content_store.get_optimization_plan(improvement_id)
        if record is None:
            raise NotFoundError(f"No optimization plan for improvement: {improvement_id}")
        return _opt_response(record)

    @router.post("/improvements/{improvement_id}/optimization-plan/confirm", response_model=OptimizationPlanResponse, summary="Confirm optimization plan")
    async def confirm_opt(improvement_id: str) -> OptimizationPlanResponse:
        return _opt_response(content_store.set_optimization_plan_status(improvement_id, status="confirmed"))


def _register_exec_routes(router: APIRouter, *, content_store: ImprovementContentStore, require: Callable) -> None:
    @router.put("/improvements/{improvement_id}/execution", response_model=ExecutionResponse, summary="Upsert execution record (result + applied changes + version, §107)")
    async def upsert_exec(improvement_id: str, req: ExecutionUpsertRequest) -> ExecutionResponse:
        require(improvement_id)
        return _exec_response(content_store.upsert_execution(
            improvement_id, summary=req.summary, changes_applied=req.changes_applied, agent_version=req.agent_version,
        ))

    @router.get("/improvements/{improvement_id}/execution", response_model=ExecutionResponse, summary="Get execution record (404 if none)")
    async def get_exec(improvement_id: str) -> ExecutionResponse:
        record = content_store.get_execution(improvement_id)
        if record is None:
            raise NotFoundError(f"No execution record for improvement: {improvement_id}")
        return _exec_response(record)

    @router.post("/improvements/{improvement_id}/execution/confirm", response_model=ExecutionResponse, summary="Confirm execution record")
    async def confirm_exec(improvement_id: str) -> ExecutionResponse:
        return _exec_response(content_store.set_execution_status(improvement_id, status="confirmed"))


def _register_regression_routes(router: APIRouter, *, content_store: ImprovementContentStore, require: Callable) -> None:
    @router.get("/improvements/{improvement_id}/regression-assessment", response_model=RegressionAssessmentResponse, summary="Get regression assessment (404 if none)")
    async def get_reg(improvement_id: str) -> RegressionAssessmentResponse:
        record = content_store.get_regression_assessment(improvement_id)
        if record is None:
            raise NotFoundError(f"No regression assessment for improvement: {improvement_id}")
        return _reg_response(record)

    @router.post("/improvements/{improvement_id}/regression-assessment/confirm", response_model=RegressionAssessmentResponse, summary="Confirm regression assessment (adopted as regression asset)")
    async def confirm_reg(improvement_id: str) -> RegressionAssessmentResponse:
        return _reg_response(content_store.set_regression_assessment_status(improvement_id, status="confirmed"))


def create_improvement_content_router(
    *,
    improvement_store: ImprovementStore,
    content_store: ImprovementContentStore,
    governor_service: ImprovementGovernorService,
    execution_service: ImprovementExecutionService,
    require_api_key: Callable,
) -> APIRouter:
    """改进事项内容子资源（v2.7 §4/§6/§8/§106/§107 P3）：系统理解 / 归因 / 优化方案 / 执行记录 / 来源反馈。"""
    router = APIRouter(prefix="/api", tags=["improvements"], dependencies=[Depends(require_api_key)])

    def _require(improvement_id: str) -> None:
        if improvement_store.get_improvement(improvement_id) is None:
            raise NotFoundError(f"ImprovementItem not found: {improvement_id}")

    _register_feedback_routes(router, improvement_store=improvement_store, content_store=content_store, require=_require)
    _register_nf_routes(router, content_store=content_store, require=_require)
    _register_attr_routes(router, content_store=content_store, require=_require)
    _register_governance_generation_routes(router, improvement_store=improvement_store, governor_service=governor_service, execution_service=execution_service, require=_require)
    _register_opt_routes(router, content_store=content_store, require=_require)
    _register_exec_routes(router, content_store=content_store, require=_require)
    _register_regression_routes(router, content_store=content_store, require=_require)
    return router
