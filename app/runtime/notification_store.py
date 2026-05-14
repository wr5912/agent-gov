from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


@dataclass(frozen=True)
class NotificationRecord:
    notification_id: str
    name: str
    value: dict[str, Any]
    created_at: str
    workspace_id: str | None = None
    user_id: str | None = None


@dataclass
class InMemoryNotificationStore:
    max_items: int = 500
    _records: list[NotificationRecord] = field(default_factory=list)

    def publish(
        self,
        *,
        name: str,
        value: dict[str, Any],
        notification_id: str | None = None,
        workspace_id: str | None = None,
        user_id: str | None = None,
    ) -> NotificationRecord:
        created_at = _utc_now()
        record = NotificationRecord(
            notification_id=notification_id or f"notification-{uuid4()}",
            name=name,
            value=value,
            created_at=created_at,
            workspace_id=workspace_id,
            user_id=user_id,
        )
        self._records.append(record)
        if len(self._records) > self.max_items:
            self._records = self._records[-self.max_items :]
        return record

    def list_after(
        self,
        cursor: str | None = None,
        *,
        workspace_id: str | None = None,
        user_id: str | None = None,
    ) -> list[NotificationRecord]:
        records = self._filter_records(workspace_id=workspace_id, user_id=user_id)
        if not cursor:
            return records
        for index, record in enumerate(records):
            if record.notification_id == cursor:
                return records[index + 1 :]
        return records

    def latest_for_stream(
        self,
        *,
        workspace_id: str | None = None,
        user_id: str | None = None,
    ) -> list[NotificationRecord]:
        return self._filter_records(workspace_id=workspace_id, user_id=user_id)

    def _filter_records(
        self,
        *,
        workspace_id: str | None,
        user_id: str | None,
    ) -> list[NotificationRecord]:
        return [
            record
            for record in self._records
            if _matches_scope(record.workspace_id, workspace_id)
            and _matches_scope(record.user_id, user_id)
        ]


def _matches_scope(record_scope: str | None, requested_scope: str | None) -> bool:
    if record_scope is None:
        return True
    if requested_scope is None:
        return False
    return record_scope == requested_scope


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
