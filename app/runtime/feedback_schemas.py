from __future__ import annotations

from typing import Literal, Optional, TypeVar

from pydantic import BaseModel, Field, ValidationError, model_validator

from .normalizers.feedback_output_normalizers import (
    normalize_attribution_output,
    normalize_execution_plan_output,
    normalize_feedback_optimization_plan_output,
    normalize_feedback_eval_case_generation_output,
    normalize_regression_impact_analysis_output,
    task_context_has_external_specificity as _task_context_has_external_specificity,
)
from .normalizers.feedback_output_records import (
    NormalizedAttributionOutput,
    NormalizedAttributionSummary,
    NormalizedBlockedOptimizationItem,
    NormalizedEvidenceRef,
    NormalizedExecutionOperation,
    NormalizedExecutionPlanOutput,
    NormalizedFeedbackEvalCaseGenerationOutput,
    NormalizedFeedbackOptimizationPlanOutput,
    NormalizedGeneratedEvalCase,
    NormalizedOptimizationPlanTask,
    NormalizedOutputRecord,
    NormalizedRegressionImpactAnalysisOutput,
    NormalizedResponsibilityBoundary,
    NormalizedSummaryItem,
    NormalizedTaskContext,
)
from .json_types import JsonObject


TOutputModel = TypeVar("TOutputModel", bound=BaseModel)


ProblemType = Literal[
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
]

OptimizationObjectType = Literal[
    "main_agent_claude_md",
    "skill",
    "subagent",
    "mcp_config",
    "mcp_description",
    "output_style",
    "eval_case",
    "runtime_code",
    "external_mcp_service",
    "soc_process",
    "not_actionable",
]

Actionability = Literal[
    "direct_workspace_change",
    "workspace_config_change",
    "eval_only",
    "external_guidance",
    "runtime_fix",
    "needs_human_analysis",
    "not_actionable",
]

Confidence = Literal["low", "medium", "high"]


class EvidenceRef(NormalizedEvidenceRef):
    type: str
    id: str
    reason: str


class ResponsibilityBoundary(NormalizedResponsibilityBoundary):
    owner: str
    reason: str


class AttributionOutput(NormalizedAttributionOutput):
    feedback_case_id: str
    attribution_job_id: str
    status: Literal["completed", "needs_human_review"] = "completed"
    problem_type: ProblemType
    optimization_object_type: OptimizationObjectType
    actionability: Actionability
    confidence: Confidence
    human_review_required: bool
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    responsibility_boundary: ResponsibilityBoundary
    rationale: str
    recommended_next_step: Literal["generate_proposal", "needs_human_review", "stop"] = "generate_proposal"


class TaskContext(NormalizedTaskContext):
    pass


class OptimizationPlanTaskOutput(NormalizedOptimizationPlanTask):
    plan_task_id: Optional[str] = None
    source_index: int = 0
    execution_kind: Literal["workspace_execution", "external_webhook"]
    status: Literal["pending_execution", "pending_notification", "needs_human_review"] | str = ""
    title: str
    description: str
    objective: str
    target_summary: Optional[str] = None
    target_type: str
    target_path: Optional[str] = None
    owner: Optional[str] = None
    actionability: Actionability
    confidence: Optional[Confidence] = None
    problem_type: Optional[ProblemType] = None
    recommendation: str
    recommended_actions: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    expected_effect: str
    validation: str
    risk: str
    analysis_summary: Optional[str] = None
    evidence_summary: Optional[str] = None
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    rationale: Optional[str] = None
    reason: Optional[str] = None
    feedback_case_ids: list[str] = Field(default_factory=list)
    eval_case_ids: list[str] = Field(default_factory=list)
    attribution_job_ids: list[str] = Field(default_factory=list)
    task_context: TaskContext = Field(default_factory=TaskContext)

    @model_validator(mode="after")
    def _is_executable_task(self) -> "OptimizationPlanTaskOutput":
        if self.execution_kind == "workspace_execution":
            if not self.target_path:
                raise ValueError("workspace_execution task must include target_path")
            if not self.status:
                self.status = "pending_execution"
        if self.execution_kind == "external_webhook":
            if not _task_context_has_external_specificity(self.task_context.model_dump(mode="json")):
                raise ValueError("external_webhook task must include actionable task_context")
            if not self.status:
                self.status = "pending_notification"
        return self


class BlockedOptimizationItemOutput(NormalizedBlockedOptimizationItem):
    blocked_item_id: Optional[str] = None
    source_index: int = 0
    status: Literal["blocked", "needs_human_review"] | str = "blocked"
    title: str
    target_type: str = "not_actionable"
    target_path: Optional[str] = None
    owner: Optional[str] = None
    actionability: Actionability = "needs_human_analysis"
    confidence: Optional[Confidence] = None
    problem_type: Optional[ProblemType] = None
    recommendation: Optional[str] = None
    reason: str
    analysis_summary: Optional[str] = None
    evidence_summary: Optional[str] = None
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    feedback_case_ids: list[str] = Field(default_factory=list)
    eval_case_ids: list[str] = Field(default_factory=list)
    attribution_job_ids: list[str] = Field(default_factory=list)
    task_context: TaskContext = Field(default_factory=TaskContext)


