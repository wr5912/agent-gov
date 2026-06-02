from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Callable, Optional
from urllib import error as urlerror
from urllib import request as urlrequest

import yaml
from sqlalchemy import select

from ..errors import BusinessRuleViolation, ConfigurationError, ConflictError
from ..external_governance_mapping import (
    apply_external_governance_record,
    external_governance_record_from_row,
)
from ..records.external_governance_records import (
    ExternalGovernanceItemRecord,
    ExternalGovernanceNotificationRecord,
    apply_external_governance_notification_record,
)
from ..runtime_db import ExternalGovernanceItemModel, ExternalNotificationModel, utc_now


ExternalWebhookSender = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


class ExternalGovernanceService:
    """External governance query and notification service."""

    def __init__(self, *, session_factory: Any, webhooks_path: Path) -> None:
        self.Session = session_factory
        self.webhooks_path = webhooks_path

    def list_webhooks(self) -> list[dict[str, Any]]:
        if not self.webhooks_path.exists():
            return []
        loaded = self._load_webhook_config()
        webhooks = loaded.get("webhooks") or []
        if not isinstance(webhooks, list):
            raise ConfigurationError("External governance webhook config field webhooks must be a list")
        normalized: list[dict[str, Any]] = []
        for item in webhooks:
            if not isinstance(item, dict):
                continue
            alias = _string(item.get("alias"))
            url = _string(item.get("url"))
            if not alias or not url:
                continue
            normalized.append(
                {
                    "alias": alias,
                    "name": _string(item.get("name")) or alias,
                    "url": url,
                    "has_token": bool(_string(item.get("token"))),
                }
            )
        return normalized

    def list_items(
        self,
        *,
        feedback_case_id: Optional[str] = None,
        proposal_job_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        stmt = select(ExternalGovernanceItemModel).order_by(ExternalGovernanceItemModel.created_at.desc()).limit(limit)
        if feedback_case_id:
            stmt = stmt.where(ExternalGovernanceItemModel.feedback_case_id == feedback_case_id)
        if proposal_job_id:
            stmt = stmt.where(ExternalGovernanceItemModel.proposal_job_id == proposal_job_id)
        if status:
            stmt = stmt.where(ExternalGovernanceItemModel.status == status)
        else:
            stmt = stmt.where(ExternalGovernanceItemModel.status != "superseded")
        with self.Session() as db:
            return [self.item_to_dict(row) for row in db.scalars(stmt).all()]

    def find_item(self, external_item_id: str) -> Optional[dict[str, Any]]:
        if not external_item_id:
            return None
        with self.Session() as db:
            row = db.get(ExternalGovernanceItemModel, external_item_id)
            return self.item_to_dict(row) if row else None

    def notify_item(
        self,
        external_item_id: str,
        *,
        webhook_alias: str,
        sender: Optional[ExternalWebhookSender] = None,
    ) -> Optional[dict[str, Any]]:
        item = self.find_item(external_item_id)
        if not item:
            return None
        if item.get("status") == "superseded":
            raise ConflictError("External governance item is superseded")
        webhook = self.webhook_by_alias(webhook_alias)
        payload = self.notification_payload(item, webhook)
        notification_id = f"egn-{uuid.uuid4()}"
        created_at = utc_now()
        notification = ExternalGovernanceNotificationRecord.sending(
            notification_id=notification_id,
            external_item_id=external_item_id,
            created_at=created_at,
            webhook_alias=webhook["alias"],
            request_json=payload,
        )
        self._insert_notification(notification)
        try:
            response = (sender or self.send_webhook)(webhook, payload)
            http_status = int(response.get("http_status") or 0)
            response_body = _truncate(_string(response.get("response_body")) or "")
            if 200 <= http_status < 300:
                notification = notification.mark_sent(
                    completed_at=utc_now(),
                    http_status=http_status,
                    response_body=response_body,
                )
            else:
                notification = notification.mark_failed(
                    completed_at=utc_now(),
                    http_status=http_status,
                    response_body=response_body,
                )
        except Exception as exc:
            notification = notification.mark_failed(completed_at=utc_now(), error=str(exc))

        with self.Session.begin() as db:
            notification_row = db.get(ExternalNotificationModel, notification_id)
            if notification_row:
                self._apply_notification_record(notification_row, notification)
            else:
                db.add(self._notification_row(notification))
            row = db.get(ExternalGovernanceItemModel, external_item_id)
            if row:
                record = external_governance_record_from_row(row).with_notification(
                    updated_at=utc_now(),
                    notification=notification,
                )
                apply_external_governance_record(row, record)
        return self.find_item(external_item_id)

    def webhook_by_alias(self, alias: str) -> dict[str, Any]:
        requested = _string(alias)
        if not requested:
            raise BusinessRuleViolation("webhook_alias is required")
        if not self.webhooks_path.exists():
            raise ConfigurationError(f"External governance webhook config not found: {self.webhooks_path}")
        loaded = self._load_webhook_config()
        for item in loaded.get("webhooks") or []:
            if not isinstance(item, dict):
                continue
            if _string(item.get("alias")) == requested and _string(item.get("url")):
                return {
                    "alias": requested,
                    "name": _string(item.get("name")) or requested,
                    "url": _string(item.get("url")),
                    "token": _string(item.get("token")),
                    "timeout_seconds": int(item.get("timeout_seconds") or 5),
                }
        raise BusinessRuleViolation(f"Unknown external governance webhook alias: {requested}")

    def notification_payload(self, item: dict[str, Any], webhook: dict[str, Any]) -> dict[str, Any]:
        record = ExternalGovernanceItemRecord.model_validate(item)
        return record.to_notification_payload(webhook_alias=webhook["alias"])

    def send_webhook(self, webhook: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if webhook.get("token"):
            headers["Authorization"] = f"Bearer {webhook['token']}"
        request = urlrequest.Request(
            str(webhook["url"]),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlrequest.urlopen(request, timeout=int(webhook.get("timeout_seconds") or 5)) as response:
                body = response.read(4096).decode("utf-8", errors="replace")
                return {"http_status": response.status, "response_body": body}
        except urlerror.HTTPError as exc:
            body = exc.read(4096).decode("utf-8", errors="replace")
            return {"http_status": exc.code, "response_body": body}

    def item_to_dict(self, row: ExternalGovernanceItemModel) -> dict[str, Any]:
        item = external_governance_record_from_row(row).to_payload()
        with self.Session() as db:
            if row.latest_notification_id:
                notification = db.get(ExternalNotificationModel, row.latest_notification_id)
            else:
                notification = db.scalar(
                    select(ExternalNotificationModel)
                    .where(ExternalNotificationModel.external_item_id == row.external_item_id)
                    .order_by(ExternalNotificationModel.created_at.desc())
                    .limit(1)
                )
        if notification:
            item["latest_notification"] = ExternalGovernanceNotificationRecord.from_row(notification).to_payload()
        return item

    def _insert_notification(self, notification: ExternalGovernanceNotificationRecord) -> None:
        with self.Session.begin() as db:
            db.add(self._notification_row(notification))

    def _notification_row(self, notification: ExternalGovernanceNotificationRecord) -> ExternalNotificationModel:
        return ExternalNotificationModel(
            notification_id=notification.notification_id,
            external_item_id=notification.external_item_id,
            created_at=notification.created_at,
            completed_at=notification.completed_at,
            status=notification.status,
            webhook_alias=notification.webhook_alias,
            http_status=notification.http_status,
            payload_json=notification.to_payload(),
        )

    def _apply_notification_record(
        self,
        row: ExternalNotificationModel,
        notification: ExternalGovernanceNotificationRecord,
    ) -> None:
        apply_external_governance_notification_record(row, notification)

    def _load_webhook_config(self) -> dict[str, Any]:
        try:
            loaded = yaml.safe_load(self.webhooks_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigurationError(f"Invalid external governance webhook config: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ConfigurationError("External governance webhook config must be a mapping")
        return loaded


def _string(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _truncate(value: str, limit: int = 2000) -> str:
    return value if len(value) <= limit else f"{value[:limit]}..."
