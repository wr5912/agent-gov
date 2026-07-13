from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.orm import sessionmaker

from ..errors import BusinessRuleViolation, ConflictError, NotFoundError
from ..improvement_db import (
    AttributionModel,
    ExecutionRecordModel,
    ImprovementItemModel,
    NormalizedFeedbackModel,
    OptimizationPlanModel,
    RegressionAssessmentModel,
)
from ..runtime_db import utc_now
from ..state_machines import validate_transition
from .improvement_execution_claim_store import ImprovementExecutionClaimStore
from .improvement_feedback_store import ImprovementFeedbackRecord as ImprovementFeedbackRecord
from .improvement_feedback_store import ImprovementFeedbackStoreMixin
from .improvement_store import advance_improvement_stage_in_transaction

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
    generated_by: str = "heuristic"
    generation_trace_id: str = ""
    generation_trace_url: str = ""


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
    generation_trace_id: str = ""
    generation_trace_url: str = ""
    extra: dict = field(default_factory=dict)
    counter_evidence: list[str] = field(default_factory=list)
    uncertainty_factors: list[str] = field(default_factory=list)
    verification_suggestions: list[str] = field(default_factory=list)


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
    generation_trace_id: str = ""
    generation_trace_url: str = ""


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
    generation_trace_id: str = ""
    generation_trace_url: str = ""


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
    generation_trace_id: str = ""
    generation_trace_url: str = ""
    base_commit_sha: str = ""
    source_optimization_plan_id: str = ""
    source_optimization_plan_updated_at: str = ""
    source_attribution_id: str = ""
    source_attribution_updated_at: str = ""
    claim_token: str = ""
    claim_generation: int = 0
    claim_expires_at: str = ""


