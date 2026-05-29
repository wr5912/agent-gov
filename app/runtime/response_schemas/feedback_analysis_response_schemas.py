from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel

from app.runtime.response_schemas.error_response_schemas import FeedbackJobErrorResponse
from app.runtime.response_schemas.feedback_output_response_schemas import AttributionOutputResponse, ProposalOutputResponse
from app.runtime.response_schemas.feedback_plan_response_schemas import FeedbackOptimizationPlanResponse


FeedbackAnalysisValidatedOutputResponse = AttributionOutputResponse | ProposalOutputResponse | FeedbackOptimizationPlanResponse


class FeedbackAnalysisJobResponse(BaseModel):
    job_id: str
    job_type: str
    feedback_case_id: str
    evidence_package_id: str
    status: str
    profile_name: str
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    input_path: str
    raw_output_path: str
    validated_output_path: str
    error_path: str
    langfuse_trace_id: Optional[str] = None
    input_json: Optional[dict[str, Any]] = None
    raw_output_json: Optional[dict[str, Any]] = None
    validated_output_json: Optional[FeedbackAnalysisValidatedOutputResponse] = None
    error_json: Optional[FeedbackJobErrorResponse] = None
