from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from sqlalchemy import select

from .runtime_db import FeedbackCaseModel, utc_now


class FeedbackCaseStoreMixin:
    """Store operations for feedback cases and case status updates."""

    def create_case(
        self,
        *,
        source_ids: list[str],
        title: Optional[str] = None,
        priority: str = "medium",
    ) -> Optional[dict[str, Any]]:
        unique_ids = self._unique_strings(source_ids)
        if not unique_ids:
            return None

        signals: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
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

        records = [*signals, *events, *pending]
        now = utc_now()
        feedback_case = self._scrub_record(
            {
                "feedback_case_id": f"fbc-{uuid.uuid4()}",
                "created_at": now,
                "updated_at": now,
                "status": "pending_evidence",
                "title": title or self._case_title(records),
                "priority": priority or "medium",
                "source_ids": unique_ids,
                "signal_ids": self._unique_strings([record.get("signal_id") for record in signals]),
                "event_ids": self._unique_strings([record.get("event_id") for record in events]),
                "pending_correlation_ids": self._unique_strings([record.get("pending_id") for record in pending]),
                "run_ids": self._unique_strings(
                    [
                        *[self._string(record.get("run_id")) or self._string(record.get("matched_run_id")) or "" for record in signals],
                        *[self._string(record.get("run_id")) or self._string(record.get("matched_run_id")) or "" for record in events],
                        *[self._string(record.get("resolved_run_id")) or "" for record in pending],
                    ]
                ),
                "session_ids": self._unique_strings([self._string(record.get("session_id")) or "" for record in records]),
                "alert_ids": self._unique_strings([self._string(record.get("alert_id")) or "" for record in records]),
                "case_ids": self._unique_strings([self._string(record.get("case_id")) or "" for record in records]),
                "evidence_package_ids": [],
                "attribution_job_ids": [],
                "proposal_job_ids": [],
            }
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
    ) -> list[dict[str, Any]]:
        query_text = q.lower() if q else None
        result: list[dict[str, Any]] = []
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

    def find_case(self, feedback_case_id: str) -> Optional[dict[str, Any]]:
        if not feedback_case_id:
            return None
        with self.Session() as db:
            record = db.get(FeedbackCaseModel, feedback_case_id)
            return self._case_to_dict(record) if record else None

    def _case_model_from_dict(self, feedback_case: dict[str, Any]) -> FeedbackCaseModel:
        return FeedbackCaseModel(
            feedback_case_id=feedback_case["feedback_case_id"],
            created_at=feedback_case["created_at"],
            updated_at=feedback_case["updated_at"],
            status=feedback_case["status"],
            title=feedback_case["title"],
            priority=feedback_case["priority"],
            current_evidence_package_id=self._latest(feedback_case.get("evidence_package_ids")),
            current_attribution_job_id=self._latest(feedback_case.get("attribution_job_ids")),
            current_proposal_job_id=self._latest(feedback_case.get("proposal_job_ids")),
            source_ids_json=feedback_case.get("source_ids") or [],
            signal_ids_json=feedback_case.get("signal_ids") or [],
            event_ids_json=feedback_case.get("event_ids") or [],
            pending_correlation_ids_json=feedback_case.get("pending_correlation_ids") or [],
            run_ids_json=feedback_case.get("run_ids") or [],
            session_ids_json=feedback_case.get("session_ids") or [],
            alert_ids_json=feedback_case.get("alert_ids") or [],
            case_ids_json=feedback_case.get("case_ids") or [],
        )

    def _case_to_dict(self, row: FeedbackCaseModel) -> dict[str, Any]:
        return {
            "feedback_case_id": row.feedback_case_id,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "status": row.status,
            "title": row.title,
            "priority": row.priority,
            "source_ids": row.source_ids_json or [],
            "signal_ids": row.signal_ids_json or [],
            "event_ids": row.event_ids_json or [],
            "pending_correlation_ids": row.pending_correlation_ids_json or [],
            "run_ids": row.run_ids_json or [],
            "session_ids": row.session_ids_json or [],
            "alert_ids": row.alert_ids_json or [],
            "case_ids": row.case_ids_json or [],
            "evidence_package_ids": [row.current_evidence_package_id] if row.current_evidence_package_id else [],
            "attribution_job_ids": [row.current_attribution_job_id] if row.current_attribution_job_id else [],
            "proposal_job_ids": [row.current_proposal_job_id] if row.current_proposal_job_id else [],
        }

    def _append_case_update(
        self,
        feedback_case: dict[str, Any],
        *,
        status: Optional[str] = None,
        evidence_package_id: Optional[str] = None,
        attribution_job_id: Optional[str] = None,
        proposal_job_id: Optional[str] = None,
    ) -> dict[str, Any]:
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
        feedback_case: dict[str, Any],
        *,
        status: Optional[str] = None,
        evidence_package_id: Optional[str] = None,
        attribution_job_id: Optional[str] = None,
        proposal_job_id: Optional[str] = None,
    ) -> bool:
        row = db.get(FeedbackCaseModel, feedback_case["feedback_case_id"])
        if not row:
            return False
        row.updated_at = utc_now()
        row.status = status or row.status
        if evidence_package_id:
            row.current_evidence_package_id = evidence_package_id
        if attribution_job_id:
            row.current_attribution_job_id = attribution_job_id
        if proposal_job_id:
            row.current_proposal_job_id = proposal_job_id
        return True

    def _case_title(self, records: list[dict[str, Any]]) -> str:
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