class ImprovementContentStore(ImprovementFeedbackStoreMixin):
    """改进事项内容子资源（四阶段改进治理 P3）：系统理解 NormalizedFeedback + 归因 Attribution + 优化方案 OptimizationPlan + 执行记录 ExecutionRecord（均与事项 1:1）+ 来源反馈 Feedback（1:多）。"""

    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory
        self.execution_claims = ImprovementExecutionClaimStore(session_factory, mutable_guard=self._lock_mutable_improvement)

    @staticmethod
    def _lock_mutable_improvement(db: Any, improvement_id: str) -> None:
        """Serialize content writes with archive/delete and reject archived items."""
        locked = db.execute(
            update(ImprovementItemModel)
            .where(
                ImprovementItemModel.improvement_id == improvement_id,
                ImprovementItemModel.improvement_status != "archived",
            )
            .values(updated_at=ImprovementItemModel.updated_at)
        ).rowcount
        if locked == 1:
            return
        item = db.get(ImprovementItemModel, improvement_id)
        if item is not None and item.improvement_status == "archived":
            raise ConflictError(f"Archived improvement cannot be modified: {improvement_id}")

    @staticmethod
    def _require_feedback_intake(item: ImprovementItemModel | None) -> None:
        if item is not None and item.improvement_stage != "feedback_intake":
            raise ConflictError(f"Refine improvement {item.improvement_id} to feedback_intake before changing source feedback")

    @staticmethod
    def _assert_no_execution_claim(db: Any, improvement_id: str, *, resource: str) -> None:
        applying = (
            db.query(ExecutionRecordModel.execution_id)
            .filter(
                ExecutionRecordModel.improvement_id == improvement_id,
                ExecutionRecordModel.status == "applying",
            )
            .first()
        )
        if applying is not None:
            raise ConflictError(f"Cannot modify {resource} while execution is applying: {improvement_id}")

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
        generated_by: str = "heuristic",
        generation_trace_id: str = "",
        generation_trace_url: str = "",
        advance_to_stage: str | None = None,
        item_title: str | None = None,
    ) -> NormalizedFeedbackRecord:
        clean_problem = (problem or "").strip()
        if not clean_problem:
            raise BusinessRuleViolation("normalized feedback problem cannot be empty")
        now = utc_now()
        with self._session_factory.begin() as db:
            self._lock_mutable_improvement(db, improvement_id)
            row = db.query(NormalizedFeedbackModel).filter(NormalizedFeedbackModel.improvement_id == improvement_id).one_or_none()
            if row is None:
                row = NormalizedFeedbackModel(normalized_feedback_id=f"nf-{uuid4().hex[:12]}", improvement_id=improvement_id, created_at=now)
                db.add(row)
            row.problem = clean_problem
            row.possible_reason = possible_reason
            row.possible_object = possible_object
            row.impact = impact
            row.suggestion = suggestion
            row.user_quote = user_quote
            row.status = "draft"
            row.generated_by = generated_by
            row.generation_trace_id = generation_trace_id
            row.generation_trace_url = generation_trace_url
            row.updated_at = now
            if item_title:
                item = db.get(ImprovementItemModel, improvement_id)
                if item is None:
                    raise NotFoundError(f"ImprovementItem not found: {improvement_id}")
                item.title = item_title
                item.updated_at = now
            db.flush()
            if advance_to_stage:
                advance_improvement_stage_in_transaction(db, improvement_id, stage=advance_to_stage)
            return _nf_record(row)

    def get_normalized_feedback(self, improvement_id: str) -> NormalizedFeedbackRecord | None:
        with self._session_factory.begin() as db:
            row = db.query(NormalizedFeedbackModel).filter(NormalizedFeedbackModel.improvement_id == improvement_id).one_or_none()
            return _nf_record(row) if row is not None else None

    def set_normalized_feedback_status(
        self,
        improvement_id: str,
        *,
        status: str,
        advance_to_stage: str | None = None,
    ) -> NormalizedFeedbackRecord:
        if status not in CONTENT_STATUS:
            raise BusinessRuleViolation(f"Unknown status: {status}; expected one of {sorted(CONTENT_STATUS)}")
        with self._session_factory.begin() as db:
            self._lock_mutable_improvement(db, improvement_id)
            row = db.query(NormalizedFeedbackModel).filter(NormalizedFeedbackModel.improvement_id == improvement_id).one_or_none()
            if row is None:
                raise BusinessRuleViolation(f"No normalized feedback for improvement: {improvement_id}")
            row.status = status
            row.updated_at = utc_now()
            if advance_to_stage:
                advance_improvement_stage_in_transaction(db, improvement_id, stage=advance_to_stage)
            return _nf_record(row)

    # ---- Attribution（归因结果）----
    def upsert_attribution(
        self,
        improvement_id: str,
        *,
        summary: str,
        responsibility_boundary: list[str] | None = None,
        evidence: list[str] | None = None,
        counter_evidence: list[str] | None = None,
        uncertainty_factors: list[str] | None = None,
        verification_suggestions: list[str] | None = None,
        generated_by: str = "heuristic",
        generation_trace_id: str = "",
        generation_trace_url: str = "",
        advance_to_stage: str | None = None,
    ) -> AttributionRecord:
        clean_summary = (summary or "").strip()
        if not clean_summary:
            raise BusinessRuleViolation("attribution summary cannot be empty")
        now = utc_now()
        with self._session_factory.begin() as db:
            self._lock_mutable_improvement(db, improvement_id)
            self._assert_no_execution_claim(db, improvement_id, resource="attribution")
            row = db.query(AttributionModel).filter(AttributionModel.improvement_id == improvement_id).one_or_none()
            if row is None:
                row = AttributionModel(attribution_id=f"attr-{uuid4().hex[:12]}", improvement_id=improvement_id, created_at=now)
                db.add(row)
            row.summary = clean_summary
            row.responsibility_boundary_json = list(responsibility_boundary or [])
            row.evidence_json = list(evidence or [])
            row.counter_evidence_json = list(counter_evidence or [])
            row.uncertainty_factors_json = list(uncertainty_factors or [])
            row.verification_suggestions_json = list(verification_suggestions or [])
            row.status = "draft"
            row.generated_by = generated_by
            row.generation_trace_id = generation_trace_id
            row.generation_trace_url = generation_trace_url
            row.updated_at = now
            db.flush()
            if advance_to_stage:
                advance_improvement_stage_in_transaction(db, improvement_id, stage=advance_to_stage)
            return _attr_record(row)

    def get_attribution(self, improvement_id: str) -> AttributionRecord | None:
        with self._session_factory.begin() as db:
            row = db.query(AttributionModel).filter(AttributionModel.improvement_id == improvement_id).one_or_none()
            return _attr_record(row) if row is not None else None

    def set_attribution_status(
        self,
        improvement_id: str,
        *,
        status: str,
        advance_to_stage: str | None = None,
    ) -> AttributionRecord:
        if status not in CONTENT_STATUS:
            raise BusinessRuleViolation(f"Unknown status: {status}; expected one of {sorted(CONTENT_STATUS)}")
        with self._session_factory.begin() as db:
            self._lock_mutable_improvement(db, improvement_id)
            self._assert_no_execution_claim(db, improvement_id, resource="attribution")
            row = db.query(AttributionModel).filter(AttributionModel.improvement_id == improvement_id).one_or_none()
            if row is None:
                raise BusinessRuleViolation(f"No attribution for improvement: {improvement_id}")
            row.status = status
            row.updated_at = utc_now()
            if advance_to_stage:
                advance_improvement_stage_in_transaction(db, improvement_id, stage=advance_to_stage)
            return _attr_record(row)

    # ---- OptimizationPlan（优化方案，§106）----
    def upsert_optimization_plan(
        self,
        improvement_id: str,
        *,
        summary: str,
        changes: list[dict] | None = None,
        risk_level: str = "",
        generated_by: str = "heuristic",
        generation_trace_id: str = "",
        generation_trace_url: str = "",
        advance_to_stage: str | None = None,
    ) -> OptimizationPlanRecord:
        clean_summary = (summary or "").strip()
        if not clean_summary:
            raise BusinessRuleViolation("optimization plan summary cannot be empty")
        clean_changes: list[dict] = []
        for change in changes or []:
            if not isinstance(change, dict):
                raise BusinessRuleViolation("optimization plan changes must be objects")
            target = str(change.get("target") or "").strip()
            description = str(change.get("change") or "").strip()
            if not target or not description:
                raise BusinessRuleViolation("optimization plan changes require non-empty target and change")
            clean_changes.append({**change, "target": target, "change": description})
        if not clean_changes:
            raise BusinessRuleViolation("optimization plan requires at least one change")
        now = utc_now()
        with self._session_factory.begin() as db:
            self._lock_mutable_improvement(db, improvement_id)
            self._assert_no_execution_claim(db, improvement_id, resource="optimization plan")
            row = db.query(OptimizationPlanModel).filter(OptimizationPlanModel.improvement_id == improvement_id).one_or_none()
            if row is None:
                row = OptimizationPlanModel(optimization_plan_id=f"opt-{uuid4().hex[:12]}", improvement_id=improvement_id, created_at=now)
                db.add(row)
            row.summary = clean_summary
            row.changes_json = clean_changes
            row.risk_level = risk_level
            row.status = "draft"
            row.generated_by = generated_by
            row.generation_trace_id = generation_trace_id
            row.generation_trace_url = generation_trace_url
            row.updated_at = now
            db.flush()
            if advance_to_stage:
                advance_improvement_stage_in_transaction(db, improvement_id, stage=advance_to_stage)
            return _opt_record(row)

    def get_optimization_plan(self, improvement_id: str) -> OptimizationPlanRecord | None:
        with self._session_factory.begin() as db:
            row = db.query(OptimizationPlanModel).filter(OptimizationPlanModel.improvement_id == improvement_id).one_or_none()
            return _opt_record(row) if row is not None else None

    def set_optimization_plan_status(
        self,
        improvement_id: str,
        *,
        status: str,
        advance_to_stage: str | None = None,
    ) -> OptimizationPlanRecord:
        if status not in CONTENT_STATUS:
            raise BusinessRuleViolation(f"Unknown status: {status}; expected one of {sorted(CONTENT_STATUS)}")
        with self._session_factory.begin() as db:
            self._lock_mutable_improvement(db, improvement_id)
            self._assert_no_execution_claim(db, improvement_id, resource="optimization plan")
            row = db.query(OptimizationPlanModel).filter(OptimizationPlanModel.improvement_id == improvement_id).one_or_none()
            if row is None:
                raise BusinessRuleViolation(f"No optimization plan for improvement: {improvement_id}")
            row.status = status
            row.updated_at = utc_now()
            if advance_to_stage:
                advance_improvement_stage_in_transaction(db, improvement_id, stage=advance_to_stage)
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
        generation_trace_id: str = "",
        generation_trace_url: str = "",
        advance_to_stage: str | None = None,
    ) -> ExecutionRecord:
        clean_summary = (summary or "").strip()
        if not clean_summary:
            raise BusinessRuleViolation("execution summary cannot be empty")
        now = utc_now()
        with self._session_factory.begin() as db:
            self._lock_mutable_improvement(db, improvement_id)
            row = db.query(ExecutionRecordModel).filter(ExecutionRecordModel.improvement_id == improvement_id).one_or_none()
            if row is None:
                row = ExecutionRecordModel(execution_id=f"exec-{uuid4().hex[:12]}", improvement_id=improvement_id, created_at=now)
                db.add(row)
            elif row.status == "applying":
                raise ConflictError(f"Execution is currently applying: {improvement_id}")
            validate_transition("improvement_execution", row.status, "draft")
            row.summary = clean_summary
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
            row.generation_trace_id = generation_trace_id
            row.generation_trace_url = generation_trace_url
            row.base_commit_sha = ""
            row.source_optimization_plan_id = ""
            row.source_optimization_plan_updated_at = ""
            row.source_attribution_id = ""
            row.source_attribution_updated_at = ""
            row.claim_token = ""
            row.claim_expires_at = ""
            row.updated_at = now
            db.flush()
            if advance_to_stage:
                advance_improvement_stage_in_transaction(db, improvement_id, stage=advance_to_stage)
            return _exec_record(row)

    def get_execution(self, improvement_id: str) -> ExecutionRecord | None:
        with self._session_factory.begin() as db:
            row = db.query(ExecutionRecordModel).filter(ExecutionRecordModel.improvement_id == improvement_id).one_or_none()
            return _exec_record(row) if row is not None else None

    def set_execution_status(
        self,
        improvement_id: str,
        *,
        status: str,
        advance_to_stage: str | None = None,
    ) -> ExecutionRecord:
        if status not in CONTENT_STATUS:
            raise BusinessRuleViolation(f"Unknown status: {status}; expected one of {sorted(CONTENT_STATUS)}")
        with self._session_factory.begin() as db:
            self._lock_mutable_improvement(db, improvement_id)
            row = db.query(ExecutionRecordModel).filter(ExecutionRecordModel.improvement_id == improvement_id).one_or_none()
            if row is None:
                raise BusinessRuleViolation(f"No execution record for improvement: {improvement_id}")
            validate_transition("improvement_execution", row.status, status)
            row.status = status
            row.updated_at = utc_now()
            if advance_to_stage:
                advance_improvement_stage_in_transaction(db, improvement_id, stage=advance_to_stage)
            return _exec_record(row)

    # ---- RegressionAssessment（回归保障评估，§11/§17.5）----
    def upsert_regression_assessment(
        self,
        improvement_id: str,
        *,
        summary: str,
        cases: list[dict] | None = None,
        suggested_gate_thresholds: dict | None = None,
        generated_by: str = "heuristic",
        generation_trace_id: str = "",
        generation_trace_url: str = "",
        advance_to_stage: str | None = None,
    ) -> RegressionAssessmentRecord:
        clean_summary = (summary or "").strip()
        if not clean_summary:
            raise BusinessRuleViolation("regression assessment summary cannot be empty")
        clean_cases: list[dict] = []
        for case in cases or []:
            if not isinstance(case, dict):
                raise BusinessRuleViolation("regression assessment cases must be objects")
            prompt = str(case.get("prompt") or "").strip()
            if not prompt:
                raise BusinessRuleViolation("regression assessment cases require a non-empty prompt")
            clean_cases.append({**case, "prompt": prompt})
        if not clean_cases:
            raise BusinessRuleViolation("regression assessment requires at least one case")
        now = utc_now()
        with self._session_factory.begin() as db:
            self._lock_mutable_improvement(db, improvement_id)
            row = db.query(RegressionAssessmentModel).filter(RegressionAssessmentModel.improvement_id == improvement_id).one_or_none()
            if row is None:
                row = RegressionAssessmentModel(regression_assessment_id=f"reg-{uuid4().hex[:12]}", improvement_id=improvement_id, created_at=now)
                db.add(row)
            row.summary = clean_summary
            row.cases_json = clean_cases
            row.suggested_gate_thresholds_json = dict(suggested_gate_thresholds or {})
            row.status = "draft"
            row.generated_by = generated_by
            row.generation_trace_id = generation_trace_id
            row.generation_trace_url = generation_trace_url
            row.updated_at = now
            db.flush()
            if advance_to_stage:
                advance_improvement_stage_in_transaction(db, improvement_id, stage=advance_to_stage)
            return _reg_record(row)

    def get_regression_assessment(self, improvement_id: str) -> RegressionAssessmentRecord | None:
        with self._session_factory.begin() as db:
            row = db.query(RegressionAssessmentModel).filter(RegressionAssessmentModel.improvement_id == improvement_id).one_or_none()
            return _reg_record(row) if row is not None else None

    def set_regression_assessment_status(
        self,
        improvement_id: str,
        *,
        status: str,
        advance_to_stage: str | None = None,
    ) -> RegressionAssessmentRecord:
        if status not in CONTENT_STATUS:
            raise BusinessRuleViolation(f"Unknown status: {status}; expected one of {sorted(CONTENT_STATUS)}")
        with self._session_factory.begin() as db:
            self._lock_mutable_improvement(db, improvement_id)
            row = db.query(RegressionAssessmentModel).filter(RegressionAssessmentModel.improvement_id == improvement_id).one_or_none()
            if row is None:
                raise BusinessRuleViolation(f"No regression assessment for improvement: {improvement_id}")
            row.status = status
            row.updated_at = utc_now()
            if advance_to_stage:
                advance_improvement_stage_in_transaction(db, improvement_id, stage=advance_to_stage)
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
        generation_trace_id=row.generation_trace_id or "",
        generation_trace_url=row.generation_trace_url or "",
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
        generation_trace_id=row.generation_trace_id or "",
        generation_trace_url=row.generation_trace_url or "",
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
        generation_trace_id=row.generation_trace_id or "",
        generation_trace_url=row.generation_trace_url or "",
        base_commit_sha=row.base_commit_sha or "",
        source_optimization_plan_id=row.source_optimization_plan_id or "",
        source_optimization_plan_updated_at=row.source_optimization_plan_updated_at or "",
        source_attribution_id=row.source_attribution_id or "",
        source_attribution_updated_at=row.source_attribution_updated_at or "",
        claim_token=row.claim_token or "",
        claim_generation=int(row.claim_generation or 0),
        claim_expires_at=row.claim_expires_at or "",
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
        generated_by=row.generated_by or "heuristic",
        generation_trace_id=row.generation_trace_id or "",
        generation_trace_url=row.generation_trace_url or "",
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
        generation_trace_id=row.generation_trace_id or "",
        generation_trace_url=row.generation_trace_url or "",
    )
