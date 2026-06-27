from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

from sqlalchemy.orm import sessionmaker

from ..errors import BusinessRuleViolation, NotFoundError
from ..improvement_db import (
    AttributionModel,
    ExecutionRecordModel,
    ImprovementFeedbackModel,
    NormalizedFeedbackModel,
    OptimizationPlanModel,
    RegressionAssessmentModel,
)
from ..runtime_db import utc_now

CONTENT_STATUS = {"draft", "confirmed"}


@dataclass(frozen=True)
class NormalizedFeedbackRecord:
    normalized_feedback_id: str
    improvement_id: str
    problem: str
    possible_reason: str
    possible_object: str
    impact: str
    suggestion: str
    user_quote: str
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class AttributionRecord:
    attribution_id: str
    improvement_id: str
    summary: str
    responsibility_boundary: list[str]
    evidence: list[str]
    status: str
    created_at: str
    updated_at: str = ""
    generated_by: str = "heuristic"
    extra: dict = field(default_factory=dict)
    counter_evidence: list[str] = field(default_factory=list)
    uncertainty_factors: list[str] = field(default_factory=list)
    verification_suggestions: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ImprovementFeedbackRecord:
    feedback_id: str
    improvement_id: str
    agent_id: str
    summary: str
    source: str
    status: str
    raw_text: str
    run_id: str
    session_id: str
    agent_version_id: str
    scenario: str
    task_id: str
    alert_id: str
    case_id: str
    created_at: str


@dataclass(frozen=True)
class OptimizationPlanRecord:
    optimization_plan_id: str
    improvement_id: str
    summary: str
    changes: list[dict]
    status: str
    created_at: str
    updated_at: str = ""
    generated_by: str = "heuristic"
    risk_level: str = ""


@dataclass(frozen=True)
class RegressionAssessmentRecord:
    regression_assessment_id: str
    improvement_id: str
    summary: str
    cases: list[dict]
    status: str
    created_at: str
    updated_at: str = ""
    generated_by: str = "heuristic"
    suggested_gate_thresholds: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionRecord:
    execution_id: str
    improvement_id: str
    summary: str
    changes_applied: list[str]
    agent_version: str
    status: str
    created_at: str
    updated_at: str = ""
    generated_by: str = "heuristic"
    change_set_id: str = ""
    applied_agent_version_id: str = ""
    applied_diff: dict = field(default_factory=dict)
    risk_level: str = ""
    rollback_strategy: str = ""
    rollback_instructions: list[str] = field(default_factory=list)


