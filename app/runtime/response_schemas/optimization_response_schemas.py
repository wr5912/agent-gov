from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from app.runtime.schemas import ExtensibleResponse


class OptimizationProposalReviewRecordResponse(ExtensibleResponse):
    review_id: str
    proposal_id: str
    created_at: str
    action: str
    status: str
    comment: Optional[str] = None


class OptimizationProposalResponse(ExtensibleResponse):
    proposal_id: str
    feedback_case_id: str
    proposal_job_id: str
    status: str
    created_at: Optional[str] = None
    actionability: Optional[str] = None
    target_type: Optional[str] = None
    target_path: Optional[str] = None
    title: Optional[str] = None
    recommendation: Optional[str] = None
    expected_effect: Optional[str] = None
    validation: Optional[str] = None
    risk: Optional[str] = None
    requires_approval: Optional[bool] = None
    base_agent_version_id: Optional[str] = None
    latest_review: Optional[OptimizationProposalReviewRecordResponse] = None


class OptimizationProposalReviewResponse(BaseModel):
    proposal: OptimizationProposalResponse
    review: OptimizationProposalReviewRecordResponse