class AttributionSummary(NormalizedAttributionSummary):
    pass


class FeedbackOptimizationPlanOutput(NormalizedFeedbackOptimizationPlanOutput):
    batch_id: str
    optimization_plan_id: Optional[str] = None
    created_at: Optional[str] = None
    status: Literal["pending_approval", "needs_human_review"] = "pending_approval"
    title: str
    summary: Optional[str] = None
    problem_types: list[str] = Field(default_factory=list)
    confidence: Confidence = "medium"
    actionability: Actionability = "needs_human_analysis"
    target_type: Optional[str] = None
    target_path: Optional[str] = None
    recommendation: str
    expected_effect: str
    validation: str
    risk: str
    source_refs: list[JsonObject] = Field(default_factory=list)
    feedback_case_ids: list[str] = Field(default_factory=list)
    eval_case_ids: list[str] = Field(default_factory=list)
    attribution_job_ids: list[str] = Field(default_factory=list)
    attribution_summaries: list[AttributionSummary] = Field(default_factory=list)
    rationale: Optional[str] = None
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    tasks: list[OptimizationPlanTaskOutput] = Field(default_factory=list)
    blocked_items: list[BlockedOptimizationItemOutput] = Field(default_factory=list)
    regeneration_instruction: Optional[str] = None

    @model_validator(mode="after")
    def _has_tasks_or_blockers(self) -> "FeedbackOptimizationPlanOutput":
        if not self.tasks and not self.blocked_items:
            raise ValueError("feedback optimization plan must include tasks or blocked_items")
        if not self.tasks:
            self.status = "needs_human_review"
        elif self.status != "needs_human_review":
            self.status = "pending_approval"
        return self


def output_model_payload(output: BaseModel) -> JsonObject:
    return output.model_dump(mode="json", exclude_none=True)


def _validated_payload(model: type[NormalizedOutputRecord], normalized: JsonObject) -> JsonObject:
    return output_model_payload(model.model_validate(normalized))


def _coerce_output_model(
    value: BaseModel | JsonObject,
    *,
    model: type[TOutputModel],
    normalizer: object,
) -> tuple[TOutputModel | None, str | None]:
    if isinstance(value, model):
        return value, None
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    try:
        normalized = normalizer(payload)  # type: ignore[operator]
        return model.model_validate(normalized), None
    except ValidationError as exc:
        return None, exc.json()


def validate_attribution_output(payload: JsonObject) -> tuple[JsonObject | None, str | None]:
    normalized = normalize_attribution_output(payload)
    try:
        return _validated_payload(AttributionOutput, normalized), None
    except ValidationError as exc:
        return None, exc.json()



def validate_feedback_optimization_plan_output(payload: JsonObject) -> tuple[JsonObject | None, str | None]:
    normalized = normalize_feedback_optimization_plan_output(payload)
    try:
        return _validated_payload(FeedbackOptimizationPlanOutput, normalized), None
    except ValidationError as exc:
        return None, exc.json()




class ExecutionOperation(NormalizedExecutionOperation):
    operation: Literal["append_text", "replace_file", "create_file", "noop"]
    path: str
    expected_sha256: Optional[str] = None
    content: Optional[str] = None
    append_text: Optional[str] = None
    rationale: Optional[str] = None


class ExecutionPlanOutput(NormalizedExecutionPlanOutput):
    optimization_task_id: str
    execution_job_id: str
    status: Literal["ready", "needs_human_review"] = "ready"
    baseline_agent_version_id: Optional[str] = None
    summary: str
    operations: list[ExecutionOperation] = Field(default_factory=list)
    validation: Optional[str] = None
    risk: Optional[str] = None
    human_review_required: bool = True
    no_action_reason: Optional[str] = None

    @model_validator(mode="after")
    def _has_action_or_reason(self) -> "ExecutionPlanOutput":
        if self.status == "ready" and not self.operations:
            raise ValueError("ready execution plan must include operations")
        if self.status == "needs_human_review" and not self.no_action_reason:
            raise ValueError("needs_human_review execution plan must include no_action_reason")
        return self


def validate_execution_plan_output(payload: JsonObject) -> tuple[JsonObject | None, str | None]:
    normalized = normalize_execution_plan_output(payload)
    try:
        return _validated_payload(ExecutionPlanOutput, normalized), None
    except ValidationError as exc:
        return None, exc.json()


