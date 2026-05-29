from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


EXTERNAL_GOVERNANCE_ITEM_SCHEMA_VERSION = "external-governance-item/v1"

ExternalGovernanceItemStatus = Literal[
    "pending_notification",
    "notification_failed",
    "notified",
    "superseded",
]
ExternalNotificationStatus = Literal["sending", "sent", "failed"]


class ExternalGovernanceNotificationRecord(BaseModel):
    """Internal model for the latest external notification payload."""

    model_config = ConfigDict(extra="allow")

    notification_id: str
    external_item_id: str
    created_at: str
    completed_at: Optional[str] = None
    status: ExternalNotificationStatus
    webhook_alias: str
    request_json: dict[str, Any] = Field(default_factory=dict)
    http_status: Optional[int] = None
    response_body: Optional[str] = None
    error: Optional[str] = None

    @classmethod
    def sending(
        cls,
        *,
        notification_id: str,
        external_item_id: str,
        created_at: str,
        webhook_alias: str,
        request_json: dict[str, Any],
    ) -> "ExternalGovernanceNotificationRecord":
        return cls(
            notification_id=notification_id,
            external_item_id=external_item_id,
            created_at=created_at,
            status="sending",
            webhook_alias=webhook_alias,
            request_json=request_json,
        )

    def mark_sent(
        self,
        *,
        completed_at: str,
        http_status: int,
        response_body: str,
    ) -> "ExternalGovernanceNotificationRecord":
        payload = self.to_payload()
        payload.update(
            {
                "completed_at": completed_at,
                "status": "sent",
                "http_status": http_status,
                "response_body": response_body,
                "error": None,
            }
        )
        return type(self).model_validate(payload)

    def mark_failed(
        self,
        *,
        completed_at: str,
        error: str | None = None,
        http_status: int | None = None,
        response_body: str | None = None,
    ) -> "ExternalGovernanceNotificationRecord":
        payload = self.to_payload()
        payload.update(
            {
                "completed_at": completed_at,
                "status": "failed",
                "error": error,
                "http_status": http_status,
                "response_body": response_body,
            }
        )
        return type(self).model_validate(payload)

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class ExternalGovernanceItemRecord(BaseModel):
    """Internal source of truth for external governance item payloads."""

    model_config = ConfigDict(extra="allow")

    schema_version: Literal["external-governance-item/v1"] = EXTERNAL_GOVERNANCE_ITEM_SCHEMA_VERSION
    external_item_id: str
    created_at: str
    updated_at: str
    status: ExternalGovernanceItemStatus
    feedback_case_id: str
    proposal_job_id: str
    source_index: int = 0
    owner: str
    actionability: str
    recommendation: str = ""
    reason: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    objective: Optional[str] = None
    target_summary: Optional[str] = None
    task_context: dict[str, Any] = Field(default_factory=dict)
    recommended_actions: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    expected_effect: Optional[str] = None
    validation: Optional[str] = None
    risk: Optional[str] = None
    analysis_summary: Optional[str] = None
    evidence_summary: Optional[str] = None
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    source: Optional[str] = None
    batch_id: Optional[str] = None
    optimization_plan_id: Optional[str] = None
    plan_task_id: Optional[str] = None
    target_type: Optional[str] = None
    target_path: Optional[str] = None
    feedback_case_ids: list[str] = Field(default_factory=list)
    eval_case_ids: list[str] = Field(default_factory=list)
    source_attribution_job_ids: list[str] = Field(default_factory=list)
    latest_notification_id: Optional[str] = None
    latest_webhook_alias: Optional[str] = None
    latest_notification: Optional[ExternalGovernanceNotificationRecord] = None
    superseded_at: Optional[str] = None
    superseded_reason: Optional[str] = None
    superseded_by_job_id: Optional[str] = None

    def to_notification_payload(self, *, webhook_alias: str) -> dict[str, Any]:
        payload = {
            "schema_version": "external-governance-notification/v1",
            "webhook_alias": webhook_alias,
            "external_item_id": self.external_item_id,
            "feedback_case_id": self.feedback_case_id,
            "proposal_job_id": self.proposal_job_id,
            "title": self.title,
            "description": self.description,
            "objective": self.objective,
            "target_summary": self.target_summary,
            "owner": self.owner,
            "actionability": self.actionability,
            "recommendation": self.recommendation,
            "recommended_actions": self.recommended_actions,
            "acceptance_criteria": self.acceptance_criteria,
            "expected_effect": self.expected_effect,
            "validation": self.validation,
            "risk": self.risk,
            "analysis_summary": self.analysis_summary,
            "evidence_summary": self.evidence_summary,
            "evidence_refs": self.evidence_refs,
            "reason": self.reason,
            "created_at": self.created_at,
        }
        optional_fields = (
            "source",
            "batch_id",
            "optimization_plan_id",
            "plan_task_id",
            "target_type",
            "target_path",
            "task_context",
            "feedback_case_ids",
            "eval_case_ids",
            "source_attribution_job_ids",
        )
        item_payload = self.to_payload()
        for key in optional_fields:
            if item_payload.get(key) is not None:
                payload[key] = item_payload[key]
        return payload

    def with_notification(
        self,
        *,
        updated_at: str,
        notification: ExternalGovernanceNotificationRecord,
    ) -> "ExternalGovernanceItemRecord":
        payload = self.to_payload()
        payload.update(
            {
                "updated_at": updated_at,
                "status": "notified" if notification.status == "sent" else "notification_failed",
                "latest_notification_id": notification.notification_id,
                "latest_webhook_alias": notification.webhook_alias,
                "latest_notification": notification.to_payload(),
            }
        )
        return type(self).model_validate(payload)

    def mark_superseded(
        self,
        *,
        updated_at: str,
        reason: str,
        superseded_by_job_id: str,
    ) -> "ExternalGovernanceItemRecord":
        payload = self.to_payload()
        payload.update(
            {
                "updated_at": updated_at,
                "status": "superseded",
                "superseded_at": updated_at,
                "superseded_reason": reason,
                "superseded_by_job_id": superseded_by_job_id,
            }
        )
        return type(self).model_validate(payload)

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
