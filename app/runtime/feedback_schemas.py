from __future__ import annotations

from collections.abc import Callable
from typing import Literal, Optional, TypeVar, cast

from pydantic import BaseModel, Field, ValidationError, model_validator

from .json_types import JsonObject
from .normalizers.feedback_output_normalizers import (
    normalize_attribution_output,
    normalize_execution_plan_output,
)
from .normalizers.feedback_output_records import (
    NormalizedAttributionOutput,
    NormalizedEvidenceRef,
    NormalizedExecutionOperation,
    NormalizedExecutionPlanOutput,
    NormalizedOutputRecord,
    NormalizedResponsibilityBoundary,
)

TOutputModel = TypeVar("TOutputModel", bound=BaseModel)
FormatterAgentOutputNormalizer = Callable[[JsonObject], JsonObject]

def _normalize_formatter_agent_output(value: object, normalizer: FormatterAgentOutputNormalizer) -> object:
    if not isinstance(value, dict):
        return value
    return normalizer(cast(JsonObject, value))


ProblemType = Literal[
    "evidence_gap",
    "reasoning_error",
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
    "test_dataset",
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


class AttributionFormatterOutput(NormalizedOutputRecord):
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
    counter_evidence: list[str] = Field(default_factory=list)
    uncertainty_factors: list[str] = Field(default_factory=list)
    verification_suggestions: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalize_formatter_output(cls, value: object) -> object:
        return _normalize_formatter_agent_output(value, normalize_attribution_output)


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


class ImprovementOptimizationChangeFormatterOutput(NormalizedOutputRecord):
    target: str = Field(description="变更对象，如 CLAUDE.md、skill、subagent、mcp_config、runtime_config。")
    change: str = Field(description="具体优化建议。")


class ImprovementOptimizationPlanFormatterOutput(NormalizedOutputRecord):
    summary: str
    changes: list[ImprovementOptimizationChangeFormatterOutput] = Field(default_factory=list)
    risk_level: str = "medium"

    @model_validator(mode="after")
    def _has_summary_or_changes(self) -> ImprovementOptimizationPlanFormatterOutput:
        if not self.summary and not self.changes:
            raise ValueError("improvement optimization plan must include summary or changes")
        return self


class ImprovementOptimizationChangeOutput(ImprovementOptimizationChangeFormatterOutput):
    pass


class ImprovementOptimizationPlanOutput(NormalizedOutputRecord):
    summary: str
    changes: list[ImprovementOptimizationChangeOutput] = Field(default_factory=list)
    risk_level: str = "medium"

    @model_validator(mode="after")
    def _has_summary_or_changes(self) -> ImprovementOptimizationPlanOutput:
        if not self.summary and not self.changes:
            raise ValueError("improvement optimization plan must include summary or changes")
        return self


class NormalizedFeedbackFormatterOutput(NormalizedOutputRecord):
    """把用户原始反馈归纳成一句话 title + 清晰的 problem。"""

    title: str = ""
    problem: str

    @model_validator(mode="after")
    def _has_problem(self) -> NormalizedFeedbackFormatterOutput:
        if not self.problem:
            raise ValueError("normalized feedback must include a non-empty problem")
        return self


class NormalizedFeedbackOutput(NormalizedFeedbackFormatterOutput):
    pass


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


class ExecutionOperation(NormalizedExecutionOperation):
    operation: Literal["append_text", "replace_file", "create_file", "noop"]
    path: str
    expected_sha256: Optional[str] = None
    content: Optional[str] = None
    append_text: Optional[str] = None
    rationale: Optional[str] = None


class ExecutionPlanFormatterOutput(NormalizedOutputRecord):
    status: Literal["ready", "needs_human_review"] = "ready"
    summary: str
    operations: list[ExecutionOperation] = Field(default_factory=list)
    validation: Optional[str] = None
    risk: Optional[str] = None
    human_review_required: bool = True
    no_action_reason: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_formatter_output(cls, value: object) -> object:
        return _normalize_formatter_agent_output(value, normalize_execution_plan_output)

    @model_validator(mode="after")
    def _has_action_or_reason(self) -> ExecutionPlanFormatterOutput:
        if self.status == "ready" and not self.operations:
            raise ValueError("ready execution plan must include operations")
        if self.status == "needs_human_review" and not self.no_action_reason:
            raise ValueError("needs_human_review execution plan must include no_action_reason")
        return self


class ExecutionPlanOutput(NormalizedExecutionPlanOutput):
    status: Literal["ready", "needs_human_review"] = "ready"
    summary: str
    operations: list[ExecutionOperation] = Field(default_factory=list)
    validation: Optional[str] = None
    risk: Optional[str] = None
    human_review_required: bool = True
    no_action_reason: Optional[str] = None

    @model_validator(mode="after")
    def _has_action_or_reason(self) -> ExecutionPlanOutput:
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


class RegressionAssessmentCaseFormatterOutput(NormalizedOutputRecord):
    expected_behavior: str
    checks_json: JsonObject = Field(default_factory=dict)
    labels: list[str] = Field(default_factory=list)


class RegressionAssessmentFormatterOutput(NormalizedOutputRecord):
    eval_cases: list[RegressionAssessmentCaseFormatterOutput] = Field(default_factory=list)
    no_action_reason: Optional[str] = None
    suggested_gate_thresholds: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _has_eval_cases_or_reason(self) -> RegressionAssessmentFormatterOutput:
        if not self.eval_cases and not self.no_action_reason:
            raise ValueError("regression assessment must include eval_cases or no_action_reason")
        return self


class RegressionAssessmentCaseOutput(NormalizedOutputRecord):
    expected_behavior: str
    checks_json: JsonObject = Field(default_factory=dict)
    labels: list[str] = Field(default_factory=list)


class RegressionAssessmentOutput(NormalizedOutputRecord):
    eval_cases: list[RegressionAssessmentCaseOutput] = Field(default_factory=list)
    no_action_reason: Optional[str] = None
    suggested_gate_thresholds: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _has_eval_cases_or_reason(self) -> RegressionAssessmentOutput:
        if not self.eval_cases and not self.no_action_reason:
            raise ValueError("regression assessment must include eval_cases or no_action_reason")
        return self
def coerce_attribution_output_model(value: BaseModel | JsonObject) -> tuple[AttributionOutput | None, str | None]:
    return _coerce_output_model(value, model=AttributionOutput, normalizer=normalize_attribution_output)

def coerce_execution_plan_output_model(value: BaseModel | JsonObject) -> tuple[ExecutionPlanOutput | None, str | None]:
    return _coerce_output_model(value, model=ExecutionPlanOutput, normalizer=normalize_execution_plan_output)
