from __future__ import annotations

from typing import Optional

from pydantic import ConfigDict, Field, field_validator
from pydantic.types import JsonValue

from app.runtime.json_types import JsonObject
from app.runtime.records.base import StrictRuntimeRecord


class NormalizedOutputRecord(StrictRuntimeRecord):
    """Base for normalized agent outputs before strict schema validation."""

    model_config = ConfigDict(extra="ignore")

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
    source_refs: list[JsonObject] = Field(default_factory=list)
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
    labels: Optional[list[str]] = None
    source_summary: JsonValue = None
    attribution_summary: JsonValue = None
    proposal_summary: JsonValue = None


class NormalizedSummaryItem(NormalizedOutputRecord):
    summary: JsonValue = None
    eval_case_id: JsonValue = None
    asset_id: JsonValue = None
    status: JsonValue = None
    asset_layer: JsonValue = None
    blocking_policy: JsonValue = None
    labels: Optional[list[str]] = None
    answer_summary: JsonValue = None


class NormalizedPlanStatusValue(StrictRuntimeRecord):
    value: str = "pending_execution"

    @field_validator("value", mode="before")
    @classmethod
    def normalize_value(cls, value: object) -> str:
        status_value = str(value or "").strip().lower()
        if status_value in {"completed", "ready", "approved", "pending_review", "pending_approval", "pending_execution", "execution_ready"}:
            return "pending_execution"
        if status_value in {"needs_review", "manual_review", "blocked", "failed", "needs_human_review"}:
            return "needs_human_review"
        return "pending_execution"


class NormalizedConfidenceValue(StrictRuntimeRecord):
    value: str = "medium"

    @field_validator("value", mode="before")
    @classmethod
    def normalize_value(cls, value: object) -> str:
        confidence = str(value or "").strip().lower()
        return confidence if confidence in {"high", "medium", "low"} else "medium"


class NormalizedActionabilityValue(StrictRuntimeRecord):
    value: str = "needs_human_analysis"

    @field_validator("value", mode="before")
    @classmethod
    def normalize_value(cls, value: object) -> str:
        actionability = str(value or "").strip()
        aliases = {
            "manual_review": "needs_human_analysis",
            "human_review": "needs_human_analysis",
            "agent_behavior": "direct_workspace_change",
            "workspace_change": "direct_workspace_change",
            "external": "external_guidance",
            "external_task": "external_guidance",
            "eval_case_promotion": "regression_asset_governance",
            "regression_asset": "regression_asset_governance",
            "not_applicable": "not_actionable",
        }
        actionability = aliases.get(actionability, actionability)
        allowed = {
            "direct_workspace_change",
            "workspace_config_change",
            "eval_only",
            "external_guidance",
            "runtime_fix",
            "regression_asset_governance",
            "needs_human_analysis",
            "not_actionable",
        }
        return actionability if actionability in allowed else "needs_human_analysis"


class NormalizedProblemTypeValue(StrictRuntimeRecord):
    value: str = "insufficient_information"

    @field_validator("value", mode="before")
    @classmethod
    def normalize_value(cls, value: object) -> str:
        problem_type = str(value or "").strip()
        aliases = {
            "tool_usage_deficiency": "tool_data_quality",
            "tool_usage_gap": "tool_data_quality",
            "tool_call_gap": "tool_data_quality",
            "agent_behavior": "instruction_gap",
        }
        problem_type = aliases.get(problem_type, problem_type)
        allowed = {
            "evidence_gap",
            "tool_misuse",
            "tool_unavailable",
            "tool_data_quality",
            "output_style_issue",
            "instruction_gap",
            "skill_gap",
            "mcp_description_gap",
            "runtime_error",
            "external_soc_process_issue",
            "user_misunderstanding",
            "insufficient_information",
        }
        return problem_type if problem_type in allowed else "insufficient_information"


class NormalizedAttributionOutput(NormalizedOutputRecord):
    feedback_case_id: JsonValue = None
    attribution_job_id: JsonValue = None
    status: JsonValue = None
    problem_type: JsonValue = None
    optimization_object_type: JsonValue = None
    actionability: JsonValue = None
    confidence: JsonValue = None
    human_review_required: JsonValue = None
    evidence_refs: list[NormalizedEvidenceRef] = Field(default_factory=list)
    responsibility_boundary: NormalizedResponsibilityBoundary | None = None
    rationale: JsonValue = None
    recommended_next_step: JsonValue = None


