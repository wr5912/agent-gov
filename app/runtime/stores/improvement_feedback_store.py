from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TypedDict
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..errors import BusinessRuleViolation, ConflictError, DataIntegrityError, NotFoundError
from ..feedback_entities import FeedbackEntities, parse_entities
from ..improvement_db import (
    ImprovementFeedbackCaseAssignmentModel,
    ImprovementFeedbackModel,
    ImprovementIdempotencyOperationModel,
    ImprovementItemModel,
)
from ..improvement_feedback_contract import (
    FEEDBACK_CASE_ATTACH_ONLY_MESSAGE,
    FEEDBACK_CASE_SOURCE,
    is_feedback_case_source,
)
from ..improvement_idempotency import (
    CREATE_IMPROVEMENT_FEEDBACK_OPERATION,
    bind_idempotency_operation,
    complete_idempotency_operation,
    feedback_create_request_fingerprint,
    normalize_idempotency_key,
)
from ..protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from ..runtime_db import AgentRunModel, FeedbackCaseModel, FeedbackEventModel, utc_now


@dataclass(frozen=True)
class ImprovementFeedbackSourceEvent:
    event_id: str
    source_system: str
    event_type: str


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
    entities: FeedbackEntities
    feedback_case_id: str | None
    source_events: list[ImprovementFeedbackSourceEvent]
    created_at: str


class _FeedbackCreateValues(TypedDict):
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
    entities_json: FeedbackEntities
    created_at: str


def _prepare_feedback_create_values(
    improvement_id: str,
    *,
    agent_id: str,
    summary: str,
    source: str,
    status: str,
    raw_text: str,
    run_id: str,
    session_id: str,
    agent_version_id: str,
    scenario: str,
    task_id: str,
    entities: Mapping[str, list[str]] | None,
) -> _FeedbackCreateValues:
    if is_feedback_case_source(source):
        raise BusinessRuleViolation(FEEDBACK_CASE_ATTACH_ONLY_MESSAGE)
    try:
        validated_entities = parse_entities(entities)
    except ValidationError as exc:
        raise BusinessRuleViolation("Invalid feedback entities") from exc
    clean_summary = (summary or "").strip()
    if not clean_summary:
        raise BusinessRuleViolation("feedback summary cannot be empty")
    return {
        "feedback_id": f"fb-{uuid4().hex[:12]}",
        "improvement_id": improvement_id,
        "agent_id": agent_id,
        "summary": clean_summary,
        "source": source,
        "status": status,
        "raw_text": raw_text,
        "run_id": run_id,
        "session_id": session_id,
        "agent_version_id": agent_version_id,
        "scenario": scenario,
        "task_id": task_id,
        "entities_json": validated_entities,
        "created_at": utc_now(),
    }


def _claim_feedback_create_or_replay_in_transaction(
    db: Session,
    *,
    key: str | None,
    values: _FeedbackCreateValues,
) -> tuple[ImprovementIdempotencyOperationModel | None, ImprovementFeedbackRecord | None]:
    if key is None:
        return None, None
    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    ledger, replayed = bind_idempotency_operation(
        db,
        operation_kind=CREATE_IMPROVEMENT_FEEDBACK_OPERATION,
        key=key,
        request_fingerprint=feedback_create_request_fingerprint(
            improvement_id=values["improvement_id"],
            summary=values["summary"],
            source=values["source"],
            status=values["status"],
            raw_text=values["raw_text"],
            run_id=values["run_id"],
            session_id=values["session_id"],
            agent_version_id=values["agent_version_id"],
            scenario=values["scenario"],
            task_id=values["task_id"],
            entities=values["entities_json"],
        ),
    )
    if not replayed:
        return ledger, None
    existing = db.get(ImprovementFeedbackModel, ledger.result_resource_id)
    if existing is None:
        raise DataIntegrityError("Idempotent improvement feedback result is missing")
    return ledger, _feedback_records(db, [existing])[0]


def _require_feedback_run_owner(db: Session, *, run_id: str, agent_id: str) -> None:
    clean_run_id = (run_id or "").strip()
    if not clean_run_id:
        return
    run = db.get(AgentRunModel, clean_run_id)
    if run is None:
        raise NotFoundError(f"AgentRun not found: {clean_run_id}")
    if run.agent_id != agent_id:
        raise BusinessRuleViolation("Cannot bind improvement feedback to a run owned by a different business agent")


