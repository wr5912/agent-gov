from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

from sqlalchemy.orm import sessionmaker

from ..errors import BusinessRuleViolation
from ..improvement_db import AttributionModel, ImprovementFeedbackModel, NormalizedFeedbackModel
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
    extra: dict = field(default_factory=dict)


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
    created_at: str


class ImprovementContentStore:
    """改进事项内容子资源（v2.7 P3）：系统理解 NormalizedFeedback + 归因 Attribution（1:1）+ 来源反馈 Feedback（1:多）。"""

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
    ) -> ImprovementFeedbackRecord:
        clean_summary = (summary or "").strip()
        if not clean_summary:
            raise BusinessRuleViolation("feedback summary cannot be empty")
        fid = f"fb-{uuid4().hex[:12]}"
        now = utc_now()
        with self._session_factory.begin() as db:
            db.add(ImprovementFeedbackModel(
                feedback_id=fid, improvement_id=improvement_id, agent_id=agent_id, summary=clean_summary,
                source=source, status=status, raw_text=raw_text, run_id=run_id, session_id=session_id, created_at=now,
            ))
        return ImprovementFeedbackRecord(
            feedback_id=fid, improvement_id=improvement_id, agent_id=agent_id, summary=clean_summary,
            source=source, status=status, raw_text=raw_text, run_id=run_id, session_id=session_id, created_at=now,
        )

    def list_feedbacks(self, improvement_id: str) -> list[ImprovementFeedbackRecord]:
        with self._session_factory.begin() as db:
            rows = (
                db.query(ImprovementFeedbackModel)
                .filter(ImprovementFeedbackModel.improvement_id == improvement_id)
                .order_by(ImprovementFeedbackModel.created_at, ImprovementFeedbackModel.feedback_id)
                .all()
            )
            return [
                ImprovementFeedbackRecord(
                    feedback_id=r.feedback_id, improvement_id=r.improvement_id, agent_id=r.agent_id, summary=r.summary,
                    source=r.source, status=r.status, raw_text=r.raw_text or "", run_id=r.run_id or "", session_id=r.session_id or "", created_at=r.created_at,
                )
                for r in rows
            ]

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
    def upsert_attribution(self, improvement_id: str, *, summary: str, responsibility_boundary: list[str] | None = None, evidence: list[str] | None = None) -> AttributionRecord:
        now = utc_now()
        with self._session_factory.begin() as db:
            row = db.query(AttributionModel).filter(AttributionModel.improvement_id == improvement_id).one_or_none()
            if row is None:
                row = AttributionModel(attribution_id=f"attr-{uuid4().hex[:12]}", improvement_id=improvement_id, created_at=now)
                db.add(row)
            row.summary = summary
            row.responsibility_boundary_json = list(responsibility_boundary or [])
            row.evidence_json = list(evidence or [])
            row.status = "draft"
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
        status=row.status or "draft",
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
