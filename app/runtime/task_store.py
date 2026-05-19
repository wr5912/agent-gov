from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class TaskResolutionRecord:
    task_id: str
    status: str
    resolution: str | None
    target_workspace: str | None
    target_id: str | None
    evidence_ids: list[str]
    handled_by: str | None
    handled_at: str
    comment: str | None
    payload: dict[str, Any]


@dataclass
class InMemoryTaskResolutionStore:
    _records: dict[str, TaskResolutionRecord] = field(default_factory=dict)

    def resolve(
        self,
        *,
        task_id: str,
        status: str,
        resolution: str | None,
        target_workspace: str | None,
        target_id: str | None,
        evidence_ids: list[str],
        handled_by: str | None,
        handled_at: str | None,
        comment: str | None,
        payload: dict[str, Any],
    ) -> TaskResolutionRecord:
        record = TaskResolutionRecord(
            task_id=task_id,
            status=status,
            resolution=resolution,
            target_workspace=target_workspace,
            target_id=target_id,
            evidence_ids=evidence_ids,
            handled_by=handled_by,
            handled_at=handled_at or _utc_now(),
            comment=comment,
            payload=payload,
        )
        self._records[task_id] = record
        return record

    def get(self, task_id: str) -> TaskResolutionRecord | None:
        return self._records.get(task_id)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
