from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field, field_validator, model_validator

from app.runtime.runtime_db import FeedbackCaseModel
from app.runtime.state_machines import CASE_STATES, validate_transition

from .json_types import StrictRuntimeRecord


FeedbackCaseStatus = Literal[
    "pending_evidence",
    "pending_attribution",
    "attribution_queued",
    "pending_proposal",
    "proposal_queued",
    "pending_review",
    "needs_human_review",
]


class FeedbackCaseRecord(StrictRuntimeRecord):
    """Internal source of truth for one feedback case row."""

    feedback_case_id: str
    created_at: str
    updated_at: str
    status: FeedbackCaseStatus
    title: str
    priority: str
    source_ids: list[str] = Field(default_factory=list)
    signal_ids: list[str] = Field(default_factory=list)
    event_ids: list[str] = Field(default_factory=list)
    pending_correlation_ids: list[str] = Field(default_factory=list)
    run_ids: list[str] = Field(default_factory=list)
    session_ids: list[str] = Field(default_factory=list)
    alert_ids: list[str] = Field(default_factory=list)
    case_ids: list[str] = Field(default_factory=list)
    evidence_package_ids: list[str] = Field(default_factory=list)
    attribution_job_ids: list[str] = Field(default_factory=list)
    proposal_job_ids: list[str] = Field(default_factory=list)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in CASE_STATES:
            raise ValueError(f"unsupported feedback case status: {value}")
        return value

    @field_validator(
        "source_ids",
        "signal_ids",
        "event_ids",
        "pending_correlation_ids",
        "run_ids",
        "session_ids",
        "alert_ids",
        "case_ids",
        "evidence_package_ids",
        "attribution_job_ids",
        "proposal_job_ids",
    )
    @classmethod
    def validate_string_list(cls, value: list[str]) -> list[str]:
        return [str(item) for item in value if item]

    @model_validator(mode="after")
    def validate_case_shape(self) -> "FeedbackCaseRecord":
        if not self.source_ids:
            raise ValueError("feedback case must include source_ids")
        if not self.title.strip():
            raise ValueError("feedback case title cannot be empty")
        return self

    def update(
        self,
        *,
        updated_at: str,
        status: str | None = None,
        evidence_package_id: Optional[str] = None,
        attribution_job_id: Optional[str] = None,
        proposal_job_id: Optional[str] = None,
    ) -> "FeedbackCaseRecord":
        payload = self.to_payload()
        payload["updated_at"] = updated_at
        if status:
            validate_transition("case", self.status, status)
            payload["status"] = status
        if evidence_package_id:
            payload["evidence_package_ids"] = [evidence_package_id]
        if attribution_job_id:
            payload["attribution_job_ids"] = [attribution_job_id]
        if proposal_job_id:
            payload["proposal_job_ids"] = [proposal_job_id]
        return type(self).model_validate(payload)

    def to_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json")

    @classmethod
    def from_row(cls, row: FeedbackCaseModel) -> "FeedbackCaseRecord":
        return cls.model_validate(
            {
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
        )