class ImprovementContentStore:
    """改进事项内容子资源（v2.7 P3）：系统理解 NormalizedFeedback + 归因 Attribution + 优化方案 OptimizationPlan + 执行记录 ExecutionRecord（均与事项 1:1）+ 来源反馈 Feedback（1:多）。"""

    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    # ---- Feedback（来源反馈，§8.4）----
    def create_feedback(
        self,
        improvement_id: str,
        *,
        agent_id: str = "main-agent",
        summary: str,
        source: str = "playground_run",
        status: str = "merged",
        raw_text: str = "",
        run_id: str = "",
        session_id: str = "",
        agent_version_id: str = "",
        scenario: str = "",
        task_id: str = "",
        alert_id: str = "",
        case_id: str = "",
    ) -> ImprovementFeedbackRecord:
        clean_summary = (summary or "").strip()
        if not clean_summary:
            raise BusinessRuleViolation("feedback summary cannot be empty")
        fid = f"fb-{uuid4().hex[:12]}"
        now = utc_now()
        with self._session_factory.begin() as db:
            db.add(ImprovementFeedbackModel(
                feedback_id=fid, improvement_id=improvement_id, agent_id=agent_id, summary=clean_summary,
                source=source, status=status, raw_text=raw_text, run_id=run_id, session_id=session_id,
                agent_version_id=agent_version_id, scenario=scenario, task_id=task_id, alert_id=alert_id, case_id=case_id,
                created_at=now,
            ))
        return ImprovementFeedbackRecord(
            feedback_id=fid, improvement_id=improvement_id, agent_id=agent_id, summary=clean_summary,
            source=source, status=status, raw_text=raw_text, run_id=run_id, session_id=session_id,
            agent_version_id=agent_version_id, scenario=scenario, task_id=task_id, alert_id=alert_id, case_id=case_id,
            created_at=now,
        )

    def list_feedbacks(self, improvement_id: str) -> list[ImprovementFeedbackRecord]:
        with self._session_factory.begin() as db:
            rows = (
                db.query(ImprovementFeedbackModel)
                .filter(ImprovementFeedbackModel.improvement_id == improvement_id)
                .order_by(ImprovementFeedbackModel.created_at, ImprovementFeedbackModel.feedback_id)
                .all()
            )
            return [_fb_record(r) for r in rows]

    def reassign_feedback(self, feedback_id: str, *, target_improvement_id: str) -> ImprovementFeedbackRecord:
        """把一条来源反馈从当前事项移动到另一个改进事项（跨事项调整）。目标存在性/同 agent 由调用方校验。"""
        clean_target = (target_improvement_id or "").strip()
        if not clean_target:
            raise BusinessRuleViolation("target_improvement_id is required")
        with self._session_factory.begin() as db:
            row = db.get(ImprovementFeedbackModel, feedback_id)
            if row is None:
                raise NotFoundError(f"Feedback not found: {feedback_id}")
            if row.improvement_id == clean_target:
                raise BusinessRuleViolation("Feedback already belongs to the target improvement")
            row.improvement_id = clean_target
            return _fb_record(row)

    def count_feedbacks(self, improvement_id: str) -> int:
        with self._session_factory.begin() as db:
            return db.query(ImprovementFeedbackModel).filter(ImprovementFeedbackModel.improvement_id == improvement_id).count()

    def list_attachable_feedbacks(self, *, agent_id: str, exclude_improvement_id: str) -> list[ImprovementFeedbackRecord]:
        """其他改进事项中、同一业务 Agent 的来源反馈——供「从其他事项调整过来」选择。"""
        with self._session_factory.begin() as db:
            rows = (
                db.query(ImprovementFeedbackModel)
                .filter(
                    ImprovementFeedbackModel.agent_id == agent_id,
                    ImprovementFeedbackModel.improvement_id != exclude_improvement_id,
                )
                .order_by(ImprovementFeedbackModel.created_at.desc(), ImprovementFeedbackModel.feedback_id)
                .all()
            )
            return [_fb_record(r) for r in rows]

    # ---- NormalizedFeedback（系统理解）----
    def upsert_normalized_feedback(
        self,
        improvement_id: str,
        *,
        problem: str,
        possible_reason: str = "",
        possible_object: str = "",
        impact: str = "",
        suggestion: str = "",
        user_quote: str = "",
    ) -> NormalizedFeedbackRecord:
        now = utc_now()
        with self._session_factory.begin() as db:
            row = db.query(NormalizedFeedbackModel).filter(NormalizedFeedbackModel.improvement_id == improvement_id).one_or_none()
            if row is None:
                row = NormalizedFeedbackModel(normalized_feedback_id=f"nf-{uuid4().hex[:12]}", improvement_id=improvement_id, created_at=now)
                db.add(row)
            row.problem = problem
            row.possible_reason = possible_reason
            row.possible_object = possible_object
            row.impact = impact
            row.suggestion = suggestion
            row.user_quote = user_quote
            row.status = "draft"
            row.updated_at = now
            db.flush()
            return _nf_record(row)

    def get_normalized_feedback(self, improvement_id: str) -> NormalizedFeedbackRecord | None:
        with self._session_factory.begin() as db:
            row = db.query(NormalizedFeedbackModel).filter(NormalizedFeedbackModel.improvement_id == improvement_id).one_or_none()
            return _nf_record(row) if row is not None else None

    def set_normalized_feedback_status(self, improvement_id: str, *, status: str) -> NormalizedFeedbackRecord:
        if status not in CONTENT_STATUS:
            raise BusinessRuleViolation(f"Unknown status: {status}; expected one of {sorted(CONTENT_STATUS)}")
        with self._session_factory.begin() as db:
            row = db.query(NormalizedFeedbackModel).filter(NormalizedFeedbackModel.improvement_id == improvement_id).one_or_none()
            if row is None:
                raise BusinessRuleViolation(f"No normalized feedback for improvement: {improvement_id}")
            row.status = status
            row.updated_at = utc_now()
            return _nf_record(row)

    # ---- Attribution（归因结果）----
    def upsert_attribution(self, improvement_id: str, *, summary: str, responsibility_boundary: list[str] | None = None, evidence: list[str] | None = None, counter_evidence: list[str] | None = None, uncertainty_factors: list[str] | None = None, verification_suggestions: list[str] | None = None, generated_by: str = "heuristic") -> AttributionRecord:
        now = utc_now()
        with self._session_factory.begin() as db:
            row = db.query(AttributionModel).filter(AttributionModel.improvement_id == improvement_id).one_or_none()
            if row is None:
                row = AttributionModel(attribution_id=f"attr-{uuid4().hex[:12]}", improvement_id=improvement_id, created_at=now)
                db.add(row)
            row.summary = summary
            row.responsibility_boundary_json = list(responsibility_boundary or [])
            row.evidence_json = list(evidence or [])
            row.counter_evidence_json = list(counter_evidence or [])
            row.uncertainty_factors_json = list(uncertainty_factors or [])
            row.verification_suggestions_json = list(verification_suggestions or [])
            row.status = "draft"
            row.generated_by = generated_by
            row.updated_at = now
            db.flush()
            return _attr_record(row)

    def get_attribution(self, improvement_id: str) -> AttributionRecord | None:
        with self._session_factory.begin() as db:
            row = db.query(AttributionModel).filter(AttributionModel.improvement_id == improvement_id).one_or_none()
            return _attr_record(row) if row is not None else None

    def set_attribution_status(self, improvement_id: str, *, status: str) -> AttributionRecord:
        if status not in CONTENT_STATUS:
            raise BusinessRuleViolation(f"Unknown status: {status}; expected one of {sorted(CONTENT_STATUS)}")
        with self._session_factory.begin() as db:
            row = db.query(AttributionModel).filter(AttributionModel.improvement_id == improvement_id).one_or_none()
            if row is None:
                raise BusinessRuleViolation(f"No attribution for improvement: {improvement_id}")
            row.status = status
            row.updated_at = utc_now()
            return _attr_record(row)

    # ---- OptimizationPlan（优化方案，§106）----
    def upsert_optimization_plan(self, improvement_id: str, *, summary: str, changes: list[dict] | None = None, risk_level: str = "", generated_by: str = "heuristic") -> OptimizationPlanRecord:
        now = utc_now()
        with self._session_factory.begin() as db:
            row = db.query(OptimizationPlanModel).filter(OptimizationPlanModel.improvement_id == improvement_id).one_or_none()
            if row is None:
                row = OptimizationPlanModel(optimization_plan_id=f"opt-{uuid4().hex[:12]}", improvement_id=improvement_id, created_at=now)
                db.add(row)
            row.summary = summary
            row.changes_json = list(changes or [])
            row.risk_level = risk_level
            row.status = "draft"
            row.generated_by = generated_by
            row.updated_at = now
            db.flush()
            return _opt_record(row)

    def get_optimization_plan(self, improvement_id: str) -> OptimizationPlanRecord | None:
        with self._session_factory.begin() as db:
            row = db.query(OptimizationPlanModel).filter(OptimizationPlanModel.improvement_id == improvement_id).one_or_none()
            return _opt_record(row) if row is not None else None

    def set_optimization_plan_status(self, improvement_id: str, *, status: str) -> OptimizationPlanRecord:
        if status not in CONTENT_STATUS:
            raise BusinessRuleViolation(f"Unknown status: {status}; expected one of {sorted(CONTENT_STATUS)}")
        with self._session_factory.begin() as db:
            row = db.query(OptimizationPlanModel).filter(OptimizationPlanModel.improvement_id == improvement_id).one_or_none()
            if row is None:
                raise BusinessRuleViolation(f"No optimization plan for improvement: {improvement_id}")
            row.status = status
            row.updated_at = utc_now()
            return _opt_record(row)

    # ---- ExecutionRecord（执行记录，§107）----
    def upsert_execution(
        self,
        improvement_id: str,
        *,
        summary: str,
        changes_applied: list[str] | None = None,
        agent_version: str = "",
        generated_by: str = "heuristic",
        change_set_id: str = "",
        applied_agent_version_id: str = "",
        applied_diff: dict | None = None,
        risk_level: str = "",
        rollback_strategy: str = "",
        rollback_instructions: list[str] | None = None,
    ) -> ExecutionRecord:
        now = utc_now()
        with self._session_factory.begin() as db:
            row = db.query(ExecutionRecordModel).filter(ExecutionRecordModel.improvement_id == improvement_id).one_or_none()
            if row is None:
                row = ExecutionRecordModel(execution_id=f"exec-{uuid4().hex[:12]}", improvement_id=improvement_id, created_at=now)
                db.add(row)
            row.summary = summary
            row.changes_applied_json = list(changes_applied or [])
            row.agent_version = agent_version
            row.status = "draft"
            row.generated_by = generated_by
            row.change_set_id = change_set_id
            row.applied_agent_version_id = applied_agent_version_id
            row.applied_diff_json = dict(applied_diff or {})
            row.risk_level = risk_level
            row.rollback_strategy = rollback_strategy
            row.rollback_instructions_json = list(rollback_instructions or [])
            row.updated_at = now
            db.flush()
            return _exec_record(row)

    def get_execution(self, improvement_id: str) -> ExecutionRecord | None:
        with self._session_factory.begin() as db:
            row = db.query(ExecutionRecordModel).filter(ExecutionRecordModel.improvement_id == improvement_id).one_or_none()
            return _exec_record(row) if row is not None else None

    def set_execution_status(self, improvement_id: str, *, status: str) -> ExecutionRecord:
        if status not in CONTENT_STATUS:
            raise BusinessRuleViolation(f"Unknown status: {status}; expected one of {sorted(CONTENT_STATUS)}")
        with self._session_factory.begin() as db:
            row = db.query(ExecutionRecordModel).filter(ExecutionRecordModel.improvement_id == improvement_id).one_or_none()
            if row is None:
                raise BusinessRuleViolation(f"No execution record for improvement: {improvement_id}")
            row.status = status
            row.updated_at = utc_now()
            return _exec_record(row)

    # ---- RegressionAssessment（回归保障评估，§11/§17.5）----
    def upsert_regression_assessment(self, improvement_id: str, *, summary: str, cases: list[dict] | None = None, suggested_gate_thresholds: dict | None = None, generated_by: str = "heuristic") -> RegressionAssessmentRecord:
        now = utc_now()
        with self._session_factory.begin() as db:
            row = db.query(RegressionAssessmentModel).filter(RegressionAssessmentModel.improvement_id == improvement_id).one_or_none()
            if row is None:
                row = RegressionAssessmentModel(regression_assessment_id=f"reg-{uuid4().hex[:12]}", improvement_id=improvement_id, created_at=now)
                db.add(row)
            row.summary = summary
            row.cases_json = list(cases or [])
            row.suggested_gate_thresholds_json = dict(suggested_gate_thresholds or {})
            row.status = "draft"
            row.generated_by = generated_by
            row.updated_at = now
            db.flush()
            return _reg_record(row)

    def get_regression_assessment(self, improvement_id: str) -> RegressionAssessmentRecord | None:
        with self._session_factory.begin() as db:
            row = db.query(RegressionAssessmentModel).filter(RegressionAssessmentModel.improvement_id == improvement_id).one_or_none()
            return _reg_record(row) if row is not None else None

    def set_regression_assessment_status(self, improvement_id: str, *, status: str) -> RegressionAssessmentRecord:
        if status not in CONTENT_STATUS:
            raise BusinessRuleViolation(f"Unknown status: {status}; expected one of {sorted(CONTENT_STATUS)}")
        with self._session_factory.begin() as db:
            row = db.query(RegressionAssessmentModel).filter(RegressionAssessmentModel.improvement_id == improvement_id).one_or_none()
            if row is None:
                raise BusinessRuleViolation(f"No regression assessment for improvement: {improvement_id}")
            row.status = status
            row.updated_at = utc_now()
            return _reg_record(row)


