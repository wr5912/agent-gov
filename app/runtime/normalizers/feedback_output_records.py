from __future__ import annotations

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