class GeneratedEvalCaseOutput(NormalizedGeneratedEvalCase):
    eval_case_id: Optional[str] = None
    status: Literal["active", "draft", "archived"] = "draft"
    source: Optional[str] = "eval_case_governor"
    source_feedback_case_id: Optional[str] = None
    source_run_id: Optional[str] = None
    source_kind: Optional[str] = None
    source_id: Optional[str] = None
    source_refs: list[JsonObject] = Field(default_factory=list)
    asset_layer: Optional[str] = "candidate"
    promotion_status: Optional[str] = "candidate"
    blocking_policy: Optional[str] = "non_blocking"
    scenario_pack: Optional[str] = None
    severity: Optional[str] = "medium"
    flaky_status: Optional[str] = "stable"
    variant_role: Optional[str] = "original_reproduction"
    prompt: str
    expected_behavior: Optional[str] = None
    checks_json: JsonObject = Field(default_factory=dict)
    labels: list[str] = Field(default_factory=list)
    source_summary: Optional[JsonObject] = None
    attribution_summary: Optional[JsonObject] = None
    proposal_summary: Optional[JsonObject] = None


class FeedbackEvalCaseGenerationOutput(NormalizedFeedbackEvalCaseGenerationOutput):
    job_id: Optional[str] = None
    scope_kind: Optional[str] = None
    scope_id: Optional[str] = None
    status: Literal["completed", "needs_human_review"] = "completed"
    eval_cases: list[GeneratedEvalCaseOutput] = Field(default_factory=list)
    results: list[JsonObject] = Field(default_factory=list)
    no_action_reason: Optional[str] = None

    @model_validator(mode="after")
    def _has_eval_cases_or_reason(self) -> "FeedbackEvalCaseGenerationOutput":
        if not self.eval_cases and not self.no_action_reason:
            raise ValueError("eval case generation output must include eval_cases or no_action_reason")
        if not self.eval_cases:
            self.status = "needs_human_review"
        return self


class ImpactedAssetSummary(NormalizedSummaryItem):
    pass


class RegressionImpactAnalysisOutput(NormalizedRegressionImpactAnalysisOutput):
    impact_analysis_id: Optional[str] = None
    eval_run_id: str
    status: Literal["completed", "needs_human_review"] = "completed"
    result_status: Optional[str] = None
    gate_result: JsonObject = Field(default_factory=dict)
    impacted_assets: list[ImpactedAssetSummary] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    summary: Optional[str] = None
    risk_assessment: Optional[str] = None
    next_steps: list[str] = Field(default_factory=list)
    no_action_reason: Optional[str] = None

    @model_validator(mode="after")
    def _has_recommendation_or_reason(self) -> "RegressionImpactAnalysisOutput":
        if not self.recommendations and not self.next_steps and not self.no_action_reason:
            raise ValueError("regression impact output must include recommendations, next_steps, or no_action_reason")
        return self


def validate_feedback_eval_case_generation_output(payload: JsonObject) -> tuple[JsonObject | None, str | None]:
    normalized = normalize_feedback_eval_case_generation_output(payload)
    try:
        return _validated_payload(FeedbackEvalCaseGenerationOutput, normalized), None
    except ValidationError as exc:
        return None, exc.json()


def validate_regression_impact_analysis_output(payload: JsonObject) -> tuple[JsonObject | None, str | None]:
    normalized = normalize_regression_impact_analysis_output(payload)
    try:
        return _validated_payload(RegressionImpactAnalysisOutput, normalized), None
    except ValidationError as exc:
        return None, exc.json()


def coerce_attribution_output_model(value: BaseModel | JsonObject) -> tuple[AttributionOutput | None, str | None]:
    return _coerce_output_model(value, model=AttributionOutput, normalizer=normalize_attribution_output)


def coerce_feedback_optimization_plan_output_model(
    value: BaseModel | JsonObject,
) -> tuple[FeedbackOptimizationPlanOutput | None, str | None]:
    return _coerce_output_model(value, model=FeedbackOptimizationPlanOutput, normalizer=normalize_feedback_optimization_plan_output)


def coerce_execution_plan_output_model(value: BaseModel | JsonObject) -> tuple[ExecutionPlanOutput | None, str | None]:
    return _coerce_output_model(value, model=ExecutionPlanOutput, normalizer=normalize_execution_plan_output)


def coerce_feedback_eval_case_generation_output_model(
    value: BaseModel | JsonObject,
) -> tuple[FeedbackEvalCaseGenerationOutput | None, str | None]:
    return _coerce_output_model(value, model=FeedbackEvalCaseGenerationOutput, normalizer=normalize_feedback_eval_case_generation_output)


def coerce_regression_impact_analysis_output_model(
    value: BaseModel | JsonObject,
) -> tuple[RegressionImpactAnalysisOutput | None, str | None]:
    return _coerce_output_model(value, model=RegressionImpactAnalysisOutput, normalizer=normalize_regression_impact_analysis_output)