def _reg_record(row: RegressionAssessmentModel) -> RegressionAssessmentRecord:
    return RegressionAssessmentRecord(
        regression_assessment_id=row.regression_assessment_id,
        improvement_id=row.improvement_id,
        summary=row.summary or "",
        cases=list(row.cases_json or []),
        suggested_gate_thresholds=dict(row.suggested_gate_thresholds_json or {}),
        status=row.status or "draft",
        created_at=row.created_at,
        updated_at=row.updated_at,
        generated_by=row.generated_by or "heuristic",
    )


def _opt_record(row: OptimizationPlanModel) -> OptimizationPlanRecord:
    return OptimizationPlanRecord(
        optimization_plan_id=row.optimization_plan_id,
        improvement_id=row.improvement_id,
        summary=row.summary or "",
        changes=list(row.changes_json or []),
        risk_level=row.risk_level or "",
        status=row.status or "draft",
        created_at=row.created_at,
        updated_at=row.updated_at,
        generated_by=row.generated_by or "heuristic",
    )


def _exec_record(row: ExecutionRecordModel) -> ExecutionRecord:
    return ExecutionRecord(
        execution_id=row.execution_id,
        improvement_id=row.improvement_id,
        summary=row.summary or "",
        changes_applied=list(row.changes_applied_json or []),
        agent_version=row.agent_version or "",
        status=row.status or "draft",
        created_at=row.created_at,
        updated_at=row.updated_at,
        generated_by=row.generated_by or "heuristic",
        change_set_id=row.change_set_id or "",
        applied_agent_version_id=row.applied_agent_version_id or "",
        applied_diff=dict(row.applied_diff_json or {}),
        risk_level=row.risk_level or "",
        rollback_strategy=row.rollback_strategy or "",
        rollback_instructions=list(row.rollback_instructions_json or []),
    )


