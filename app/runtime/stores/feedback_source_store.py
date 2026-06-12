from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from ..agent_profiles import MAIN_AGENT_PROFILE
from ..errors import BusinessRuleViolation
from ..records.agent_job_records import AgentJobRecord
from ..records.case_records import FeedbackCaseRecord
from ..records.eval_case_records import EvalCaseRecord
from ..records.source_records import (
    AgentRunRecord,
    FeedbackSourceAnnotationRecord,
    FeedbackSignalRecord,
    PendingCorrelationRecord,
    SocEventRecord,
    apply_feedback_source_annotation_record,
    apply_pending_correlation_record,
)
from ..json_types import JsonObject
from ..runtime_db import (
    AgentRunModel,
    AgentJobModel,
    EvalCaseModel,
    FeedbackSignalModel,
    FeedbackSourceAnnotationModel,
    PendingCorrelationModel,
    SocEventModel,
    utc_now,
)
from ..schemas import FeedbackSignalCreateRequest, SocEventIngestRequest
from .store_projection_maps import (
    AgentJobsById,
    EvalCasesByFeedbackCaseId,
    FeedbackCasesBySourceId,
    SourceAnnotationsByKey,
)


class FeedbackSourceStoreMixin:
    """Store operations for runs, feedback sources, annotations, and source eval cases."""

    def record_run(self, record: JsonObject) -> JsonObject:
        payload = record if self.enable_debug_evidence else self._scrub_record(record)
        run_id = self._string(payload.get("run_id")) or f"run-{uuid.uuid4()}"
        payload = {**payload, "run_id": run_id, "created_at": payload.get("created_at") or utc_now()}
        run_record = AgentRunRecord.from_payload(payload)
        with self.Session.begin() as db:
            existing = db.get(AgentRunModel, run_id)
            values = {
                "session_id": run_record.session_id,
                "sdk_session_id": run_record.sdk_session_id,
                "agent_version_id": run_record.agent_version_id,
                "alert_id": run_record.alert_id,
                "case_id": run_record.case_id,
                "created_at": run_record.created_at,
                "completed_at": run_record.completed_at,
                "langfuse_trace_id": run_record.langfuse_trace_id,
                "langfuse_trace_url": run_record.langfuse_trace_url,
                "payload_json": run_record.to_payload(),
            }
            if existing:
                for key, value in values.items():
                    setattr(existing, key, value)
            else:
                db.add(AgentRunModel(run_id=run_record.run_id, **values))
        return run_record.to_payload()

    def list_runs(
        self,
        *,
        run_id: Optional[str] = None,
        session_id: Optional[str] = None,
        alert_id: Optional[str] = None,
        case_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[JsonObject]:
        stmt = select(AgentRunModel).order_by(AgentRunModel.created_at.desc()).limit(limit)
        if run_id:
            stmt = stmt.where(AgentRunModel.run_id == run_id)
        if session_id:
            stmt = stmt.where(AgentRunModel.session_id == session_id)
        if alert_id:
            stmt = stmt.where(AgentRunModel.alert_id == alert_id)
        if case_id:
            stmt = stmt.where(AgentRunModel.case_id == case_id)
        with self.Session() as db:
            return [AgentRunRecord.from_row(row).to_payload() for row in db.scalars(stmt).all()]

    def find_run(self, *, run_id: Optional[str] = None) -> Optional[JsonObject]:
        if not run_id:
            return None
        with self.Session() as db:
            row = db.get(AgentRunModel, run_id)
            return AgentRunRecord.from_row(row).to_payload() if row else None

    def find_run_for_event(self, event: JsonObject) -> Optional[JsonObject]:
        exact = self.find_run(run_id=self._string(event.get("run_id")))
        if exact:
            return exact
        with self.Session() as db:
            runs = [AgentRunRecord.from_row(row).to_payload() for row in db.scalars(select(AgentRunModel).order_by(AgentRunModel.created_at.desc())).all()]
        session_id = self._string(event.get("session_id"))
        alert_id = self._string(event.get("alert_id"))
        case_id = self._string(event.get("case_id"))
        for run in runs:
            if session_id and run.get("session_id") == session_id and self._same_case_or_alert(run, alert_id, case_id):
                return run
        for run in runs:
            if self._same_case_or_alert(run, alert_id, case_id):
                return run
        return None

    def create_signal(self, req: FeedbackSignalCreateRequest) -> JsonObject:
        payload = self._scrub_record(req.model_dump(mode="json"))
        if payload.get("source_type") == "implicit_feedback":
            payload["auto_captured"] = True
            payload["requires_review"] = True
        if not payload.get("run_id") and not (payload.get("session_id") or payload.get("alert_id") or payload.get("case_id")):
            raise BusinessRuleViolation("Feedback signal requires run_id, session_id, alert_id, or case_id")

        run = self.find_run(run_id=self._string(payload.get("run_id")))
        if run:
            payload["session_id"] = payload.get("session_id") or run.get("session_id")
            payload["alert_id"] = payload.get("alert_id") or run.get("alert_id")
            payload["case_id"] = payload.get("case_id") or run.get("case_id")
        signal = {
            **payload,
            "signal_id": payload.get("signal_id") or f"fbs-{uuid.uuid4()}",
            "created_at": utc_now(),
            "matched_run_id": run.get("run_id") if run else None,
        }
        record = FeedbackSignalRecord.model_validate(signal)
        with self.Session.begin() as db:
            db.merge(
                FeedbackSignalModel(
                    signal_id=record.signal_id,
                    source_type=record.source_type,
                    agent_id=MAIN_AGENT_PROFILE,
                    run_id=record.run_id,
                    matched_run_id=record.matched_run_id,
                    session_id=record.session_id,
                    alert_id=record.alert_id,
                    case_id=record.case_id,
                    created_at=record.created_at,
                    payload_json=record.to_payload(),
                )
            )
        return record.to_payload()

    def list_signals(
        self,
        *,
        run_id: Optional[str] = None,
        session_id: Optional[str] = None,
        alert_id: Optional[str] = None,
        case_id: Optional[str] = None,
        source_type: Optional[str] = None,
        limit: int = 100,
    ) -> list[JsonObject]:
        stmt = select(FeedbackSignalModel).order_by(FeedbackSignalModel.created_at.desc()).limit(limit)
        if run_id:
            stmt = stmt.where(or_(FeedbackSignalModel.run_id == run_id, FeedbackSignalModel.matched_run_id == run_id))
        if session_id:
            stmt = stmt.where(FeedbackSignalModel.session_id == session_id)
        if alert_id:
            stmt = stmt.where(FeedbackSignalModel.alert_id == alert_id)
        if case_id:
            stmt = stmt.where(FeedbackSignalModel.case_id == case_id)
        if source_type:
            stmt = stmt.where(FeedbackSignalModel.source_type == source_type)
        with self.Session() as db:
            return [FeedbackSignalRecord.from_row(row).to_payload() for row in db.scalars(stmt).all()]

    def find_signal(self, signal_id: str) -> Optional[JsonObject]:
        if not signal_id:
            return None
        with self.Session() as db:
            record = db.get(FeedbackSignalModel, signal_id)
            return FeedbackSignalRecord.from_row(record).to_payload() if record else None

    def ingest_soc_event(self, req: SocEventIngestRequest) -> JsonObject:
        payload = self._scrub_record(req.model_dump(mode="json"))
        payload["auto_captured"] = True
        payload["requires_review"] = True if payload.get("requires_review") is None else payload.get("requires_review")
        run = self.find_run_for_event(payload)
        event = {
            "created_at": utc_now(),
            **payload,
            "matched_run_id": run.get("run_id") if run else None,
        }
        event_record = SocEventRecord.model_validate(event)
        event = event_record.to_payload()

        pending = None
        status = "matched"
        duplicate_event = None
        try:
            with self.Session.begin() as db:
                existing = db.get(SocEventModel, req.event_id)
                if existing:
                    duplicate_event = SocEventRecord.from_row(existing).to_payload()
                else:
                    db.add(
                        SocEventModel(
                            event_id=event_record.event_id,
                            event_type=event_record.event_type,
                            source_system=event_record.source_system,
                            run_id=event_record.run_id,
                            matched_run_id=event_record.matched_run_id,
                            session_id=event_record.session_id,
                            alert_id=event_record.alert_id,
                            case_id=event_record.case_id,
                            created_at=event_record.created_at,
                            payload_json=event,
                        )
                    )
                    if not run:
                        status = "pending_correlation"
                        pending_record = self._add_pending_correlation_row(db, event_record)
                        pending = pending_record.to_payload()
        except IntegrityError:
            duplicate_event = self.find_event(req.event_id)

        if duplicate_event:
            return {
                "event": duplicate_event,
                "correlation_status": "duplicate",
                "matched_run_id": duplicate_event.get("matched_run_id"),
                "pending_correlation": None,
            }

        return {
            "event": event,
            "correlation_status": status,
            "matched_run_id": event.get("matched_run_id"),
            "pending_correlation": pending,
        }

    @staticmethod
    def _add_pending_correlation_row(db: Any, event: SocEventRecord) -> PendingCorrelationRecord:
        pending = PendingCorrelationRecord.model_validate(
            {
                "pending_id": f"pc-{uuid.uuid4()}",
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "status": "pending",
                "reason": "no_matching_run",
                "event_id": event.event_id,
                "event_type": event.event_type,
                "source_system": event.source_system,
                "session_id": event.session_id,
                "alert_id": event.alert_id,
                "case_id": event.case_id,
            }
        )
        db.add(
            PendingCorrelationModel(
                pending_id=pending.pending_id,
                event_id=pending.event_id,
                status=pending.status,
                created_at=pending.created_at,
                updated_at=pending.updated_at,
                payload_json=pending.to_payload(),
            )
        )
        return pending

    def list_events(
        self,
        *,
        run_id: Optional[str] = None,
        session_id: Optional[str] = None,
        alert_id: Optional[str] = None,
        case_id: Optional[str] = None,
        event_type: Optional[str] = None,
        limit: int = 100,
    ) -> list[JsonObject]:
        stmt = select(SocEventModel).order_by(SocEventModel.created_at.desc()).limit(limit)
        if run_id:
            stmt = stmt.where(or_(SocEventModel.run_id == run_id, SocEventModel.matched_run_id == run_id))
        if session_id:
            stmt = stmt.where(SocEventModel.session_id == session_id)
        if alert_id:
            stmt = stmt.where(SocEventModel.alert_id == alert_id)
        if case_id:
            stmt = stmt.where(SocEventModel.case_id == case_id)
        if event_type:
            stmt = stmt.where(SocEventModel.event_type == event_type)
        with self.Session() as db:
            return [SocEventRecord.from_row(row).to_payload() for row in db.scalars(stmt).all()]

    def find_event(self, event_id: str) -> Optional[JsonObject]:
        if not event_id:
            return None
        with self.Session() as db:
            record = db.get(SocEventModel, event_id)
            return SocEventRecord.from_row(record).to_payload() if record else None

    def list_pending(self, *, status: Optional[str] = None, limit: int = 100) -> list[JsonObject]:
        stmt = select(PendingCorrelationModel).order_by(PendingCorrelationModel.updated_at.desc()).limit(limit)
        if status:
            stmt = stmt.where(PendingCorrelationModel.status == status)
        with self.Session() as db:
            return [PendingCorrelationRecord.from_row(row).to_payload() for row in db.scalars(stmt).all()]

    def find_pending(self, pending_id: str) -> Optional[JsonObject]:
        if not pending_id:
            return None
        with self.Session() as db:
            record = db.get(PendingCorrelationModel, pending_id)
            if not record:
                record = db.scalar(select(PendingCorrelationModel).where(PendingCorrelationModel.event_id == pending_id))
            return PendingCorrelationRecord.from_row(record).to_payload() if record else None

    def resolve_pending(
        self,
        pending_id: str,
        *,
        run_id: Optional[str] = None,
        session_id: Optional[str] = None,
        alert_id: Optional[str] = None,
        case_id: Optional[str] = None,
        comment: Optional[str] = None,
    ) -> Optional[JsonObject]:
        with self.Session.begin() as db:
            record = db.get(PendingCorrelationModel, pending_id)
            if not record:
                record = db.scalar(select(PendingCorrelationModel).where(PendingCorrelationModel.event_id == pending_id))
            if not record:
                return None
            resolved = PendingCorrelationRecord.from_row(record).resolve(
                updated_at=utc_now(),
                run_id=run_id,
                session_id=session_id,
                alert_id=alert_id,
                case_id=case_id,
                comment=comment,
            )
            apply_pending_correlation_record(record, resolved)
        return resolved.to_payload()

    def list_feedback_sources(self, *, limit: int = 500) -> list[JsonObject]:
        annotations = self._source_annotations_by_key()
        cases_by_source_id = self._cases_by_source_id()
        feedback_case_ids = self._unique_strings(
            [item.feedback_case_id for item in cases_by_source_id.values()]
        )
        eval_cases_by_case_id = self._eval_cases_by_feedback_case_ids(feedback_case_ids)
        attribution_job_ids = self._unique_strings(
            [self._latest(item.attribution_job_ids) or "" for item in cases_by_source_id.values()]
        )
        attribution_jobs_by_id = self._jobs_by_id(attribution_job_ids)
        rows: list[JsonObject] = []
        rows.extend(
            self._source_row(
                source_kind="signal",
                source_id=str(item["signal_id"]),
                raw=item,
                annotation=annotations.get(("signal", str(item["signal_id"]))),
                feedback_case=cases_by_source_id.get(str(item["signal_id"])),
                eval_cases_by_case_id=eval_cases_by_case_id,
                attribution_jobs_by_id=attribution_jobs_by_id,
            )
            for item in self.list_signals(limit=limit)
        )
        rows.extend(
            self._source_row(
                source_kind="soc_event",
                source_id=str(item["event_id"]),
                raw=item,
                annotation=annotations.get(("soc_event", str(item["event_id"]))),
                feedback_case=cases_by_source_id.get(str(item["event_id"])),
                eval_cases_by_case_id=eval_cases_by_case_id,
                attribution_jobs_by_id=attribution_jobs_by_id,
            )
            for item in self.list_events(limit=limit)
        )
        rows.extend(
            self._source_row(
                source_kind="pending_correlation",
                source_id=str(item["pending_id"]),
                raw=item,
                annotation=annotations.get(("pending_correlation", str(item["pending_id"]))),
                feedback_case=cases_by_source_id.get(str(item["pending_id"])),
                eval_cases_by_case_id=eval_cases_by_case_id,
                attribution_jobs_by_id=attribution_jobs_by_id,
            )
            for item in self.list_pending(limit=limit)
        )
        rows.sort(key=lambda item: str(item.get("created_at") or item.get("updated_at") or ""), reverse=True)
        return rows[:limit]

    def find_feedback_source(self, source_kind: str, source_id: str) -> Optional[JsonObject]:
        kind = self._normalize_source_kind(source_kind)
        raw = self._find_source_record(kind, source_id)
        if not raw:
            return None
        annotation = self._find_source_annotation(kind, source_id)
        feedback_case = self._find_case_for_source_id(source_id)
        return self._source_row(
            source_kind=kind,
            source_id=source_id,
            raw=raw,
            annotation=annotation,
            feedback_case=feedback_case,
        )

    def update_feedback_source_annotation(self, source_kind: str, source_id: str, fields: JsonObject) -> Optional[JsonObject]:
        kind = self._normalize_source_kind(source_kind)
        raw = self._find_source_record(kind, source_id)
        if not raw:
            return None
        with self.Session.begin() as db:
            self._upsert_feedback_source_annotation(db, kind, source_id, fields)
        return self.find_feedback_source(kind, source_id)

    def _upsert_feedback_source_annotation(self, db: Any, source_kind: str, source_id: str, fields: JsonObject) -> JsonObject:
        kind = self._normalize_source_kind(source_kind)
        annotation_id = self._source_annotation_id(kind, source_id)
        now = utc_now()
        row = db.get(FeedbackSourceAnnotationModel, annotation_id)
        record = FeedbackSourceAnnotationRecord.from_row(row) if row else FeedbackSourceAnnotationRecord.model_validate(
            {
                "annotation_id": annotation_id,
                "source_kind": kind,
                "source_id": source_id,
                "created_at": now,
                "updated_at": now,
            }
        )
        record = record.update(fields=fields, updated_at=now)
        if row:
            apply_feedback_source_annotation_record(row, record)
        else:
            db.add(
                FeedbackSourceAnnotationModel(
                    annotation_id=annotation_id,
                    source_kind=kind,
                    source_id=source_id,
                    status=record.status,
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                    payload_json=record.to_payload(),
                )
            )
        return record.to_payload()

    def ensure_case_for_source(self, source_kind: str, source_id: str, *, priority: str = "medium") -> Optional[JsonObject]:
        feedback_case, should_create = self._prepare_feedback_case_for_source(
            {"source_kind": source_kind, "source_id": source_id},
            priority=priority,
        )
        if not feedback_case or not should_create:
            return feedback_case
        with self.Session.begin() as db:
            db.add(self._case_model_from_dict(feedback_case))
        return feedback_case

    def generate_eval_cases_for_sources(self, source_refs: list[JsonObject], *, force: bool = False) -> JsonObject:
        return self.queue_feedback_eval_case_generation_agent_job(source_refs=source_refs, force=force) or {
            "created": 0,
            "reused": 0,
            "updated": 0,
            "skipped": 0,
            "eval_cases": [],
            "results": [],
        }

    def _prepare_feedback_case_for_source(self, ref: dict[str, str], *, priority: str) -> tuple[Optional[JsonObject], bool]:
        kind = self._normalize_source_kind(ref["source_kind"])
        source_id = ref["source_id"]
        raw = self._find_source_record(kind, source_id)
        if not raw:
            return None, False
        existing = self._find_case_for_source_id(source_id)
        if existing:
            return existing.to_payload(), False
        source = self.find_feedback_source(kind, source_id)
        feedback_case = self._new_feedback_case_for_source(kind, source_id, raw, source or {}, priority=priority)
        return (feedback_case, True) if feedback_case else (None, False)

    def _new_feedback_case_for_source(
        self,
        source_kind: str,
        source_id: str,
        raw: JsonObject,
        source: JsonObject,
        *,
        priority: str,
    ) -> Optional[JsonObject]:
        signals: list[JsonObject] = []
        events: list[JsonObject] = []
        pending: list[JsonObject] = []
        if source_kind == "signal":
            signals.append(raw)
        elif source_kind == "soc_event":
            if not raw.get("matched_run_id") and not raw.get("run_id"):
                return None
            events.append(raw)
        else:
            if raw.get("status") != "resolved":
                return None
            pending.append(raw)
            event = self.find_event(str(raw.get("event_id") or ""))
            if event:
                events.append(event)

        return self._feedback_case_payload(
            source_ids=[source_id],
            signals=signals,
            events=events,
            pending=pending,
            title=self._source_case_title(source) if source else None,
            priority=self._string(source.get("priority")) or priority or "medium",
        )

    def _normalize_source_kind(self, source_kind: str) -> str:
        normalized = str(source_kind or "").strip()
        aliases = {
            "feedback_signal": "signal",
            "event": "soc_event",
            "pending": "pending_correlation",
        }
        normalized = aliases.get(normalized, normalized)
        if normalized not in {"signal", "soc_event", "pending_correlation"}:
            raise BusinessRuleViolation(f"Unsupported feedback source kind: {source_kind}")
        return normalized

    def _normalize_source_refs(self, source_refs: list[JsonObject]) -> list[dict[str, str]]:
        refs: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in source_refs:
            if not isinstance(item, dict):
                continue
            try:
                kind = self._normalize_source_kind(str(item.get("source_kind") or item.get("kind") or ""))
            except BusinessRuleViolation:
                continue
            source_id = self._string(item.get("source_id") or item.get("id"))
            if not source_id:
                continue
            key = (kind, source_id)
            if key in seen:
                continue
            seen.add(key)
            refs.append({"source_kind": kind, "source_id": source_id})
        return refs

    def _find_source_record(self, source_kind: str, source_id: str) -> Optional[JsonObject]:
        kind = self._normalize_source_kind(source_kind)
        if kind == "signal":
            return self.find_signal(source_id)
        if kind == "soc_event":
            return self.find_event(source_id)
        return self.find_pending(source_id)

    def _source_annotation_id(self, source_kind: str, source_id: str) -> str:
        return f"{self._normalize_source_kind(source_kind)}:{source_id}"

    def _find_source_annotation(self, source_kind: str, source_id: str) -> Optional[FeedbackSourceAnnotationRecord]:
        with self.Session() as db:
            row = db.get(FeedbackSourceAnnotationModel, self._source_annotation_id(source_kind, source_id))
            return FeedbackSourceAnnotationRecord.from_row(row) if row else None

    def _source_annotations_by_key(self) -> SourceAnnotationsByKey:
        with self.Session() as db:
            rows = db.scalars(select(FeedbackSourceAnnotationModel)).all()
        return {(row.source_kind, row.source_id): FeedbackSourceAnnotationRecord.from_row(row) for row in rows}

    def _cases_by_source_id(self) -> FeedbackCasesBySourceId:
        result: FeedbackCasesBySourceId = {}
        for feedback_case_payload in self.list_cases(limit=1000):
            feedback_case = FeedbackCaseRecord.model_validate(feedback_case_payload)
            for source_id in feedback_case.source_ids:
                if isinstance(source_id, str) and source_id and source_id not in result:
                    result[source_id] = feedback_case
        return result

    def _find_case_for_source_id(self, source_id: str) -> Optional[FeedbackCaseRecord]:
        if not source_id:
            return None
        for feedback_case_payload in self.list_cases(limit=1000):
            feedback_case = FeedbackCaseRecord.model_validate(feedback_case_payload)
            if source_id in feedback_case.source_ids:
                return feedback_case
        return None

    def _eval_cases_by_feedback_case_ids(self, feedback_case_ids: list[str]) -> EvalCasesByFeedbackCaseId:
        if not feedback_case_ids:
            return {}
        with self.Session() as db:
            rows = db.scalars(select(EvalCaseModel).where(EvalCaseModel.source_feedback_case_id.in_(feedback_case_ids))).all()
        return {str(row.source_feedback_case_id): EvalCaseRecord.from_row(row) for row in rows if row.source_feedback_case_id}

    def _jobs_by_id(self, job_ids: list[str]) -> AgentJobsById:
        if not job_ids:
            return {}
        with self.Session() as db:
            rows = db.scalars(select(AgentJobModel).where(AgentJobModel.job_id.in_(job_ids))).all()
        return {row.job_id: AgentJobRecord.from_row(row) for row in rows}

    def _source_row(
        self,
        *,
        source_kind: str,
        source_id: str,
        raw: JsonObject,
        annotation: Optional[FeedbackSourceAnnotationRecord] = None,
        feedback_case: Optional[FeedbackCaseRecord] = None,
        eval_cases_by_case_id: Optional[EvalCasesByFeedbackCaseId] = None,
        attribution_jobs_by_id: Optional[AgentJobsById] = None,
    ) -> JsonObject:
        annotation_payload = annotation.to_payload() if annotation else {}
        feedback_case_id = self._string(feedback_case.feedback_case_id if feedback_case else None)
        if eval_cases_by_case_id is not None:
            eval_case_record = eval_cases_by_case_id.get(feedback_case_id)
            eval_case = eval_case_record.to_payload() if eval_case_record else None
        else:
            eval_case = self.find_eval_case(source_feedback_case_id=feedback_case_id) if feedback_case_id else None
        attribution_job_id = self._latest(feedback_case.attribution_job_ids if feedback_case else [])
        if attribution_jobs_by_id is not None:
            attribution_job_record = attribution_jobs_by_id.get(attribution_job_id or "")
            attribution_job = attribution_job_record.to_payload() if attribution_job_record else None
        else:
            attribution_job = self.get_job(attribution_job_id) if attribution_job_id else None
        run_id = (
            self._string(raw.get("run_id"))
            or self._string(raw.get("matched_run_id"))
            or self._string(raw.get("resolved_run_id"))
        )
        labels = annotation_payload.get("labels") if isinstance(annotation_payload.get("labels"), list) else raw.get("labels")
        if not isinstance(labels, list):
            labels = [raw.get("event_type")] if raw.get("event_type") else []
        comment = self._string(annotation_payload.get("comment")) or self._string(raw.get("comment"))
        created_at = self._string(raw.get("created_at")) or self._string(raw.get("timestamp")) or self._string(annotation_payload.get("created_at"))
        updated_at = self._string(annotation_payload.get("updated_at")) or self._string(raw.get("updated_at")) or created_at
        return {
            "schema_version": "feedback-source/v1",
            "source_kind": self._normalize_source_kind(source_kind),
            "source_id": source_id,
            "id": source_id,
            "created_at": created_at,
            "updated_at": updated_at,
            "status": self._string(annotation_payload.get("status")) or self._base_source_status(source_kind, raw),
            "label": self._source_label(source_kind, raw, labels, comment),
            "labels": self._unique_strings([str(item) for item in labels or [] if str(item).strip()]),
            "comment": comment,
            "priority": self._string(annotation_payload.get("priority")) or "medium",
            "requires_review": bool(
                annotation_payload.get("requires_review")
                if "requires_review" in annotation_payload
                else raw.get("requires_review")
            ),
            "metadata": annotation_payload.get("metadata") if isinstance(annotation_payload.get("metadata"), dict) else {},
            "run_id": run_id,
            "session_id": self._string(raw.get("session_id")),
            "alert_id": self._string(raw.get("alert_id")),
            "case_id": self._string(raw.get("case_id")),
            "feedback_case_id": feedback_case_id,
            "eval_case_id": self._string((eval_case or {}).get("eval_case_id")),
            "latest_attribution_job_id": attribution_job_id,
            "latest_attribution_status": self._string((attribution_job or {}).get("status")),
            "raw": raw,
        }

    def _base_source_status(self, source_kind: str, raw: JsonObject) -> str:
        kind = self._normalize_source_kind(source_kind)
        if kind == "signal":
            return "needs_review" if raw.get("requires_review") else "collected"
        if kind == "soc_event":
            return "matched" if raw.get("matched_run_id") or raw.get("run_id") else "pending_correlation"
        return self._string(raw.get("status")) or "pending"

    def _source_label(self, source_kind: str, raw: JsonObject, labels: Any, comment: Optional[str]) -> str:
        if comment:
            return comment[:120]
        if isinstance(labels, list) and labels:
            return ", ".join(str(item) for item in labels[:3])
        if raw.get("event_type"):
            return str(raw["event_type"])
        if raw.get("source_type"):
            return str(raw["source_type"])
        return self._normalize_source_kind(source_kind)

    def _source_case_title(self, source: JsonObject) -> str:
        return (
            self._string(source.get("comment"))
            or self._string(source.get("label"))
            or f"{source.get('source_kind') or 'feedback'} {source.get('source_id') or ''}"
        )[:120]

    def _same_case_or_alert(self, run: JsonObject, alert_id: Optional[str], case_id: Optional[str]) -> bool:
        return bool((alert_id and run.get("alert_id") == alert_id) or (case_id and run.get("case_id") == case_id))
