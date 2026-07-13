from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import BusinessRuleViolation, ConflictError, DataIntegrityError
from ..json_types import JsonObject
from ..records.case_records import FeedbackCaseRecord
from ..records.source_records import FeedbackSignalRecord, PendingCorrelationRecord, SocEventRecord
from ..runtime_db import (
    FeedbackCaseModel,
    FeedbackCaseSourceModel,
    FeedbackSignalModel,
    PendingCorrelationModel,
    SocEventModel,
    utc_now,
)

CaseSourceKind = Literal["signal", "soc_event", "pending_correlation"]
CaseSourceRef = tuple[CaseSourceKind, str]
CaseSourceOwner = tuple[CaseSourceKind, str, str | None]


@dataclass
class _ResolvedCaseSources:
    signals: dict[str, JsonObject] = field(default_factory=dict)
    events: dict[str, JsonObject] = field(default_factory=dict)
    pending: dict[str, JsonObject] = field(default_factory=dict)
    owners: list[CaseSourceOwner] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)


class FeedbackCaseStoreMixin:
    """Store operations for feedback cases and case status updates."""

    def create_case(
        self,
        *,
        source_refs: list[tuple[str, str]],
        title: Optional[str] = None,
        priority: str = "medium",
    ) -> Optional[JsonObject]:
        unique_refs = self._unique_case_source_refs(source_refs)
        if not unique_refs:
            return None

        with self.Session() as db:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            try:
                sources = self._resolve_case_sources(db, unique_refs)
                if sources.unresolved:
                    db.rollback()
                    return None
                agent_id = self._case_agent_id(sources)
                feedback_case = self._feedback_case_payload(
                    source_ids=self._unique_strings([source_id for _, source_id in unique_refs]),
                    signals=list(sources.signals.values()),
                    events=list(sources.events.values()),
                    pending=list(sources.pending.values()),
                    agent_id=agent_id,
                    title=title,
                    priority=priority,
                )
                self._claim_case_sources(
                    db,
                    sources.owners,
                    direct_refs=unique_refs,
                    case_id=str(feedback_case["feedback_case_id"]),
                )
                db.add(self._case_model_from_dict(feedback_case))
                db.commit()
            except Exception:
                db.rollback()
                raise
        return feedback_case

    def _resolve_case_sources(self, db: Session, source_refs: list[CaseSourceRef]) -> _ResolvedCaseSources:
        sources = _ResolvedCaseSources()
        for source_kind, source_id in source_refs:
            if source_kind == "signal":
                signal_row = db.get(FeedbackSignalModel, source_id)
                if signal_row is None:
                    sources.unresolved.append(f"signal:{source_id}")
                    continue
                signal = FeedbackSignalRecord.from_row(signal_row).to_payload()
                sources.signals[source_id] = signal
                sources.owners.append(("signal", source_id, self._string(signal.get("agent_id"))))
                continue
            if source_kind == "soc_event":
                event_row = db.get(SocEventModel, source_id)
                if event_row is None:
                    sources.unresolved.append(f"soc_event:{source_id}")
                    continue
                event = SocEventRecord.from_row(event_row).to_payload()
                sources.events[source_id] = event
                sources.owners.append(("soc_event", source_id, self._string(event.get("agent_id"))))
                continue
            self._resolve_pending_case_source(db, source_id, sources)
        self._include_pending_events(sources)
        return sources

    @staticmethod
    def _unique_case_source_refs(source_refs: list[tuple[str, str]]) -> list[CaseSourceRef]:
        unique: list[CaseSourceRef] = []
        seen: set[CaseSourceRef] = set()
        for raw_kind, raw_source_id in source_refs:
            source_kind = raw_kind.strip()
            source_id = raw_source_id.strip()
            if source_kind not in {"signal", "soc_event", "pending_correlation"}:
                raise BusinessRuleViolation(f"Unsupported FeedbackCase source kind: {raw_kind}")
            if not source_id:
                raise BusinessRuleViolation("FeedbackCase source_id cannot be empty")
            normalized: CaseSourceRef
            if source_kind == "signal":
                normalized = ("signal", source_id)
            elif source_kind == "soc_event":
                normalized = ("soc_event", source_id)
            else:
                normalized = ("pending_correlation", source_id)
            if normalized not in seen:
                seen.add(normalized)
                unique.append(normalized)
        return unique

    def _resolve_pending_case_source(self, db: Session, source_id: str, sources: _ResolvedCaseSources) -> None:
        pending_row = db.get(PendingCorrelationModel, source_id)
        if pending_row is None:
            sources.unresolved.append(f"pending_correlation:{source_id}")
            return
        pending_record = PendingCorrelationRecord.from_row(pending_row).to_payload()
        if pending_record.get("status") != "resolved":
            raise BusinessRuleViolation("FeedbackCase cannot include an unresolved correlation")
        event_id = self._string(pending_record.get("event_id"))
        event_row = db.get(SocEventModel, event_id) if event_id else None
        if event_row is None:
            raise BusinessRuleViolation("Resolved correlation source event no longer exists")
        event = SocEventRecord.from_row(event_row).to_payload()
        sources.pending[pending_row.pending_id] = pending_record
        sources.events[event_row.event_id] = event
        sources.owners.append(("pending_correlation", pending_row.pending_id, self._string(event.get("agent_id"))))

    def _include_pending_events(self, sources: _ResolvedCaseSources) -> None:
        direct_event_ids = {source_id for kind, source_id, _ in sources.owners if kind == "soc_event"}
        for event_id, event in sources.events.items():
            if event_id not in direct_event_ids:
                sources.owners.append(("soc_event", event_id, self._string(event.get("agent_id"))))

    def _case_agent_id(self, sources: _ResolvedCaseSources) -> str:
        if any(not agent_id for _, _, agent_id in sources.owners):
            raise BusinessRuleViolation("FeedbackCase sources must be attributed to a business agent before case creation")
        agent_ids = self._unique_strings([agent_id or "" for _, _, agent_id in sources.owners])
        if len(agent_ids) != 1:
            raise BusinessRuleViolation("FeedbackCase sources must belong to one business agent")
        agent_id = agent_ids[0]
        agent_exists = getattr(self, "agent_exists", None)
        if agent_exists is not None and not agent_exists(agent_id):
            raise BusinessRuleViolation(f"FeedbackCase source business agent does not exist: {agent_id}")
        return agent_id

    def _claim_case_sources(
        self,
        db: Session,
        owners: list[CaseSourceOwner],
        *,
        direct_refs: list[CaseSourceRef],
        case_id: str,
    ) -> None:
        for source_kind, source_id, _ in owners:
            existing = db.get(FeedbackCaseSourceModel, (source_kind, source_id))
            if existing is not None:
                raise ConflictError(f"Feedback source already belongs to FeedbackCase: {existing.case_id}")
        direct_positions = {ref: position for position, ref in enumerate(direct_refs)}
        now = utc_now()
        for source_kind, source_id, owner_id in owners:
            if not owner_id:
                raise DataIntegrityError("FeedbackCase claim owner cannot be empty")
            direct_position = direct_positions.get((source_kind, source_id))
            db.add(
                FeedbackCaseSourceModel(
                    source_kind=source_kind,
                    source_id=source_id,
                    case_id=case_id,
                    agent_id=owner_id,
                    is_direct=direct_position is not None,
                    direct_position=direct_position,
                    created_at=now,
                )
            )

    def list_cases(
        self,
        *,
        agent_id: Optional[str] = None,
        status: Optional[str] = None,
        q: Optional[str] = None,
        limit: int = 100,
    ) -> list[JsonObject]:
        query_text = q.lower() if q else None
        result: list[JsonObject] = []
        with self.Session() as db:
            stmt = select(FeedbackCaseModel).order_by(FeedbackCaseModel.updated_at.desc())
            rows = db.scalars(stmt).all()
            for row in rows:
                record = self._case_to_dict(db, row)
                if record is None:
                    continue
                if agent_id and record.get("agent_id") != agent_id:
                    continue
                if status and record.get("status") != status:
                    continue
                if query_text and query_text not in json.dumps(record, ensure_ascii=False).lower():
                    continue
                result.append(record)
                if len(result) >= limit:
                    break
        return result

    def find_case(self, feedback_case_id: str) -> Optional[JsonObject]:
        if not feedback_case_id:
            return None
        with self.Session() as db:
            record = db.get(FeedbackCaseModel, feedback_case_id)
            return self._case_to_dict(db, record) if record else None

    def _feedback_case_payload(
        self,
        *,
        source_ids: list[str],
        signals: list[JsonObject],
        events: list[JsonObject],
        pending: list[JsonObject],
        agent_id: str,
        title: Optional[str],
        priority: str,
    ) -> JsonObject:
        records = [*signals, *events, *pending]
        now = utc_now()
        return self._scrub_record(
            {
                "feedback_case_id": f"fbc-{uuid.uuid4()}",
                "agent_id": agent_id,
                "created_at": now,
                "updated_at": now,
                "status": "pending_evidence",
                "title": title or self._case_title(records),
                "priority": priority or "medium",
                "source_ids": source_ids,
                "signal_ids": self._unique_strings([record.get("signal_id") for record in signals]),
                "event_ids": self._unique_strings([record.get("event_id") for record in events]),
                "pending_correlation_ids": self._unique_strings([record.get("pending_id") for record in pending]),
                "run_ids": self._feedback_case_run_ids(signals=signals, events=events, pending=pending),
                "session_ids": self._unique_strings([self._string(record.get("session_id")) or "" for record in records]),
                "alert_ids": self._unique_strings([self._string(record.get("alert_id")) or "" for record in records]),
                "case_ids": self._unique_strings([self._string(record.get("case_id")) or "" for record in records]),
                "evidence_package_ids": [],
                "attribution_job_ids": [],
            }
        )

    def _feedback_case_run_ids(
        self,
        *,
        signals: list[JsonObject],
        events: list[JsonObject],
        pending: list[JsonObject],
    ) -> list[str]:
        return self._unique_strings(
            [
                *[self._string(record.get("run_id")) or self._string(record.get("matched_run_id")) or "" for record in signals],
                *[self._string(record.get("run_id")) or self._string(record.get("matched_run_id")) or "" for record in events],
                *[self._string(record.get("resolved_run_id")) or "" for record in pending],
            ]
        )

    def _case_model_from_dict(self, feedback_case: JsonObject) -> FeedbackCaseModel:
        record = FeedbackCaseRecord.model_validate(feedback_case)
        return FeedbackCaseModel(
            feedback_case_id=record.feedback_case_id,
            agent_id=record.agent_id,
            created_at=record.created_at,
            updated_at=record.updated_at,
            status=record.status,
            title=record.title,
            priority=record.priority,
            current_evidence_package_id=self._latest(record.evidence_package_ids),
            current_attribution_job_id=self._latest(record.attribution_job_ids),
            source_ids_json=record.source_ids,
            signal_ids_json=record.signal_ids,
            event_ids_json=record.event_ids,
            pending_correlation_ids_json=record.pending_correlation_ids,
            run_ids_json=record.run_ids,
            session_ids_json=record.session_ids,
            alert_ids_json=record.alert_ids,
            case_ids_json=record.case_ids,
        )

    def _case_to_dict(self, db: Session, row: FeedbackCaseModel) -> Optional[JsonObject]:
        record = self._project_case_record(db, row)
        return record.to_payload() if record else None

    def _project_case_record(
        self,
        db: Session,
        row: FeedbackCaseModel,
        claims: Sequence[FeedbackCaseSourceModel] | None = None,
    ) -> Optional[FeedbackCaseRecord]:
        claim_rows = (
            list(claims)
            if claims is not None
            else list(db.scalars(select(FeedbackCaseSourceModel).where(FeedbackCaseSourceModel.case_id == row.feedback_case_id)).all())
        )
        if not claim_rows:
            return None
        ordered_claims = sorted(claim_rows, key=self._case_claim_sort_key)
        refs = [(claim.source_kind, claim.source_id) for claim in ordered_claims]
        sources = self._resolve_claimed_case_sources(db, self._unique_case_source_refs(refs))
        self._validate_projected_case_claims(row.feedback_case_id, ordered_claims, sources)
        direct_claims = sorted((claim for claim in ordered_claims if claim.is_direct), key=self._case_claim_sort_key)
        signal_claims = sorted((claim for claim in ordered_claims if claim.source_kind == "signal"), key=self._case_claim_sort_key)
        pending_claims = sorted(
            (claim for claim in ordered_claims if claim.source_kind == "pending_correlation"),
            key=self._case_claim_sort_key,
        )
        event_claims = self._ordered_event_claims(ordered_claims, sources)
        signals = [sources.signals[claim.source_id] for claim in signal_claims]
        events = [sources.events[claim.source_id] for claim in event_claims]
        pending = [sources.pending[claim.source_id] for claim in pending_claims]
        records = [*signals, *events, *pending]
        agent_id = ordered_claims[0].agent_id
        return FeedbackCaseRecord.model_validate(
            {
                "feedback_case_id": row.feedback_case_id,
                "agent_id": agent_id,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
                "status": row.status,
                "title": row.title,
                "priority": row.priority,
                "source_ids": self._unique_strings([claim.source_id for claim in direct_claims]),
                "signal_ids": [claim.source_id for claim in signal_claims],
                "event_ids": [claim.source_id for claim in event_claims],
                "pending_correlation_ids": [claim.source_id for claim in pending_claims],
                "run_ids": self._feedback_case_run_ids(signals=signals, events=events, pending=pending),
                "session_ids": self._unique_strings([self._string(record.get("session_id")) or "" for record in records]),
                "alert_ids": self._unique_strings([self._string(record.get("alert_id")) or "" for record in records]),
                "case_ids": self._unique_strings([self._string(record.get("case_id")) or "" for record in records]),
                "evidence_package_ids": [row.current_evidence_package_id] if row.current_evidence_package_id else [],
                "attribution_job_ids": [row.current_attribution_job_id] if row.current_attribution_job_id else [],
            }
        )

    def _validate_projected_case_claims(
        self,
        case_id: str,
        claims: list[FeedbackCaseSourceModel],
        sources: _ResolvedCaseSources,
    ) -> None:
        if sources.unresolved:
            raise DataIntegrityError(f"FeedbackCase {case_id} has missing claimed sources: {sources.unresolved}")
        claimed_owners = {(claim.source_kind, claim.source_id): claim.agent_id for claim in claims}
        actual_owners = {(kind, source_id): owner for kind, source_id, owner in sources.owners}
        if set(claimed_owners) != set(actual_owners):
            raise DataIntegrityError(f"FeedbackCase {case_id} claim graph does not match resolved sources")
        owner_ids = set()
        direct_positions: list[int] = []
        for claim in claims:
            ref = (claim.source_kind, claim.source_id)
            actual_owner = actual_owners.get(ref)
            if not claim.agent_id or not actual_owner or claim.agent_id != actual_owner:
                raise DataIntegrityError(f"FeedbackCase {case_id} claim owner does not match source owner: {ref}")
            owner_ids.add(claim.agent_id)
            if claim.is_direct:
                if claim.direct_position is None or claim.direct_position < 0:
                    raise DataIntegrityError(f"FeedbackCase {case_id} has an invalid direct claim position: {ref}")
                direct_positions.append(claim.direct_position)
            elif claim.direct_position is not None:
                raise DataIntegrityError(f"FeedbackCase {case_id} has an indirect claim with a direct position: {ref}")
        if len(owner_ids) != 1 or sorted(direct_positions) != list(range(len(direct_positions))):
            raise DataIntegrityError(f"FeedbackCase {case_id} claims do not have one owner and contiguous direct positions")
        self._validate_implicit_event_claims(case_id, claims, sources)

    def _resolve_claimed_case_sources(self, db: Session, source_refs: list[CaseSourceRef]) -> _ResolvedCaseSources:
        sources = _ResolvedCaseSources()
        for source_kind, source_id in source_refs:
            if source_kind == "signal":
                row = db.get(FeedbackSignalModel, source_id)
                if row is None:
                    sources.unresolved.append(f"signal:{source_id}")
                    continue
                payload = self._claimed_source_payload(row, id_key="signal_id", source_id=source_id)
                sources.signals[source_id] = payload
                sources.owners.append(("signal", source_id, self._string(payload.get("agent_id"))))
                continue
            if source_kind == "soc_event":
                row = db.get(SocEventModel, source_id)
                if row is None:
                    sources.unresolved.append(f"soc_event:{source_id}")
                    continue
                payload = self._claimed_source_payload(row, id_key="event_id", source_id=source_id)
                sources.events[source_id] = payload
                sources.owners.append(("soc_event", source_id, self._string(payload.get("agent_id"))))
                continue
            self._resolve_claimed_pending_source(db, source_id, sources)
        self._include_pending_events(sources)
        return sources

    def _resolve_claimed_pending_source(
        self,
        db: Session,
        source_id: str,
        sources: _ResolvedCaseSources,
    ) -> None:
        row = db.get(PendingCorrelationModel, source_id)
        if row is None:
            sources.unresolved.append(f"pending_correlation:{source_id}")
            return
        pending = self._claimed_source_payload(row, id_key="pending_id", source_id=source_id)
        pending.update({"event_id": row.event_id, "status": row.status})
        if row.status != "resolved":
            raise DataIntegrityError(f"FeedbackCase claimed pending source is not resolved: {source_id}")
        event_row = db.get(SocEventModel, row.event_id)
        if event_row is None:
            sources.unresolved.append(f"soc_event:{row.event_id}")
            return
        event = self._claimed_source_payload(event_row, id_key="event_id", source_id=row.event_id)
        sources.pending[source_id] = pending
        sources.events[row.event_id] = event
        sources.owners.append(("pending_correlation", source_id, self._string(event.get("agent_id"))))

    @staticmethod
    def _claimed_source_payload(row: Any, *, id_key: str, source_id: str) -> JsonObject:
        payload = dict(getattr(row, "payload_json", None) or {})
        payload[id_key] = source_id
        for key in (
            "agent_id",
            "run_id",
            "matched_run_id",
            "session_id",
            "alert_id",
            "case_id",
            "created_at",
            "updated_at",
        ):
            value = getattr(row, key, None)
            if value is not None:
                payload[key] = value
        return payload

    def _validate_implicit_event_claims(
        self,
        case_id: str,
        claims: list[FeedbackCaseSourceModel],
        sources: _ResolvedCaseSources,
    ) -> None:
        direct_refs = {(claim.source_kind, claim.source_id) for claim in claims if claim.is_direct}
        if any(claim.source_kind in {"signal", "pending_correlation"} and not claim.is_direct for claim in claims):
            raise DataIntegrityError(f"FeedbackCase {case_id} has a non-direct signal or pending claim")
        linked_event_ids = {self._string(pending.get("event_id")) for pending in sources.pending.values() if self._string(pending.get("event_id"))}
        for claim in claims:
            if claim.source_kind != "soc_event" or claim.is_direct:
                continue
            if claim.source_id not in linked_event_ids:
                raise DataIntegrityError(f"FeedbackCase {case_id} has an unexplained indirect event claim: {claim.source_id}")
        for event_id in linked_event_ids:
            if ("soc_event", event_id) not in {(claim.source_kind, claim.source_id) for claim in claims}:
                raise DataIntegrityError(f"FeedbackCase {case_id} is missing a pending-linked event claim: {event_id}")
        if not direct_refs:
            raise DataIntegrityError(f"FeedbackCase {case_id} has no direct source claims")

    def _ordered_event_claims(
        self,
        claims: list[FeedbackCaseSourceModel],
        sources: _ResolvedCaseSources,
    ) -> list[FeedbackCaseSourceModel]:
        pending_positions = {
            self._string(sources.pending[claim.source_id].get("event_id")): claim.direct_position
            for claim in claims
            if claim.source_kind == "pending_correlation" and claim.source_id in sources.pending
        }

        def event_key(claim: FeedbackCaseSourceModel) -> tuple[int, int, str]:
            if claim.is_direct and claim.direct_position is not None:
                return claim.direct_position, 0, claim.source_id
            return int(pending_positions.get(claim.source_id) or 0), 1, claim.source_id

        return sorted((claim for claim in claims if claim.source_kind == "soc_event"), key=event_key)

    @staticmethod
    def _case_claim_sort_key(claim: FeedbackCaseSourceModel) -> tuple[int, str, str]:
        position = claim.direct_position if claim.direct_position is not None else 2**31
        return position, claim.source_kind, claim.source_id

    def _append_case_update(
        self,
        feedback_case: JsonObject,
        *,
        status: Optional[str] = None,
        evidence_package_id: Optional[str] = None,
        attribution_job_id: Optional[str] = None,
    ) -> JsonObject:
        with self.Session.begin() as db:
            if not self._append_case_update_row(
                db,
                feedback_case,
                status=status,
                evidence_package_id=evidence_package_id,
                attribution_job_id=attribution_job_id,
            ):
                return feedback_case
        return self.find_case(feedback_case["feedback_case_id"]) or feedback_case

    def _append_case_update_row(
        self,
        db: Any,
        feedback_case: JsonObject,
        *,
        status: Optional[str] = None,
        evidence_package_id: Optional[str] = None,
        attribution_job_id: Optional[str] = None,
    ) -> bool:
        row = db.get(FeedbackCaseModel, feedback_case["feedback_case_id"])
        if not row:
            return False
        projected = self._project_case_record(db, row)
        if projected is None:
            return False
        record = projected.update(
            updated_at=utc_now(),
            status=status,
            evidence_package_id=evidence_package_id,
            attribution_job_id=attribution_job_id,
        )
        self._apply_case_record(row, record)
        return True

    def _apply_case_record(self, row: FeedbackCaseModel, record: FeedbackCaseRecord) -> None:
        row.created_at = record.created_at
        row.agent_id = record.agent_id
        row.updated_at = record.updated_at
        row.status = record.status
        row.title = record.title
        row.priority = record.priority
        row.current_evidence_package_id = self._latest(record.evidence_package_ids)
        row.current_attribution_job_id = self._latest(record.attribution_job_ids)
        row.source_ids_json = record.source_ids
        row.signal_ids_json = record.signal_ids
        row.event_ids_json = record.event_ids
        row.pending_correlation_ids_json = record.pending_correlation_ids
        row.run_ids_json = record.run_ids
        row.session_ids_json = record.session_ids
        row.alert_ids_json = record.alert_ids
        row.case_ids_json = record.case_ids

    def _case_title(self, records: list[JsonObject]) -> str:
        for record in records:
            comment = self._string(record.get("comment"))
            if comment:
                return comment[:120]
            event_type = self._string(record.get("event_type"))
            if event_type:
                return event_type
            labels = record.get("labels")
            if isinstance(labels, list) and labels:
                return ", ".join(map(str, labels[:3]))
        return "反馈处置单"