def _nf_record(row: NormalizedFeedbackModel) -> NormalizedFeedbackRecord:
    return NormalizedFeedbackRecord(
        normalized_feedback_id=row.normalized_feedback_id,
        improvement_id=row.improvement_id,
        problem=row.problem or "",
        possible_reason=row.possible_reason or "",
        possible_object=row.possible_object or "",
        impact=row.impact or "",
        suggestion=row.suggestion or "",
        user_quote=row.user_quote or "",
        status=row.status or "draft",
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _attr_record(row: AttributionModel) -> AttributionRecord:
    return AttributionRecord(
        attribution_id=row.attribution_id,
        improvement_id=row.improvement_id,
        summary=row.summary or "",
        responsibility_boundary=list(row.responsibility_boundary_json or []),
        evidence=list(row.evidence_json or []),
        counter_evidence=list(row.counter_evidence_json or []),
        uncertainty_factors=list(row.uncertainty_factors_json or []),
        verification_suggestions=list(row.verification_suggestions_json or []),
        status=row.status or "draft",
        created_at=row.created_at,
        updated_at=row.updated_at,
        generated_by=row.generated_by or "heuristic",
    )


def _fb_record(row: ImprovementFeedbackModel) -> ImprovementFeedbackRecord:
    return ImprovementFeedbackRecord(
        feedback_id=row.feedback_id, improvement_id=row.improvement_id, agent_id=row.agent_id, summary=row.summary,
        source=row.source, status=row.status, raw_text=row.raw_text or "", run_id=row.run_id or "",
        session_id=row.session_id or "", agent_version_id=row.agent_version_id or "", scenario=row.scenario or "",
        task_id=row.task_id or "", alert_id=row.alert_id or "", case_id=row.case_id or "", created_at=row.created_at,
    )
