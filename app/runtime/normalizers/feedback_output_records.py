from __future__ import annotations

from typing import Any, Optional

from pydantic import ConfigDict, Field

from app.runtime.records.json_types import JsonObject, StrictRuntimeRecord


class NormalizedOutputRecord(StrictRuntimeRecord):
    """Base for normalized agent outputs before strict schema validation."""

    model_config = ConfigDict(extra="allow")

    def to_payload(self) -> JsonObject:
        return self.model_dump(mode="json")


class NormalizedAttributionOutput(NormalizedOutputRecord):
    schema_version: Optional[str] = None
    evidence_refs: Any = Field(default_factory=list)
    responsibility_boundary: Any = None


class NormalizedProposalOutput(NormalizedOutputRecord):
    schema_version: Optional[str] = None
    proposals: list[Any] = Field(default_factory=list)
    external_guidance: list[Any] = Field(default_factory=list)


class NormalizedTaskContext(NormalizedOutputRecord):
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


class NormalizedOptimizationPlanTask(NormalizedOutputRecord):
    source_index: int = 0
    execution_kind: str
    status: str
    target_type: str
    task_context: JsonObject = Field(default_factory=dict)
    evidence_refs: list[JsonObject] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    feedback_case_ids: list[str] = Field(default_factory=list)
    eval_case_ids: list[str] = Field(default_factory=list)
    attribution_job_ids: list[str] = Field(default_factory=list)


class NormalizedBlockedOptimizationItem(NormalizedOutputRecord):
    source_index: int = 0
    status: str = "blocked"
    title: str = "未形成可执行优化任务"
    target_type: str = "not_actionable"
    actionability: str = "needs_human_analysis"
    reason: str
    task_context: JsonObject = Field(default_factory=dict)
    evidence_refs: list[JsonObject] = Field(default_factory=list)
    feedback_case_ids: list[str] = Field(default_factory=list)
    eval_case_ids: list[str] = Field(default_factory=list)
    attribution_job_ids: list[str] = Field(default_factory=list)


class NormalizedFeedbackOptimizationPlanOutput(NormalizedOutputRecord):
    schema_version: str
    status: str
    confidence: str
    actionability: str
    tasks: list[JsonObject] = Field(default_factory=list)
    blocked_items: list[JsonObject] = Field(default_factory=list)
    feedback_case_ids: list[str] = Field(default_factory=list)
    eval_case_ids: list[str] = Field(default_factory=list)
    attribution_job_ids: list[str] = Field(default_factory=list)
    attribution_summaries: list[JsonObject] = Field(default_factory=list)
    problem_types: list[str] = Field(default_factory=list)
    evidence_refs: list[JsonObject] = Field(default_factory=list)


class NormalizedExecutionPlanOutput(NormalizedOutputRecord):
    status: str
    operations: list[Any] = Field(default_factory=list)


class NormalizedFeedbackEvalCaseGenerationOutput(NormalizedOutputRecord):
    schema_version: str
    eval_cases: list[JsonObject] = Field(default_factory=list)
    status: str


class NormalizedRegressionImpactAnalysisOutput(NormalizedOutputRecord):
    schema_version: str
    gate_result: JsonObject = Field(default_factory=dict)
    impacted_assets: list[JsonObject] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    status: str
