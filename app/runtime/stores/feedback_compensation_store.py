from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any, Optional

from sqlalchemy import select

from ..records.feedback_compensation_records import ExecutionCompensationRecord
from ..runtime_db import ExecutionCompensationModel, utc_now


class FeedbackCompensationStoreMixin:
    """Persistence boundary for repairable execution-application compensation records."""

    def record_execution_compensation(
        self,
        *,
        optimization_task_id: str,
        execution_job_id: str,
        pre_execution_agent_version_id: str | None,
        restore_status: str,
        original_error: str,
        restore_error: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        record = ExecutionCompensationRecord.post_write_failure(
            compensation_id=f"fco-{uuid.uuid4()}",
            now=now,
            optimization_task_id=optimization_task_id,
            execution_job_id=execution_job_id,
            pre_execution_agent_version_id=pre_execution_agent_version_id,
            restore_status=restore_status,
            original_error=original_error,
            restore_error=restore_error,
        )
        row = self._execution_compensation_row(record)
        with self.Session.begin() as db:
            db.add(row)
        return record.to_payload()

    def find_execution_compensation(self, compensation_id: str) -> Optional[dict[str, Any]]:
        if not compensation_id:
            return None
        with self.Session() as db:
            row = db.get(ExecutionCompensationModel, compensation_id)
            return self._execution_compensation_payload(row) if row else None

    def list_execution_compensations(
        self,
        *,
        status: Optional[str] = None,
        optimization_task_id: Optional[str] = None,
        execution_job_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit or 100), 500))
        stmt = (
            select(ExecutionCompensationModel)
            .order_by(ExecutionCompensationModel.created_at.desc())
            .limit(safe_limit)
        )
        if status:
            stmt = stmt.where(ExecutionCompensationModel.status == status)
        if optimization_task_id:
            stmt = stmt.where(ExecutionCompensationModel.optimization_task_id == optimization_task_id)
        if execution_job_id:
            stmt = stmt.where(ExecutionCompensationModel.execution_job_id == execution_job_id)
        with self.Session() as db:
            return [self._execution_compensation_payload(row) for row in db.scalars(stmt).all()]

    def mark_execution_compensation_resolved(
        self,
        compensation_id: str,
        *,
        restore_result: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        return self._update_execution_compensation(
            compensation_id,
            update_record=lambda record, now: record.mark_resolved(
                updated_at=now,
                restore_result=restore_result,
            ),
        )

    def mark_execution_compensation_restore_failed(
        self,
        compensation_id: str,
        restore_error: str,
    ) -> Optional[dict[str, Any]]:
        return self._update_execution_compensation(
            compensation_id,
            update_record=lambda record, now: record.mark_restore_failed(
                updated_at=now,
                restore_error=restore_error,
            ),
        )

    def _execution_compensations_for_job(self, execution_job_id: str) -> list[dict[str, Any]]:
        if not execution_job_id:
            return []
        return self.list_execution_compensations(execution_job_id=execution_job_id, limit=20)

    def _update_execution_compensation(
        self,
        compensation_id: str,
        *,
        update_record: Callable[[ExecutionCompensationRecord, str], ExecutionCompensationRecord],
    ) -> Optional[dict[str, Any]]:
        now = utc_now()
        updated: ExecutionCompensationRecord | None = None
        with self.Session.begin() as db:
            row = db.get(ExecutionCompensationModel, compensation_id)
            if not row:
                return None
            record = self._execution_compensation_record(row)
            updated = update_record(record, now)
            self._apply_execution_compensation_record(row, updated)
        return updated.to_payload() if updated else None

    def _execution_compensation_payload(self, row: ExecutionCompensationModel) -> dict[str, Any]:
        return self._execution_compensation_record(row).to_payload()

    def _execution_compensation_record(self, row: ExecutionCompensationModel) -> ExecutionCompensationRecord:
        return ExecutionCompensationRecord.model_validate(row.payload_json or {})

    def _execution_compensation_row(self, record: ExecutionCompensationRecord) -> ExecutionCompensationModel:
        payload = record.to_payload()
        return ExecutionCompensationModel(
            compensation_id=record.compensation_id,
            created_at=record.created_at,
            updated_at=record.updated_at,
            status=record.status,
            compensation_type=record.compensation_type,
            optimization_task_id=record.optimization_task_id,
            execution_job_id=record.execution_job_id,
            pre_execution_agent_version_id=record.pre_execution_agent_version_id,
            restore_status=record.restore_status,
            payload_json=payload,
        )

    def _apply_execution_compensation_record(
        self,
        row: ExecutionCompensationModel,
        record: ExecutionCompensationRecord,
    ) -> None:
        row.updated_at = record.updated_at
        row.status = record.status
        row.restore_status = record.restore_status
        row.payload_json = record.to_payload()