class ImprovementFeedbackStoreMixin:
    def create_feedback(
        self,
        improvement_id: str,
        *,
        agent_id: str = DEFAULT_BUSINESS_AGENT_ID,
        summary: str,
        source: str = "playground_run",
        status: str = "merged",
        raw_text: str = "",
        run_id: str = "",
        session_id: str = "",
        agent_version_id: str = "",
        scenario: str = "",
        task_id: str = "",
        entities: Mapping[str, list[str]] | None = None,
        idempotency_key: str | None = None,
    ) -> ImprovementFeedbackRecord:
        values = _prepare_feedback_create_values(
            improvement_id,
            agent_id=agent_id,
            summary=summary,
            source=source,
            status=status,
            raw_text=raw_text,
            run_id=run_id,
            session_id=session_id,
            agent_version_id=agent_version_id,
            scenario=scenario,
            task_id=task_id,
            entities=entities,
        )
        clean_key = normalize_idempotency_key(idempotency_key)
        with self._session_factory.begin() as db:
            ledger, replayed_record = _claim_feedback_create_or_replay_in_transaction(
                db,
                key=clean_key,
                values=values,
            )
            if replayed_record is not None:
                return replayed_record
            self._lock_mutable_improvement(db, improvement_id)
            item = db.get(ImprovementItemModel, improvement_id)
            if item is None:
                raise NotFoundError(f"ImprovementItem not found: {improvement_id}")
            self._require_feedback_intake(item)
            if item.agent_id != agent_id:
                raise BusinessRuleViolation("Cannot create feedback under a different business agent")
            _require_feedback_run_owner(db, run_id=values["run_id"], agent_id=agent_id)
            row = ImprovementFeedbackModel(**values)
            db.add(row)
            db.flush()
            if ledger is not None:
                complete_idempotency_operation(
                    ledger,
                    resource_kind="improvement_feedback",
                    resource_id=values["feedback_id"],
                )
                db.flush()
            return _feedback_records(db, [row])[0]

    def list_feedbacks(self, improvement_id: str) -> list[ImprovementFeedbackRecord]:
        with self._session_factory.begin() as db:
            rows = (
                db.query(ImprovementFeedbackModel)
                .filter(ImprovementFeedbackModel.improvement_id == improvement_id)
                .order_by(ImprovementFeedbackModel.created_at, ImprovementFeedbackModel.feedback_id)
                .all()
            )
            return _feedback_records(db, rows)

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
                case = db.get(FeedbackCaseModel, clean_case_id)
                if case is None:
                    raise NotFoundError(f"FeedbackCase not found: {clean_case_id}")
                if case.agent_id != agent_id:
                    raise BusinessRuleViolation("Cannot attach feedback case across different business agents")
                _require_feedback_run_owner(db, run_id=run_id, agent_id=agent_id)
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
                    entities_json=parse_entities(case.entities_json),
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
                return _feedback_records(db, [row])[0]
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
            return _feedback_records(db, [row])[0]

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
            return _feedback_records(db, rows)


def _source_events_for_case(
    case: FeedbackCaseModel,
    events_by_id: Mapping[str, FeedbackEventModel],
) -> list[ImprovementFeedbackSourceEvent]:
    event_ids = list(dict.fromkeys(case.event_ids_json or []))
    if any(event_id not in events_by_id for event_id in event_ids):
        raise ConflictError("FeedbackCase source event record is missing")
    return [
        ImprovementFeedbackSourceEvent(
            event_id=event_id,
            source_system=events_by_id[event_id].source_system,
            event_type=events_by_id[event_id].event_type,
        )
        for event_id in event_ids
    ]


def _feedback_records(db: Session, rows: Sequence[ImprovementFeedbackModel]) -> list[ImprovementFeedbackRecord]:
    if not rows:
        return []
    feedback_ids = [row.feedback_id for row in rows]
    assignments = db.scalars(select(ImprovementFeedbackCaseAssignmentModel).where(ImprovementFeedbackCaseAssignmentModel.feedback_id.in_(feedback_ids))).all()
    assignments_by_feedback = {assignment.feedback_id: assignment for assignment in assignments}
    case_ids = [assignment.feedback_case_id for assignment in assignments]
    cases = db.scalars(select(FeedbackCaseModel).where(FeedbackCaseModel.feedback_case_id.in_(case_ids))).all() if case_ids else []
    cases_by_id = {case.feedback_case_id: case for case in cases}
    event_ids = list(dict.fromkeys(event_id for case in cases for event_id in (case.event_ids_json or [])))
    event_rows = db.scalars(select(FeedbackEventModel).where(FeedbackEventModel.event_id.in_(event_ids))).all() if event_ids else []
    events_by_id = {event.event_id: event for event in event_rows}
    records: list[ImprovementFeedbackRecord] = []
    for row in rows:
        assignment = assignments_by_feedback.get(row.feedback_id)
        if is_feedback_case_source(row.source) != (assignment is not None):
            raise ConflictError("FeedbackCase assignment does not match feedback source")
        case = cases_by_id.get(assignment.feedback_case_id) if assignment is not None else None
        if assignment is not None and (
            case is None or assignment.agent_id != row.agent_id or assignment.improvement_id != row.improvement_id or case.agent_id != row.agent_id
        ):
            raise ConflictError("FeedbackCase assignment does not match feedback ownership")
        records.append(
            ImprovementFeedbackRecord(
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
                entities=parse_entities(row.entities_json),
                feedback_case_id=assignment.feedback_case_id if assignment is not None else None,
                source_events=_source_events_for_case(case, events_by_id) if case is not None else [],
                created_at=row.created_at,
            )
        )
    return records
