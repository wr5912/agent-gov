from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from ..errors import BusinessRuleViolation, ConflictError, NotFoundError
from ..json_types import JsonObject
from ..records.agent_job_records import AgentJobRecord
from ..records.case_records import FeedbackCaseRecord
from ..records.source_records import (
    AgentRunRecord,
    FeedbackSignalRecord,
    FeedbackSourceAnnotationRecord,
    PendingCorrelationRecord,
    SocEventRecord,
    apply_feedback_source_annotation_record,
    apply_pending_correlation_record,
    upsert_agent_run_record,
)
from ..runtime_db import (
    AgentJobModel,
    AgentRunModel,
    FeedbackCaseModel,
    FeedbackCaseSourceModel,
    FeedbackSignalModel,
    FeedbackSourceAnnotationModel,
    PendingCorrelationModel,
    SocEventModel,
    utc_now,
)
from ..schemas import FeedbackSignalCreateRequest, SocEventIngestRequest
from .store_projection_maps import (
    AgentJobsById,
    FeedbackCasesBySourceRef,
    SourceAnnotationsByKey,
)


class FeedbackSourceStoreMixin:
    """Store operations for runs, feedback sources, annotations, and ownership."""

    def prepare_run_record(self, record: JsonObject) -> AgentRunRecord:
        payload = record if self.enable_debug_evidence else self._scrub_record(record)
        run_id = self._string(payload.get("run_id")) or f"run-{uuid.uuid4()}"
        payload = {**payload, "run_id": run_id, "created_at": payload.get("created_at") or utc_now()}
        return AgentRunRecord.from_payload(payload)

    def record_run(self, record: JsonObject) -> JsonObject:
        run_record = self.prepare_run_record(record)
        with self.Session.begin() as db:
            upsert_agent_run_record(db, run_record)
        return run_record.to_payload()

    def list_runs(
        self,
        *,
        run_id: Optional[str] = None,
        session_id: Optional[str] = None,
        alert_id: Optional[str] = None,
        case_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[JsonObject]:
        stmt = select(AgentRunModel).order_by(AgentRunModel.created_at.desc()).limit(limit)
        if agent_id:
            # run 的 agent_id 存于 payload_json，按 Agent 维度过滤运行（business Agent 间不串扰）。
            stmt = stmt.where(AgentRunModel.payload_json["agent_id"].as_string() == agent_id)
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
        with self.Session() as db:
            row = self._find_run_row_for_source(db, event)
            return AgentRunRecord.from_row(row).to_payload() if row else None

    def _find_run_row_for_source(self, db: Any, source: JsonObject) -> AgentRunModel | None:
        run_id = self._string(source.get("run_id"))
        if run_id:
            return db.get(AgentRunModel, run_id)
        runs = db.scalars(select(AgentRunModel).order_by(AgentRunModel.created_at.desc())).all()
        payloads = [(row, AgentRunRecord.from_row(row).to_payload()) for row in runs]
        session_id = self._string(source.get("session_id"))
        alert_id = self._string(source.get("alert_id"))
        case_id = self._string(source.get("case_id"))
        for row, run in payloads:
            same_incident = not (alert_id or case_id) or self._same_case_or_alert(run, alert_id, case_id)
            if session_id and run.get("session_id") == session_id and same_incident:
                return row
        if alert_id or case_id:
            for row, run in payloads:
                if self._same_case_or_alert(run, alert_id, case_id):
                    return row
        return None

    def create_signal(self, req: FeedbackSignalCreateRequest) -> JsonObject:
        with self.Session() as db:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            try:
                record = self._feedback_signal_record(db, req)
                existing = db.get(FeedbackSignalModel, record.signal_id)
                if existing is not None:
                    existing_record = FeedbackSignalRecord.from_row(existing)
                    retry_record = record.model_copy(update={"created_at": existing_record.created_at})
                    if retry_record.to_payload() != existing_record.to_payload():
                        raise ConflictError(f"Feedback signal id is already owned by different content: {record.signal_id}")
                    db.commit()
                    return existing_record.to_payload()
                db.add(self._feedback_signal_model(record))
                db.commit()
                return record.to_payload()
            except Exception:
                db.rollback()
                raise

    def _feedback_signal_record(
        self,
        db: Any,
        request: FeedbackSignalCreateRequest,
    ) -> FeedbackSignalRecord:
        payload = self._scrub_record(request.model_dump(mode="json"))
        if payload.get("source_type") == "implicit_feedback":
            payload["auto_captured"] = True
            payload["requires_review"] = True
        if not payload.get("run_id") and not (payload.get("session_id") or payload.get("alert_id") or payload.get("case_id")):
            raise BusinessRuleViolation("Feedback signal requires run_id, session_id, alert_id, or case_id")
        run_row = self._find_run_row_for_source(db, payload)
        run = AgentRunRecord.from_row(run_row).to_payload() if run_row is not None else None
        normalized = dict(payload)
        if run:
            normalized["session_id"] = normalized.get("session_id") or run.get("session_id")
            normalized["alert_id"] = normalized.get("alert_id") or run.get("alert_id")
            normalized["case_id"] = normalized.get("case_id") or run.get("case_id")
        agent_id = self._string((run or {}).get("agent_id"))
        metadata = dict(normalized.get("metadata") or {})
        if not agent_id:
            metadata["attribution_status"] = "unassigned"
            normalized["requires_review"] = True
        return FeedbackSignalRecord.model_validate(
            {
                **normalized,
                "signal_id": normalized.get("signal_id") or f"fbs-{uuid.uuid4()}",
                "created_at": utc_now(),
                "matched_run_id": run.get("run_id") if run else None,
                "agent_id": agent_id,
                "metadata": metadata,
            }
        )

    @staticmethod
    def _feedback_signal_model(record: FeedbackSignalRecord) -> FeedbackSignalModel:
        return FeedbackSignalModel(
            signal_id=record.signal_id,
            source_type=record.source_type,
            agent_id=record.agent_id,
            run_id=record.run_id,
            matched_run_id=record.matched_run_id,
            session_id=record.session_id,
            alert_id=record.alert_id,
            case_id=record.case_id,
            created_at=record.created_at,
            payload_json=record.to_payload(),
        )

    def reassign_signal_agent(self, signal_id: str, *, agent_id: str, operator: str, reason: Optional[str] = None) -> FeedbackSignalRecord:
        """管理员修正反馈归属：改写信号 agent_id，并把修正记录（from/to/operator/reason/at）
        写入 payload_json.attribution_corrections 审计列表，保留可追溯历史（AGV-025 criterion 3）。"""
        target = (agent_id or "").strip()
        if not target:
            raise BusinessRuleViolation("Reassign target agent_id cannot be empty")
        if not (operator or "").strip():
            raise BusinessRuleViolation("Reassign requires an operator for audit")
        with self.Session() as db:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            try:
                agent_exists = getattr(self, "agent_exists", None)
                if agent_exists is None or not agent_exists(target):
                    raise BusinessRuleViolation(f"Reassign target business agent does not exist: {target}")
                row = db.get(FeedbackSignalModel, signal_id)
                if row is None:
                    raise NotFoundError(f"Feedback signal not found: {signal_id}")
                owner = db.get(FeedbackCaseSourceModel, ("signal", signal_id))
                if owner is not None:
                    raise BusinessRuleViolation("Feedback signal ownership is immutable while it belongs to a FeedbackCase")
                payload = dict(row.payload_json or {})
                metadata = dict(payload.get("metadata") or {})
                corrections = list(metadata.get("attribution_corrections") or [])
                corrections.append(
                    {
                        "from": row.agent_id,
                        "to": target,
                        "operator": operator,
                        "reason": reason,
                        "at": utc_now(),
                    }
                )
                metadata["attribution_corrections"] = corrections
                payload["metadata"] = metadata
                payload["agent_id"] = target
                row.agent_id = target
                row.payload_json = payload
                db.commit()
                return FeedbackSignalRecord.from_row(row)
            except Exception:
                db.rollback()
                raise

    def list_signals(
        self,
        *,
        run_id: Optional[str] = None,
        session_id: Optional[str] = None,
        alert_id: Optional[str] = None,
        case_id: Optional[str] = None,
        source_type: Optional[str] = None,
        agent_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[JsonObject]:
        stmt = select(FeedbackSignalModel).order_by(FeedbackSignalModel.created_at.desc()).limit(limit)
        if agent_id:
            # 按 Agent 维度过滤反馈：只返回归属该 Agent 的信号，业务 Agent 间反馈不串扰。
            stmt = stmt.where(FeedbackSignalModel.agent_id == agent_id)
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
            "agent_id": self._string((run or {}).get("agent_id")),
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
                            agent_id=event_record.agent_id,
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
                return None
            pending_payload = PendingCorrelationRecord.from_row(record).to_payload()
            locator = {
                "run_id": run_id,
                "session_id": session_id or pending_payload.get("session_id"),
                "alert_id": alert_id or pending_payload.get("alert_id"),
                "case_id": case_id or pending_payload.get("case_id"),
            }
            run_row = self._find_run_row_for_source(db, locator)
            if run_row is None:
                raise BusinessRuleViolation("Resolved correlation requires an existing Agent run")
            run_payload = AgentRunRecord.from_row(run_row).to_payload()
            resolved = PendingCorrelationRecord.from_row(record).resolve(
                updated_at=utc_now(),
                run_id=run_row.run_id,
                session_id=session_id or self._string(run_payload.get("session_id")),
                alert_id=alert_id or self._string(run_payload.get("alert_id")),
                case_id=case_id or self._string(run_payload.get("case_id")),
                comment=comment,
            )
            apply_pending_correlation_record(record, resolved)
            event_row = db.get(SocEventModel, resolved.event_id)
            if event_row is None:
                raise BusinessRuleViolation("Pending correlation source event no longer exists")
            event_payload = dict(event_row.payload_json or {})
            event_payload.update(
                {
                    "agent_id": self._string(run_payload.get("agent_id")),
                    "matched_run_id": run_row.run_id,
                    "session_id": resolved.session_id,
                    "alert_id": resolved.alert_id,
                    "case_id": resolved.case_id,
                }
            )
            event_row.agent_id = self._string(run_payload.get("agent_id"))
            event_row.matched_run_id = run_row.run_id
            event_row.session_id = resolved.session_id
            event_row.alert_id = resolved.alert_id
            event_row.case_id = resolved.case_id
            event_row.payload_json = event_payload
        return resolved.to_payload()

    def list_feedback_sources(self, *, limit: int = 500) -> list[JsonObject]:
        annotations = self._source_annotations_by_key()
        cases_by_source_ref = self._cases_by_source_ref()
        attribution_job_ids = self._unique_strings([self._latest(item.attribution_job_ids) or "" for item in cases_by_source_ref.values()])
        attribution_jobs_by_id = self._jobs_by_id(attribution_job_ids)
        rows: list[JsonObject] = []
        rows.extend(
            self._source_row(
                source_kind="signal",
                source_id=str(item["signal_id"]),
                raw=item,
                annotation=annotations.get(("signal", str(item["signal_id"]))),
                feedback_case=cases_by_source_ref.get(("signal", str(item["signal_id"]))),
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
                feedback_case=cases_by_source_ref.get(("soc_event", str(item["event_id"]))),
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
                feedback_case=cases_by_source_ref.get(("pending_correlation", str(item["pending_id"]))),
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
        feedback_case = self._find_case_for_source(kind, source_id)
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
        record = (
            FeedbackSourceAnnotationRecord.from_row(row)
            if row
            else FeedbackSourceAnnotationRecord.model_validate(
                {
                    "annotation_id": annotation_id,
                    "source_kind": kind,
                    "source_id": source_id,
                    "created_at": now,
                    "updated_at": now,
                }
            )
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
        kind = self._normalize_source_kind(source_kind)
        raw = self._find_source_record(kind, source_id)
        if not raw:
            return None
        existing = self._find_case_for_source(kind, source_id)
        if existing:
            return existing.to_payload()
        source = self.find_feedback_source(kind, source_id) or {}
        try:
            return self.create_case(
                source_refs=[(kind, source_id)],
                title=self._source_case_title(source),
                priority=self._string(source.get("priority")) or priority or "medium",
            )
        except ConflictError:
            # A concurrent creator may win between the relation lookup and BEGIN IMMEDIATE.
            existing = self._find_case_for_source(kind, source_id)
            if existing:
                return existing.to_payload()
            raise

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

    def _cases_by_source_ref(self) -> FeedbackCasesBySourceRef:
        with self.Session() as db:
            rows = db.execute(
                select(FeedbackCaseSourceModel, FeedbackCaseModel).join(
                    FeedbackCaseModel,
                    FeedbackCaseModel.feedback_case_id == FeedbackCaseSourceModel.case_id,
                )
            ).all()
            claims_by_case: dict[str, list[FeedbackCaseSourceModel]] = {}
            cases_by_id: dict[str, FeedbackCaseModel] = {}
            for claim, feedback_case in rows:
                claims_by_case.setdefault(claim.case_id, []).append(claim)
                cases_by_id[feedback_case.feedback_case_id] = feedback_case
            result: FeedbackCasesBySourceRef = {}
            for case_id, claims in claims_by_case.items():
                record = self._project_case_record(db, cases_by_id[case_id], claims)
                if record is None:
                    continue
                for claim in claims:
                    result[(claim.source_kind, claim.source_id)] = record
            return result

    def _find_case_for_source(self, source_kind: str, source_id: str) -> Optional[FeedbackCaseRecord]:
        if not source_id:
            return None
        kind = self._normalize_source_kind(source_kind)
        with self.Session() as db:
            claim = db.get(FeedbackCaseSourceModel, (kind, source_id))
            feedback_case = db.get(FeedbackCaseModel, claim.case_id) if claim else None
            return self._project_case_record(db, feedback_case) if feedback_case else None

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
        attribution_jobs_by_id: Optional[AgentJobsById] = None,
    ) -> JsonObject:
        annotation_payload = annotation.to_payload() if annotation else {}
        feedback_case_id = self._string(feedback_case.feedback_case_id if feedback_case else None)
        attribution_job_id = self._latest(feedback_case.attribution_job_ids if feedback_case else [])
        if attribution_jobs_by_id is not None:
            attribution_job_record = attribution_jobs_by_id.get(attribution_job_id or "")
            attribution_job = attribution_job_record.to_payload() if attribution_job_record else None
        else:
            attribution_job = self.get_agent_job(attribution_job_id) if attribution_job_id else None
        run_id = self._string(raw.get("run_id")) or self._string(raw.get("matched_run_id")) or self._string(raw.get("resolved_run_id"))
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
            "requires_review": bool(annotation_payload.get("requires_review") if "requires_review" in annotation_payload else raw.get("requires_review")),
            "metadata": annotation_payload.get("metadata") if isinstance(annotation_payload.get("metadata"), dict) else {},
            "run_id": run_id,
            "session_id": self._string(raw.get("session_id")),
            "alert_id": self._string(raw.get("alert_id")),
            "case_id": self._string(raw.get("case_id")),
            "feedback_case_id": feedback_case_id,
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
