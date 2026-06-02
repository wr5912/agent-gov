from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from sqlalchemy import select

from ..records.case_records import FeedbackCaseRecord
from ..records.json_types import JsonObject
from ..runtime_db import FeedbackCaseModel, utc_now


class FeedbackCaseStoreMixin:
    """Store operations for feedback cases and case status updates."""

    def create_case(
        self,
        *,
        source_ids: list[str],
        title: Optional[str] = None,
        priority: str = "medium",
    ) -> Optional[JsonObject]:
        unique_ids = self._unique_strings(source_ids)
        if not unique_ids:
            return None

        signals: list[JsonObject] = []
        events: list[JsonObject] = []
        pending: list[JsonObject] = []
        unresolved: list[str] = []

        for source_id in unique_ids:
            signal = self.find_signal(source_id)
            if signal:
                signals.append(signal)
                continue
            event = self.find_event(source_id)
            if event:
                if not event.get("matched_run_id") and not event.get("run_id"):
                    return None
                events.append(event)
                continue
            pending_record = self.find_pending(source_id)
            if pending_record and pending_record.get("status") == "resolved":
                pending.append(pending_record)
                event = self.find_event(str(pending_record.get("event_id") or ""))
                if event:
                    events.append(event)
                continue
            unresolved.append(source_id)

        if unresolved:
            return None

        feedback_case = self._feedback_case_payload(
            source_ids=unique_ids,
            signals=signals,
            events=events,
            pending=pending,
            title=title,
            priority=priority,
        )
        with self.Session.begin() as db:
            db.add(self._case_model_from_dict(feedback_case))
        return feedback_case

    def list_cases(
        self,
        *,
        status: Optional[str] = None,
        q: Optional[str] = None,
        limit: int = 100,
    ) -> list[JsonObject]:
        query_text = q.lower() if q else None
        result: list[JsonObject] = []
        with self.Session() as db:
            rows = db.scalars(select(FeedbackCaseModel).order_by(FeedbackCaseModel.updated_at.desc())).all()
            for row in rows:
                record = self._case_to_dict(row)
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
            return self._case_to_dict(record) if record else None

    def _feedback_case_payload(
        self,
        *,
        source_ids: list[str],
        signals: list[JsonObject],
        events: list[JsonObject],
        pending: list[JsonObject],
        title: Optional[str],
        priority: str,
    ) -> JsonObject:
        records = [*signals, *events, *pending]
        now = utc_now()
        return self._scrub_record(
            {
                "feedback_case_id": f"fbc-{uuid.uuid4()}",
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
                "proposal_job_ids": [],
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
            created_at=record.created_at,
            updated_at=record.updated_at,
            status=record.status,
            title=record.title,
            priority=record.priority,
            current_evidence_package_id=self._latest(record.evidence_package_ids),
            current_attribution_job_id=self._latest(record.attribution_job_ids),
            current_proposal_job_id=self._latest(record.proposal_job_ids),
            source_ids_json=record.source_ids,
            signal_ids_json=record.signal_ids,
            event_ids_json=record.event_ids,
            pending_correlation_ids_json=record.pending_correlation_ids,
            run_ids_json=record.run_ids,
            session_ids_json=record.session_ids,
            alert_ids_json=record.alert_ids,
            case_ids_json=record.case_ids,
        )

    def _case_to_dict(self, row: FeedbackCaseModel) -> JsonObject:
        return FeedbackCaseRecord.from_row(row).to_payload()

    def _append_case_update(
        self,
        feedback_case: JsonObject,
        *,
        status: Optional[str] = None,
        evidence_package_id: Optional[str] = None,
        attribution_job_id: Optional[str] = None,
        proposal_job_id: Optional[str] = None,
    ) -> JsonObject:
        with self.Session.begin() as db:
            if not self._append_case_update_row(
                db,
                feedback_case,
                status=status,
                evidence_package_id=evidence_package_id,
                attribution_job_id=attribution_job_id,
                proposal_job_id=proposal_job_id,
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
        proposal_job_id: Optional[str] = None,
    ) -> bool:
        row = db.get(FeedbackCaseModel, feedback_case["feedback_case_id"])
        if not row:
            return False
        record = FeedbackCaseRecord.from_row(row).update(
            updated_at=utc_now(),
            status=status,
            evidence_package_id=evidence_package_id,
            attribution_job_id=attribution_job_id,
            proposal_job_id=proposal_job_id,
        )
        self._apply_case_record(row, record)
        return True

    def _apply_case_record(self, row: FeedbackCaseModel, record: FeedbackCaseRecord) -> None:
        row.created_at = record.created_at
        row.updated_at = record.updated_at
        row.status = record.status
        row.title = record.title
        row.priority = record.priority
        row.current_evidence_package_id = self._latest(record.evidence_package_ids)
        row.current_attribution_job_id = self._latest(record.attribution_job_ids)
        row.current_proposal_job_id = self._latest(record.proposal_job_ids)
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
