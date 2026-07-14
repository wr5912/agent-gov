from __future__ import annotations

from typing import Optional

from sqlalchemy import select

from ..json_types import JsonObject
from ..records.agent_job_records import AgentJobRecord
from ..runtime_db import AgentJobModel


class AgentJobStoreMixin:
    """Read-only projection for historical persisted Agent jobs."""

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

    @staticmethod
    def _agent_job_to_dict(row: AgentJobModel) -> JsonObject:
        return AgentJobRecord.from_row(row).to_payload()
