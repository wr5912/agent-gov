from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field

from app.runtime.feedback_schemas import Actionability, Confidence, OptimizationObjectType, ProblemType
from app.runtime.schemas import ExtensibleResponse


class EvidenceRefResponse(ExtensibleResponse):
    type: str
    id: str
    reason: str


class ResponsibilityBoundaryResponse(ExtensibleResponse):
    owner: str
    reason: str


class AttributionOutputResponse(ExtensibleResponse):
    schema_version: str
    feedback_case_id: str
    attribution_job_id: str
    status: Literal["completed", "needs_human_review"] = "completed"
    problem_type: ProblemType
    optimization_object_type: OptimizationObjectType
    actionability: Actionability
    confidence: Confidence
    human_review_required: bool
    evidence_refs: list[EvidenceRefResponse] = Field(default_factory=list)
    responsibility_boundary: ResponsibilityBoundaryResponse
    rationale: str
    recommended_next_step: Literal["generate_proposal", "needs_human_review", "stop"] = "generate_proposal"


class ProposalItemResponse(ExtensibleResponse):
    proposal_id: Optional[str] = None
    created_at: Optional[str] = None
    feedback_case_id: Optional[str] = None
    proposal_job_id: Optional[str] = None
    status: Optional[str] = None
    title: str
    actionability: Actionability
    target_type: str
    target_path: Optional[str] = None
    recommendation: str
    expected_effect: str
    validation: str
    risk: str
    requires_approval: bool = True
    base_agent_version_id: Optional[str] = None


class ExternalGuidanceResponse(ExtensibleResponse):
    owner: str
    actionability: Actionability
    recommendation: str
    reason: Optional[str] = None
    external_item_id: Optional[str] = None
    source_index: Optional[int] = None
    status: Optional[str] = None
    latest_notification_id: Optional[str] = None
    latest_webhook_alias: Optional[str] = None


class ProposalOutputResponse(ExtensibleResponse):
    schema_version: str
    feedback_case_id: str
    proposal_job_id: str
    status: Literal["completed", "needs_human_review"] = "completed"
    proposals: list[ProposalItemResponse] = Field(default_factory=list)
    external_guidance: list[ExternalGuidanceResponse] = Field(default_factory=list)
    no_action_reason: Optional[str] = None