class NormalizedProposalOutput(NormalizedOutputRecord):
    feedback_case_id: JsonValue = None
    proposal_job_id: JsonValue = None
    status: JsonValue = None
    proposals: list[NormalizedProposalItem] = Field(default_factory=list)
    external_guidance: list[NormalizedExternalGuidanceItem] = Field(default_factory=list)
    no_action_reason: JsonValue = None


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
    plan_task_id: JsonValue = None
    source_index: int = 0
    execution_kind: str
    status: str
    internal_action: JsonValue = None
    title: JsonValue = None
    description: JsonValue = None
    objective: JsonValue = None
    target_summary: JsonValue = None
    target_type: str
    target_path: JsonValue = None
    owner: JsonValue = None
    actionability: JsonValue = None
    confidence: JsonValue = None
    problem_type: JsonValue = None
    task_context: NormalizedTaskContext = Field(default_factory=NormalizedTaskContext)
    recommendation: JsonValue = None
    evidence_refs: list[NormalizedEvidenceRef] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    expected_effect: JsonValue = None
    validation: JsonValue = None
    risk: JsonValue = None
    analysis_summary: JsonValue = None
    evidence_summary: JsonValue = None
    rationale: JsonValue = None
    reason: JsonValue = None
    feedback_case_ids: list[str] = Field(default_factory=list)
    eval_case_ids: list[str] = Field(default_factory=list)
    attribution_job_ids: list[str] = Field(default_factory=list)


class NormalizedBlockedOptimizationItem(NormalizedOutputRecord):
    blocked_item_id: JsonValue = None
    source_index: int = 0
    status: str = "blocked"
    title: str = "未形成可执行优化任务"
    target_type: str = "not_actionable"
    target_path: JsonValue = None
    owner: JsonValue = None
    actionability: str = "needs_human_analysis"
    confidence: JsonValue = None
    problem_type: JsonValue = None
    recommendation: JsonValue = None
    reason: str
    analysis_summary: JsonValue = None
    evidence_summary: JsonValue = None
    task_context: NormalizedTaskContext = Field(default_factory=NormalizedTaskContext)
    evidence_refs: list[NormalizedEvidenceRef] = Field(default_factory=list)
    feedback_case_ids: list[str] = Field(default_factory=list)
    eval_case_ids: list[str] = Field(default_factory=list)
    attribution_job_ids: list[str] = Field(default_factory=list)


class NormalizedFeedbackOptimizationPlanOutput(NormalizedOutputRecord):
    batch_id: JsonValue = None
    optimization_plan_id: JsonValue = None
    created_at: JsonValue = None
    status: str
    title: JsonValue = None
    summary: JsonValue = None
    confidence: str
    actionability: str
    target_type: JsonValue = None
    target_path: JsonValue = None
    recommendation: JsonValue = None
    expected_effect: JsonValue = None
    validation: JsonValue = None
    risk: JsonValue = None
    source_refs: list[JsonObject] = Field(default_factory=list)
    tasks: list[NormalizedOptimizationPlanTask] = Field(default_factory=list)
    blocked_items: list[NormalizedBlockedOptimizationItem] = Field(default_factory=list)
    feedback_case_ids: list[str] = Field(default_factory=list)
    eval_case_ids: list[str] = Field(default_factory=list)
    attribution_job_ids: list[str] = Field(default_factory=list)
    attribution_summaries: list[NormalizedAttributionSummary] = Field(default_factory=list)
    problem_types: list[str] = Field(default_factory=list)
    rationale: JsonValue = None
    evidence_refs: list[NormalizedEvidenceRef] = Field(default_factory=list)
    regeneration_instruction: JsonValue = None


class NormalizedExecutionPlanOutput(NormalizedOutputRecord):
    optimization_task_id: JsonValue = None
    execution_job_id: JsonValue = None
    status: str
    summary: JsonValue = None
    operations: list[NormalizedExecutionOperation] = Field(default_factory=list)
    validation: JsonValue = None
    risk: JsonValue = None
    human_review_required: JsonValue = None
    no_action_reason: JsonValue = None


class NormalizedFeedbackEvalCaseGenerationOutput(NormalizedOutputRecord):
    job_id: JsonValue = None
    scope_kind: JsonValue = None
    scope_id: JsonValue = None
    eval_cases: list[NormalizedGeneratedEvalCase] = Field(default_factory=list)
    results: list[JsonObject] = Field(default_factory=list)
    status: str
    no_action_reason: JsonValue = None


class NormalizedRegressionImpactAnalysisOutput(NormalizedOutputRecord):
    impact_analysis_id: JsonValue = None
    eval_run_id: JsonValue = None
    gate_result: JsonObject = Field(default_factory=dict)
    impacted_assets: list[NormalizedSummaryItem] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    status: str
    result_status: JsonValue = None
    summary: JsonValue = None
    risk_assessment: JsonValue = None
    no_action_reason: JsonValue = None
