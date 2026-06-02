from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field, field_validator, model_validator

from app.runtime.runtime_db import OptimizationProposalModel, ProposalReviewModel
from app.runtime.state_machines import PROPOSAL_STATES, validate_transition

from .json_types import JsonObject, StrictRuntimeRecord


ProposalStatus = Literal[
    "pending_review",
    "approved",
    "rejected",
    "needs_more_analysis",
    "superseded",
]
ProposalReviewAction = Literal["approve", "reject", "request_more_analysis"]


class ProposalReviewRecord(StrictRuntimeRecord):
    """Internal source of truth for one optimization proposal review."""

    review_id: str
    proposal_id: str
    created_at: str
    action: ProposalReviewAction
    status: ProposalStatus
    comment: Optional[str] = None
    source: Optional[str] = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in PROPOSAL_STATES:
            raise ValueError(f"unsupported proposal review status: {value}")
        return value

    @model_validator(mode="after")
    def validate_action_status_pair(self) -> "ProposalReviewRecord":
        expected = {
            "approve": "approved",
            "reject": "rejected",
            "request_more_analysis": "needs_more_analysis",
        }[self.action]
        if self.status != expected:
            raise ValueError(f"proposal review action {self.action!r} requires status {expected!r}")
        return self

    def to_payload(self) -> JsonObject:
        return self.model_dump(mode="json")

    @classmethod
    def from_row(cls, row: ProposalReviewModel) -> "ProposalReviewRecord":
        payload = dict(row.payload_json or {})
        payload.update(
            {
                "review_id": row.review_id,
                "proposal_id": row.proposal_id,
                "created_at": row.created_at,
                "action": row.action,
                "status": row.status,
            }
        )
        return cls.model_validate(payload)


class OptimizationProposalRecord(StrictRuntimeRecord):
    """Internal source of truth for optimization proposal payload_json."""

    proposal_id: str
    feedback_case_id: str
    proposal_job_id: str
    status: ProposalStatus
    created_at: str
    actionability: Optional[str] = None
    target_type: Optional[str] = None
    target_path: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    objective: Optional[str] = None
    target_summary: Optional[str] = None
    task_context: JsonObject = Field(default_factory=dict)
    recommendation: Optional[str] = None
    recommended_actions: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    expected_effect: Optional[str] = None
    validation: Optional[str] = None
    risk: Optional[str] = None
    analysis_summary: Optional[str] = None
    evidence_summary: Optional[str] = None
    evidence_refs: list[JsonObject] = Field(default_factory=list)
    regeneration_instruction: Optional[str] = None
    requires_approval: Optional[bool] = None
    base_agent_version_id: Optional[str] = None
    source_batch_id: Optional[str] = None
    source_plan_task_id: Optional[str] = None
    source_feedback_case_ids: list[str] = Field(default_factory=list)
    source_refs: list[JsonObject] = Field(default_factory=list)
    superseded_at: Optional[str] = None
    superseded_reason: Optional[str] = None
    superseded_by_job_id: Optional[str] = None
    latest_review: Optional[ProposalReviewRecord] = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in PROPOSAL_STATES:
            raise ValueError(f"unsupported optimization proposal status: {value}")
        return value

    @field_validator("recommended_actions", "acceptance_criteria", "source_feedback_case_ids")
    @classmethod
    def validate_string_list(cls, value: list[str]) -> list[str]:
        return [str(item) for item in value if item]

    @model_validator(mode="after")
    def validate_proposal_shape(self) -> "OptimizationProposalRecord":
        if not self.target_path:
            raise ValueError("optimization proposal must include target_path")
        if self.status == "superseded" and (
            not self.superseded_at or not self.superseded_reason or not self.superseded_by_job_id
        ):
            raise ValueError("superseded proposals require superseded metadata")
        if self.latest_review and self.latest_review.proposal_id != self.proposal_id:
            raise ValueError("latest_review.proposal_id must match proposal_id")
        return self

    def transition_to(
        self,
        status: str,
        *,
        fields: JsonObject | None = None,
    ) -> "OptimizationProposalRecord":
        validate_transition("proposal", self.status, status)
        payload = self.to_payload()
        payload.update(fields or {})
        payload["status"] = status
        return type(self).model_validate(payload)

    def to_payload(self) -> JsonObject:
        return self.model_dump(mode="json")

    @classmethod
    def from_row(
        cls,
        row: OptimizationProposalModel,
        *,
        latest_review: ProposalReviewRecord | None = None,
    ) -> "OptimizationProposalRecord":
        payload = dict(row.payload_json or {})
        payload.update(
            {
                "proposal_id": row.proposal_id,
                "feedback_case_id": row.feedback_case_id,
                "proposal_job_id": row.proposal_job_id,
                "status": row.status,
                "created_at": row.created_at,
                "actionability": row.actionability if row.actionability is not None else payload.get("actionability"),
                "target_path": row.target_path if row.target_path is not None else payload.get("target_path"),
            }
        )
        if latest_review is not None:
            payload["latest_review"] = latest_review.to_payload()
        return cls.model_validate(payload)
