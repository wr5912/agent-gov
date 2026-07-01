from __future__ import annotations

from typing import Optional

from pydantic import ConfigDict, Field
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


class NormalizedExecutionOperation(NormalizedOutputRecord):
    operation: JsonValue = None
    path: JsonValue = None
    expected_sha256: JsonValue = None
    content: JsonValue = None
    append_text: JsonValue = None
    rationale: JsonValue = None


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
    optimization_plan_summary: JsonValue = None


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


class NormalizedExecutionPlanOutput(NormalizedOutputRecord):
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
