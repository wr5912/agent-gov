from __future__ import annotations

from typing import Optional

from pydantic import ConfigDict, Field
from pydantic.types import JsonValue

from app.runtime.records.json_types import JsonObject, StrictRuntimeRecord


class NormalizedOutputRecord(StrictRuntimeRecord):
    """Base for normalized agent outputs before strict schema validation."""

    model_config = ConfigDict(extra="allow")

    def to_payload(self) -> JsonObject:
        return self.model_dump(mode="json", exclude_none=True)


class NormalizedEvidenceRef(NormalizedOutputRecord):
    type: JsonValue = None
    id: JsonValue = None
    reason: JsonValue = None


class NormalizedResponsibilityBoundary(NormalizedOutputRecord):
    owner: JsonValue = None
    reason: JsonValue = None


class NormalizedProposalItem(NormalizedOutputRecord):
    proposal_id: JsonValue = None
    title: JsonValue = None
    actionability: JsonValue = None
    target_type: JsonValue = None
    target_path: JsonValue = None
    recommendation: JsonValue = None
    expected_effect: JsonValue = None
    validation: JsonValue = None
    risk: JsonValue = None
    requires_approval: JsonValue = None
    base_agent_version_id: JsonValue = None


class NormalizedExternalGuidanceItem(NormalizedOutputRecord):
    owner: JsonValue = None
    actionability: JsonValue = None
    recommendation: JsonValue = None
    reason: JsonValue = None


class NormalizedExecutionOperation(NormalizedOutputRecord):
    operation: JsonValue = None
    path: JsonValue = None
    expected_sha256: JsonValue = None
    content: JsonValue = None
    append_text: JsonValue = None
    rationale: JsonValue = None


class NormalizedAttributionSummary(NormalizedOutputRecord):
    attribution_job_id: JsonValue = None
    feedback_case_id: JsonValue = None
    problem_type: JsonValue = None
    optimization_object_type: JsonValue = None
    actionability: JsonValue = None
    confidence: JsonValue = None
    rationale: JsonValue = None
    summary: JsonValue = None


class NormalizedGeneratedEvalCase(NormalizedOutputRecord):
    schema_version: JsonValue = None
    eval_case_id: JsonValue = None
    status: JsonValue = None
    source: JsonValue = None
    source_feedback_case_id: JsonValue = None
    source_run_id: JsonValue = None
    source_kind: JsonValue = None
    source_id: JsonValue = None
    asset_layer: JsonValue = None
    promotion_status: JsonValue = None
    blocking_policy: JsonValue = None
    scenario_pack: JsonValue = None
    severity: JsonValue = None
    flaky_status: JsonValue = None
    variant_role: JsonValue = None
    prompt: JsonValue = None
    expected_behavior: JsonValue = None
    checks_json: JsonObject = Field(default_factory=dict)
    labels: list[str] = Field(default_factory=list)
    source_summary: JsonValue = None
    attribution_summary: JsonValue = None
    proposal_summary: JsonValue = None


class NormalizedSummaryItem(NormalizedOutputRecord):
    summary: JsonValue = None


class NormalizedAttributionOutput(NormalizedOutputRecord):
    schema_version: Optional[str] = None
    evidence_refs: list[NormalizedEvidenceRef] = Field(default_factory=list)
    responsibility_boundary: NormalizedResponsibilityBoundary | None = None


class NormalizedProposalOutput(NormalizedOutputRecord):
    schema_version: Optional[str] = None
    proposals: list[NormalizedProposalItem] = Field(default_factory=list)
    external_guidance: list[NormalizedExternalGuidanceItem] = Field(default_factory=list)


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
    task_context: NormalizedTaskContext = Field(default_factory=NormalizedTaskContext)
    evidence_refs: list[NormalizedEvidenceRef] = Field(default_factory=list)
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
    task_context: NormalizedTaskContext = Field(default_factory=NormalizedTaskContext)
    evidence_refs: list[NormalizedEvidenceRef] = Field(default_factory=list)
    feedback_case_ids: list[str] = Field(default_factory=list)
    eval_case_ids: list[str] = Field(default_factory=list)
    attribution_job_ids: list[str] = Field(default_factory=list)


class NormalizedFeedbackOptimizationPlanOutput(NormalizedOutputRecord):
    schema_version: str
    status: str
    confidence: str
    actionability: str
    tasks: list[NormalizedOptimizationPlanTask] = Field(default_factory=list)
    blocked_items: list[NormalizedBlockedOptimizationItem] = Field(default_factory=list)
    feedback_case_ids: list[str] = Field(default_factory=list)
    eval_case_ids: list[str] = Field(default_factory=list)
    attribution_job_ids: list[str] = Field(default_factory=list)
    attribution_summaries: list[NormalizedAttributionSummary] = Field(default_factory=list)
    problem_types: list[str] = Field(default_factory=list)
    evidence_refs: list[NormalizedEvidenceRef] = Field(default_factory=list)


class NormalizedExecutionPlanOutput(NormalizedOutputRecord):
    status: str
    operations: list[NormalizedExecutionOperation] = Field(default_factory=list)


class NormalizedFeedbackEvalCaseGenerationOutput(NormalizedOutputRecord):
    schema_version: str
    eval_cases: list[NormalizedGeneratedEvalCase] = Field(default_factory=list)
    status: str


class NormalizedRegressionImpactAnalysisOutput(NormalizedOutputRecord):
    schema_version: str
    gate_result: JsonObject = Field(default_factory=dict)
    impacted_assets: list[NormalizedSummaryItem] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    status: str
