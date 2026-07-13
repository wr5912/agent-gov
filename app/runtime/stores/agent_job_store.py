from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select, update

from ..agent_job_logging import log_agent_job_event
from ..agent_job_types import FormatterOutputModel, ProjectedOutputModel, coerce_agent_job_type
from ..json_types import JsonObject
from ..records.agent_job_records import AgentJobRecord
from ..runtime_db import AgentJobModel, utc_now
from ..state_machines import validate_transition

_UNSET = object()
_COMPLETION_CLAIMABLE_STATES = ("queued", "running", "failed", "needs_human_review")
logger = logging.getLogger(__name__)


class AgentJobStoreMixin:
    """Generic persisted Agent job queue and lifecycle operations."""

    def create_agent_job(
        self,
        *,
        job_id: str,
        job_type: str,
        scope_kind: str,
        scope_id: str,
        profile_name: str,
        input_payload: JsonObject,
        input_path: Optional[str] = None,
        profile_version: Optional[JsonObject] = None,
        status: str = "queued",
    ) -> JsonObject:
        input_path = input_path or ""
        now = utc_now()
        row = AgentJobModel(
            job_id=job_id,
            job_type=job_type,
            scope_kind=scope_kind,
            scope_id=scope_id,
            status=status,
            profile_name=profile_name,
            created_at=now,
            started_at=None,
            completed_at=now if status in {"completed", "failed", "needs_human_review"} else None,
            input_path=input_path,
            raw_output_path=f"sqlite://agent_jobs/{job_id}/raw_output_json",
            validated_output_path=f"sqlite://agent_jobs/{job_id}/validated_output_json",
            error_path=f"sqlite://agent_jobs/{job_id}/error_json",
            runtime_version=self.runtime_version,
            schema_version=f"{job_type}-agent-job/v1",
            timeout_seconds=int(getattr(self, "agent_job_timeout_seconds", 300)),
            retry_count=0,
            profile_version_json=profile_version,
            input_json=input_payload,
        )
        with self.Session.begin() as db:
            existing = db.get(AgentJobModel, job_id)
            if existing:
                return self._agent_job_to_dict(existing)
            db.add(row)
        created = self.get_agent_job(job_id) or self._agent_job_to_dict(row)
        log_agent_job_event(logger, logging.INFO, "agent_job.queued", created)
        return created

    def get_agent_job(self, job_id: str) -> Optional[JsonObject]:
        if not job_id:
            return None
        with self.Session() as db:
            row = db.get(AgentJobModel, job_id)
            return self._agent_job_to_dict(row) if row else None

    def list_agent_jobs(
        self,
        *,
        job_type: Optional[str] = None,
        scope_kind: Optional[str] = None,
        scope_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[JsonObject]:
        stmt = select(AgentJobModel).order_by(AgentJobModel.created_at.desc()).limit(limit)
        if job_type:
            stmt = stmt.where(AgentJobModel.job_type == job_type)
        if scope_kind:
            stmt = stmt.where(AgentJobModel.scope_kind == scope_kind)
        if scope_id:
            stmt = stmt.where(AgentJobModel.scope_id == scope_id)
        if status:
            stmt = stmt.where(AgentJobModel.status == status)
        with self.Session() as db:
            return [self._agent_job_to_dict(row) for row in db.scalars(stmt).all()]

    def claim_next_agent_job(self, *, job_types: Optional[list[str]] = None) -> Optional[JsonObject]:
        now = utc_now()
        with self.Session.begin() as db:
            stmt = select(AgentJobModel).where(AgentJobModel.status == "queued").order_by(AgentJobModel.created_at.asc()).limit(20)
            if job_types:
                stmt = stmt.where(AgentJobModel.job_type.in_(job_types))
            for candidate in db.scalars(stmt).all():
                result = db.execute(
                    update(AgentJobModel)
                    .where(AgentJobModel.job_id == candidate.job_id, AgentJobModel.status == "queued")
                    .values(status="running", started_at=now)
                )
                if result.rowcount != 1:
                    continue
                db.flush()
                row = db.get(AgentJobModel, candidate.job_id)
                return self._agent_job_to_dict(row) if row else None
        return None

    def _timeout_stale_agent_jobs(self, *, limit: int = 100) -> list[JsonObject]:
        now = utc_now()
        now_dt = datetime.now(timezone.utc)
        timed_out_ids: list[str] = []
        with self.Session() as db:
            stmt = (
                select(AgentJobModel)
                .where(AgentJobModel.status.in_(("running", "schema_validating")))
                .order_by(AgentJobModel.started_at.asc(), AgentJobModel.created_at.asc())
                .limit(limit)
            )
            candidates = [(row.job_id, row.status, row.started_at or row.created_at, row.timeout_seconds) for row in db.scalars(stmt).all()]
        for job_id, observed_status, started_at, configured_timeout in candidates:
            base = self._parse_datetime(started_at)
            if not base:
                continue
            timeout_seconds = int(configured_timeout or getattr(self, "agent_job_timeout_seconds", 300))
            if now_dt < base + timedelta(seconds=timeout_seconds):
                continue
            error_payload = {
                "error_code": "AGENT_TIMEOUT",
                "message": f"Agent job exceeded timeout_seconds={timeout_seconds}",
                "created_at": now,
                "job_id": job_id,
            }
            with self.Session.begin() as db:
                row = self._compare_and_transition_agent_job_row(
                    db,
                    job_id,
                    expected_statuses=(observed_status,),
                    status="timeout",
                    completed_at=now,
                )
                if not row:
                    continue
                self._apply_agent_job_json_fields(row, {"error_json": error_payload})
                timed_out_ids.append(job_id)
        return [job for job in (self.get_agent_job(job_id) for job_id in timed_out_ids) if job]

    def complete_projected_agent_job(
        self,
        job: JsonObject,
        _job_output: FormatterOutputModel | ProjectedOutputModel | JsonObject,
    ) -> Optional[JsonObject]:
        job_id = str(job.get("job_id") or "")
        try:
            job_type = coerce_agent_job_type(str(job.get("job_type") or ""))
        except ValueError:
            return self.fail_agent_job(job_id, error_code="UNSUPPORTED_AGENT_JOB_TYPE", message=f"Unsupported agent job type: {job.get('job_type')}")
        return self.fail_agent_job(
            job_id,
            error_code="UNSUPPORTED_AGENT_JOB_COMPLETION",
            message=f"Persisted completion is not registered for Agent job type: {job_type}",
        )

    def fail_projected_agent_job(
        self,
        job: JsonObject,
        *,
        error_code: str,
        message: str,
        raw_output_json: Optional[JsonObject] = None,
        status: str = "failed",
    ) -> Optional[JsonObject]:
        return self.fail_agent_job(
            str(job.get("job_id") or ""),
            error_code=error_code,
            message=message,
            raw_output_json=raw_output_json,
            status=status,
        )

    def fail_agent_job(
        self,
        job_id: str,
        *,
        error_code: str,
        message: str,
        raw_output_json: Optional[JsonObject] = None,
        status: str = "failed",
    ) -> Optional[JsonObject]:
        if status not in {"failed", "timeout"}:
            raise ValueError(f"Unsupported agent job failure status: {status}")
        error_payload = {"error_code": error_code, "message": message, "created_at": utc_now(), "job_id": job_id}
        with self.Session.begin() as db:
            row = self._claim_agent_job_completion(db, job_id)
            if row is None:
                current = db.get(AgentJobModel, job_id)
                return self._agent_job_to_dict(current) if current else None
            fields: JsonObject = {"error_json": error_payload}
            if raw_output_json is not None:
                fields["raw_output_json"] = raw_output_json
            self._apply_agent_job_json_fields(row, fields)
            self._append_agent_job_update_row(db, job_id, status=status, completed_at=utc_now())
        return self.get_agent_job(job_id)

    def _apply_agent_job_json_fields(self, row: AgentJobModel, fields: JsonObject) -> AgentJobModel:
        payload = AgentJobRecord.from_row(row).to_payload()
        payload.update(fields)
        record = AgentJobRecord.model_validate(payload)
        row.raw_output_json = record.raw_output_json
        row.validated_output_json = record.validated_output_json
        row.error_json = record.error_json
        return row

    def _append_agent_job_update_row(
        self,
        db: Any,
        job_id: str,
        *,
        status: str,
        started_at: Optional[str] = None,
        completed_at: Optional[str] = None,
    ) -> Optional[AgentJobModel]:
        row = db.get(AgentJobModel, job_id)
        if not row:
            return None
        updated = AgentJobRecord.from_row(row).transition_to(
            status,
            started_at=started_at,
            completed_at=completed_at,
        )
        row.status = status
        row.started_at = updated.started_at
        row.completed_at = updated.completed_at
        return row

    def _claim_agent_job_completion(self, db: Any, job_id: str) -> Optional[AgentJobModel]:
        return self._compare_and_transition_agent_job_row(
            db,
            job_id,
            expected_statuses=_COMPLETION_CLAIMABLE_STATES,
            status="schema_validating",
        )

    def _compare_and_transition_agent_job_row(
        self,
        db: Any,
        job_id: str,
        *,
        expected_statuses: tuple[str, ...],
        status: str,
        started_at: Any = _UNSET,
        completed_at: Any = _UNSET,
    ) -> Optional[AgentJobModel]:
        for expected_status in expected_statuses:
            validate_transition("agent_job", expected_status, status)
        values: dict[str, object] = {"status": status}
        if started_at is not _UNSET:
            values["started_at"] = started_at
        if completed_at is not _UNSET:
            values["completed_at"] = completed_at
        result = db.execute(
            update(AgentJobModel)
            .where(AgentJobModel.job_id == job_id, AgentJobModel.status.in_(expected_statuses))
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            return None
        db.expire_all()
        row = db.get(AgentJobModel, job_id)
        if row is not None:
            AgentJobRecord.from_row(row)
        return row

    @staticmethod
    def _agent_job_to_dict(row: AgentJobModel) -> JsonObject:
        return AgentJobRecord.from_row(row).to_payload()
