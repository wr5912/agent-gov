from __future__ import annotations

from typing import Literal, Optional

from pydantic import ConfigDict, Field, model_validator

from .json_types import JsonObject, StrictRuntimeRecord


class ExtensiblePlanRecord(StrictRuntimeRecord):
    """Base for persisted plan payloads that may carry forward agent extras."""

    model_config = ConfigDict(extra="allow")


class FeedbackOptimizationTaskContextRecord(ExtensiblePlanRecord):
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

    def to_payload(self) -> JsonObject:
        return self.model_dump(mode="json", exclude_none=True)


class FeedbackOptimizationPlanTaskRecord(ExtensiblePlanRecord):
    schema_version: str = "feedback-optimization-plan-task/v2"
    plan_task_id: str
    source_index: int = 0
    execution_kind: Literal["workspace_execution", "external_webhook"]
    status: str
    title: str
    description: str = ""
    objective: str = ""
    target_summary: Optional[str] = None
    target_type: str
    target_path: Optional[str] = None
    owner: Optional[str] = None
    actionability: str = "needs_human_analysis"
    confidence: Optional[str] = None
    problem_type: Optional[str] = None
    task_context: JsonObject = Field(default_factory=dict)
    recommendation: str = ""
    recommended_actions: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    expected_effect: str = ""
    validation: str = ""
    risk: str = ""
    analysis_summary: Optional[str] = None
    evidence_summary: Optional[str] = None
    evidence_refs: list[JsonObject] = Field(default_factory=list)
    rationale: Optional[str] = None
    reason: Optional[str] = None
    feedback_case_ids: list[str] = Field(default_factory=list)
    eval_case_ids: list[str] = Field(default_factory=list)
    attribution_job_ids: list[str] = Field(default_factory=list)
    internal_proposal_id: Optional[str] = None
    optimization_task_id: Optional[str] = None
    execution_job_id: Optional[str] = None
    latest_execution_job: Optional[JsonObject] = None
    external_item_id: Optional[str] = None
    latest_webhook_alias: Optional[str] = None
    latest_notification: Optional[JsonObject] = None
    applied_agent_version_id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @model_validator(mode="after")
    def validate_task_shape(self) -> "FeedbackOptimizationPlanTaskRecord":
        if not self.plan_task_id.strip():
            raise ValueError("plan_task_id cannot be empty")
        if not self.status.strip():
            raise ValueError("plan task status cannot be empty")
        if not self.title.strip():
            raise ValueError("plan task title cannot be empty")
        if not self.target_type.strip():
            raise ValueError("plan task target_type cannot be empty")
        if self.execution_kind == "workspace_execution" and not self.target_path:
            raise ValueError("workspace_execution plan task must include target_path")
        return self

    def to_payload(self) -> JsonObject:
        return self.model_dump(mode="json", exclude_none=True)


class FeedbackOptimizationBlockedItemRecord(ExtensiblePlanRecord):
    schema_version: str = "feedback-optimization-blocked-item/v1"
    blocked_item_id: str
    source_index: int = 0
    status: str = "blocked"
    title: str = "未形成可执行优化任务"
    target_type: str = "not_actionable"
    target_path: Optional[str] = None
    owner: Optional[str] = None
    actionability: str = "needs_human_analysis"
    confidence: Optional[str] = None
    problem_type: Optional[str] = None
    analysis_summary: Optional[str] = None
    evidence_summary: Optional[str] = None
    evidence_refs: list[JsonObject] = Field(default_factory=list)
    recommendation: Optional[str] = None
    reason: str
    feedback_case_ids: list[str] = Field(default_factory=list)
    eval_case_ids: list[str] = Field(default_factory=list)
    attribution_job_ids: list[str] = Field(default_factory=list)
    task_context: JsonObject = Field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @model_validator(mode="after")
    def validate_blocked_shape(self) -> "FeedbackOptimizationBlockedItemRecord":
        if not self.blocked_item_id.strip():
            raise ValueError("blocked_item_id cannot be empty")
        if not self.reason.strip():
            raise ValueError("blocked item reason cannot be empty")
        return self

    def to_payload(self) -> JsonObject:
        return self.model_dump(mode="json", exclude_none=True)


class FeedbackOptimizationPlanRecord(ExtensiblePlanRecord):
    schema_version: str = "feedback-optimization-plan/v1"
    optimization_plan_id: str
    batch_id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    status: str
    title: str
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
    source_refs: list[JsonObject] = Field(default_factory=list)
    feedback_case_ids: list[str] = Field(default_factory=list)
    eval_case_ids: list[str] = Field(default_factory=list)
    attribution_job_ids: list[str] = Field(default_factory=list)
    attribution_summaries: list[JsonObject] = Field(default_factory=list)
    evidence_refs: list[JsonObject] = Field(default_factory=list)
    tasks: list[FeedbackOptimizationPlanTaskRecord] = Field(default_factory=list)
    task_summary: JsonObject = Field(default_factory=dict)
    blocked_items: list[FeedbackOptimizationBlockedItemRecord] = Field(default_factory=list)
    blocked_summary: JsonObject = Field(default_factory=dict)
    source_output_schema_version: Optional[str] = None
    optimization_plan_job_id: Optional[str] = None
    generated_by: Optional[str] = None
    internal_proposal_id: Optional[str] = None
    approved_at: Optional[str] = None
    approval_comment: Optional[str] = None
    rejected_at: Optional[str] = None
    rejection_comment: Optional[str] = None

    @model_validator(mode="after")
    def validate_plan_shape(self) -> "FeedbackOptimizationPlanRecord":
        if not self.optimization_plan_id.strip():
            raise ValueError("optimization_plan_id cannot be empty")
        if not self.status.strip():
            raise ValueError("optimization plan status cannot be empty")
        if not self.title.strip():
            raise ValueError("optimization plan title cannot be empty")
        if not self.tasks and not self.blocked_items:
            raise ValueError("optimization plan must include tasks or blocked_items")
        return self

    def to_payload(self) -> JsonObject:
        return self.model_dump(mode="json", exclude_none=True)
