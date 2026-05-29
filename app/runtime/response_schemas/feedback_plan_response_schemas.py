from __future__ import annotations

from typing import Optional

from pydantic import Field

from app.runtime.response_schemas.feedback_output_response_schemas import EvidenceRefResponse
from app.runtime.feedback_schemas import Actionability, Confidence, OptimizationObjectType, ProblemType
from app.runtime.schemas import ExtensibleResponse, FeedbackSourceRef


class FeedbackOptimizationTaskContextResponse(ExtensibleResponse):
    external_system: Optional[str] = None
    mcp_server: Optional[str] = None
    tool_name: Optional[str] = None
    tool_names: list[str] = Field(default_factory=list)
    api_name: Optional[str] = None
    api_path: Optional[str] = None
    api_method: Optional[str] = None
    endpoint: Optional[str] = None
    query_ids: list[str] = Field(default_factory=list)
    alert_ids: list[str] = Field(default_factory=list)
    case_ids: list[str] = Field(default_factory=list)
    asset_ids: list[str] = Field(default_factory=list)
    dates: list[str] = Field(default_factory=list)
    affected_fields: list[str] = Field(default_factory=list)
    observed_issue: Optional[str] = None
    expected_fix: Optional[str] = None
    target_file: Optional[str] = None
    config_section: Optional[str] = None
    symbol: Optional[str] = None


class FeedbackOptimizationAttributionSummaryResponse(ExtensibleResponse):
    attribution_job_id: Optional[str] = None
    feedback_case_id: Optional[str] = None
    problem_type: Optional[ProblemType] = None
    optimization_object_type: Optional[OptimizationObjectType] = None
    actionability: Optional[Actionability] = None
    confidence: Optional[Confidence] = None
    rationale: Optional[str] = None


class FeedbackOptimizationPlanTaskResponse(ExtensibleResponse):
    schema_version: Optional[str] = None
    plan_task_id: str
    source_index: Optional[int] = None
    execution_kind: str
    status: str
    title: Optional[str] = None
    target_type: Optional[str] = None
    target_path: Optional[str] = None
    owner: Optional[str] = None
    actionability: Optional[Actionability] = None
    confidence: Optional[Confidence] = None
    problem_type: Optional[ProblemType] = None
    description: Optional[str] = None
    objective: Optional[str] = None
    target_summary: Optional[str] = None
    task_context: FeedbackOptimizationTaskContextResponse = Field(default_factory=FeedbackOptimizationTaskContextResponse)
    recommendation: Optional[str] = None
    recommended_actions: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    expected_effect: Optional[str] = None
    validation: Optional[str] = None
    risk: Optional[str] = None
    analysis_summary: Optional[str] = None
    evidence_summary: Optional[str] = None
    evidence_refs: list[EvidenceRefResponse] = Field(default_factory=list)
    rationale: Optional[str] = None
    reason: Optional[str] = None
    feedback_case_ids: list[str] = Field(default_factory=list)
    eval_case_ids: list[str] = Field(default_factory=list)
    attribution_job_ids: list[str] = Field(default_factory=list)
    optimization_task_id: Optional[str] = None
    execution_job_id: Optional[str] = None
    external_item_id: Optional[str] = None
    applied_agent_version_id: Optional[str] = None
    latest_webhook_alias: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class FeedbackOptimizationBlockedItemResponse(ExtensibleResponse):
    schema_version: Optional[str] = None
    blocked_item_id: str
    source_index: Optional[int] = None
    status: Optional[str] = None
    title: Optional[str] = None
    target_type: Optional[str] = None
    target_path: Optional[str] = None
    owner: Optional[str] = None
    actionability: Optional[str] = None
    confidence: Optional[str] = None
    problem_type: Optional[str] = None
    analysis_summary: Optional[str] = None
    evidence_summary: Optional[str] = None
    evidence_refs: list[EvidenceRefResponse] = Field(default_factory=list)
    recommendation: Optional[str] = None
    reason: Optional[str] = None
    feedback_case_ids: list[str] = Field(default_factory=list)
    eval_case_ids: list[str] = Field(default_factory=list)
    attribution_job_ids: list[str] = Field(default_factory=list)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class FeedbackOptimizationPlanResponse(ExtensibleResponse):
    schema_version: Optional[str] = None
    optimization_plan_id: Optional[str] = None
    batch_id: Optional[str] = None
    created_at: Optional[str] = None
    status: str
    title: Optional[str] = None
    summary: Optional[str] = None
    problem_types: list[str] = Field(default_factory=list)
    confidence: Optional[str] = None
    actionability: Optional[str] = None
    optimization_object_type: Optional[str] = None
    target_type: Optional[str] = None
    target_path: Optional[str] = None
    recommendation: Optional[str] = None
    expected_effect: Optional[str] = None
    validation: Optional[str] = None
    risk: Optional[str] = None
    rationale: Optional[str] = None
    regeneration_instruction: Optional[str] = None
    source_refs: list[FeedbackSourceRef] = Field(default_factory=list)
    feedback_case_ids: list[str] = Field(default_factory=list)
    eval_case_ids: list[str] = Field(default_factory=list)
    attribution_job_ids: list[str] = Field(default_factory=list)
    attribution_summaries: list[FeedbackOptimizationAttributionSummaryResponse] = Field(default_factory=list)
    evidence_refs: list[EvidenceRefResponse] = Field(default_factory=list)
    tasks: list[FeedbackOptimizationPlanTaskResponse] = Field(default_factory=list)
    task_summary: dict[str, int] = Field(default_factory=dict)
    blocked_items: list[FeedbackOptimizationBlockedItemResponse] = Field(default_factory=list)
    blocked_summary: dict[str, int] = Field(default_factory=dict)
