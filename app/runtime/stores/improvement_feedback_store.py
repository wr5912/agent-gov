from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from ..errors import BusinessRuleViolation, ConflictError, NotFoundError
from ..improvement_db import (
    ImprovementFeedbackCaseAssignmentModel,
    ImprovementFeedbackModel,
    ImprovementItemModel,
)
from ..improvement_feedback_contract import (
    FEEDBACK_CASE_ATTACH_ONLY_MESSAGE,
    FEEDBACK_CASE_SOURCE,
    has_feedback_case_semantics,
)
from ..runtime_db import utc_now


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


class ImprovementFeedbackStoreMixin:
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
        if has_feedback_case_semantics(source=source, case_id=case_id):
            raise BusinessRuleViolation(FEEDBACK_CASE_ATTACH_ONLY_MESSAGE)
        clean_summary = (summary or "").strip()
        if not clean_summary:
            raise BusinessRuleViolation("feedback summary cannot be empty")
        feedback_id = f"fb-{uuid4().hex[:12]}"
        now = utc_now()
        with self._session_factory.begin() as db:
            self._lock_mutable_improvement(db, improvement_id)
            self._require_feedback_intake(db.get(ImprovementItemModel, improvement_id))
            row = ImprovementFeedbackModel(
                feedback_id=feedback_id,
                improvement_id=improvement_id,
                agent_id=agent_id,
                summary=clean_summary,
                source=source,
                status=status,
                raw_text=raw_text,
                run_id=run_id,
                session_id=session_id,
                agent_version_id=agent_version_id,
                scenario=scenario,
                task_id=task_id,
                alert_id=alert_id,
                case_id=case_id,
                created_at=now,
            )
            db.add(row)
            db.flush()
            return _feedback_record(row)

    def list_feedbacks(self, improvement_id: str) -> list[ImprovementFeedbackRecord]:
        with self._session_factory.begin() as db:
            rows = (
                db.query(ImprovementFeedbackModel)
                .filter(ImprovementFeedbackModel.improvement_id == improvement_id)
                .order_by(ImprovementFeedbackModel.created_at, ImprovementFeedbackModel.feedback_id)
                .all()
            )
            return [_feedback_record(row) for row in rows]

    def attach_feedback_case(
        self,
        improvement_id: str,
        *,
        agent_id: str,
        feedback_case_id: str,
        summary: str,
        run_id: str = "",
    ) -> ImprovementFeedbackRecord:
        clean_case_id = (feedback_case_id or "").strip()
        clean_summary = (summary or "").strip()
        if not clean_case_id:
            raise BusinessRuleViolation("feedback_case_id is required")
        if not clean_summary:
            raise BusinessRuleViolation("feedback summary cannot be empty")
        feedback_id = f"fb-{uuid4().hex[:12]}"
        now = utc_now()
        try:
            with self._session_factory.begin() as db:
                self._lock_mutable_improvement(db, improvement_id)
                item = db.get(ImprovementItemModel, improvement_id)
                if item is None:
                    raise NotFoundError(f"ImprovementItem not found: {improvement_id}")
                self._require_feedback_intake(item)
                if item.agent_id != agent_id:
                    raise BusinessRuleViolation("Cannot attach feedback case across different business agents")
                existing = db.get(ImprovementFeedbackCaseAssignmentModel, clean_case_id)
                if existing is not None:
                    raise ConflictError(f"FeedbackCase {clean_case_id} is already assigned to improvement {existing.improvement_id}")
                row = ImprovementFeedbackModel(
                    feedback_id=feedback_id,
                    improvement_id=improvement_id,
                    agent_id=agent_id,
                    summary=clean_summary,
                    source=FEEDBACK_CASE_SOURCE,
                    status="merged",
                    run_id=run_id,
                    case_id=clean_case_id,
                    created_at=now,
                )
                db.add(row)
                db.add(
                    ImprovementFeedbackCaseAssignmentModel(
                        feedback_case_id=clean_case_id,
                        improvement_id=improvement_id,
                        feedback_id=feedback_id,
                        agent_id=agent_id,
                        created_at=now,
                    )
                )
                refs = list(item.source_feedback_refs_json or [])
                if clean_case_id not in refs:
                    refs.append(clean_case_id)
                    item.source_feedback_refs_json = refs
                    item.updated_at = now
                db.flush()
                return _feedback_record(row)
        except IntegrityError as exc:
            raise ConflictError(f"FeedbackCase {clean_case_id} was assigned concurrently") from exc

    def reassign_feedback(
        self,
        feedback_id: str,
        *,
        source_improvement_id: str,
        target_improvement_id: str,
    ) -> ImprovementFeedbackRecord:
        clean_source = (source_improvement_id or "").strip()
        clean_target = (target_improvement_id or "").strip()
        if not clean_source:
            raise BusinessRuleViolation("source_improvement_id is required")
        if not clean_target:
            raise BusinessRuleViolation("target_improvement_id is required")
        if clean_source == clean_target:
            raise BusinessRuleViolation("Feedback already belongs to the target improvement")
        with self._session_factory.begin() as db:
            self._lock_mutable_improvement(db, clean_source)
            self._lock_mutable_improvement(db, clean_target)
            source = db.get(ImprovementItemModel, clean_source)
            target = db.get(ImprovementItemModel, clean_target)
            if source is None or target is None:
                raise NotFoundError("ImprovementItem not found")
            self._require_feedback_intake(source)
            self._require_feedback_intake(target)
            if source.agent_id != target.agent_id:
                raise BusinessRuleViolation("Cannot reassign feedback across different business agents")
            moved = db.execute(
                update(ImprovementFeedbackModel)
                .where(
                    ImprovementFeedbackModel.feedback_id == feedback_id,
                    ImprovementFeedbackModel.improvement_id == clean_source,
                    ImprovementFeedbackModel.agent_id == source.agent_id,
                )
                .values(improvement_id=clean_target)
            ).rowcount
            row = db.get(ImprovementFeedbackModel, feedback_id)
            if row is None:
                raise NotFoundError(f"Feedback not found: {feedback_id}")
            if moved != 1:
                if row.agent_id != source.agent_id:
                    raise BusinessRuleViolation("Cannot reassign feedback across different business agents")
                raise ConflictError("Feedback does not belong to the source improvement")
            assignment = (
                db.query(ImprovementFeedbackCaseAssignmentModel).filter(ImprovementFeedbackCaseAssignmentModel.feedback_id == feedback_id).one_or_none()
            )
            if assignment is not None:
                if assignment.improvement_id != clean_source or assignment.agent_id != source.agent_id:
                    raise ConflictError("FeedbackCase assignment does not match the source feedback")
                assignment.improvement_id = clean_target
                source.source_feedback_refs_json = [ref for ref in (source.source_feedback_refs_json or []) if ref != assignment.feedback_case_id]
                target_refs = list(target.source_feedback_refs_json or [])
                if assignment.feedback_case_id not in target_refs:
                    target_refs.append(assignment.feedback_case_id)
                target.source_feedback_refs_json = target_refs
                source.updated_at = utc_now()
                target.updated_at = source.updated_at
            return _feedback_record(row)

    def count_feedbacks(self, improvement_id: str) -> int:
        with self._session_factory.begin() as db:
            return db.query(ImprovementFeedbackModel).filter(ImprovementFeedbackModel.improvement_id == improvement_id).count()

    def list_attachable_feedbacks(
        self,
        *,
        agent_id: str,
        exclude_improvement_id: str,
    ) -> list[ImprovementFeedbackRecord]:
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
            return [_feedback_record(row) for row in rows]


def _feedback_record(row: ImprovementFeedbackModel) -> ImprovementFeedbackRecord:
    return ImprovementFeedbackRecord(
        feedback_id=row.feedback_id,
        improvement_id=row.improvement_id,
        agent_id=row.agent_id,
        summary=row.summary,
        source=row.source,
        status=row.status,
        raw_text=row.raw_text or "",
        run_id=row.run_id or "",
        session_id=row.session_id or "",
        agent_version_id=row.agent_version_id or "",
        scenario=row.scenario or "",
        task_id=row.task_id or "",
        alert_id=row.alert_id or "",
        case_id=row.case_id or "",
        created_at=row.created_at,
    )
