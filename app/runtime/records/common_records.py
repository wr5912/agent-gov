from __future__ import annotations

from typing import Literal, Optional

from pydantic import ConfigDict, Field, field_validator, model_validator

from ..json_types import JsonObject
from .base import StrictRuntimeRecord


class FeedbackSourceRefRecord(StrictRuntimeRecord):
    source_kind: Literal["signal", "soc_event", "pending_correlation"]
    source_id: str

    @model_validator(mode="before")
    @classmethod
    def normalize_source_ref(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        aliases = {
            "feedback_signal": "signal",
            "event": "soc_event",
            "pending": "pending_correlation",
        }
        source_kind = value.get("source_kind") or value.get("kind")
        source_id = value.get("source_id") or value.get("id")
        return {
            **value,
            "source_kind": aliases.get(str(source_kind or "").strip(), source_kind),
            "source_id": source_id,
        }

    @model_validator(mode="after")
    def validate_source_ref(self) -> "FeedbackSourceRefRecord":
        if not self.source_id.strip():
            raise ValueError("source_id cannot be empty")
        return self


class SkippedFeedbackSourceRefRecord(FeedbackSourceRefRecord):
    reason: str

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("skipped source reason cannot be empty")
        return text


class EvalCaseSourceRefRecord(StrictRuntimeRecord):
    source_kind: str
    source_id: str

    @model_validator(mode="before")
    @classmethod
    def normalize_eval_case_source_ref(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        return {
            **value,
            "source_kind": value.get("source_kind") or value.get("kind"),
            "source_id": value.get("source_id") or value.get("id"),
        }

    @model_validator(mode="after")
    def validate_source_ref(self) -> "EvalCaseSourceRefRecord":
        if not self.source_kind.strip():
            raise ValueError("source_kind cannot be empty")
        if not self.source_id.strip():
            raise ValueError("source_id cannot be empty")
        return self


class ExtensibleRuntimeRecord(StrictRuntimeRecord):
    """Base for normalized runtime records with schema-owned persisted fields."""

    model_config = ConfigDict(extra="ignore")


class FeedbackOptimizationTaskContextRecord(ExtensibleRuntimeRecord):
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
        return self.model_dump(mode="json", exclude_none=True, exclude_defaults=True)


class FeedbackOptimizationEvidenceRefRecord(ExtensibleRuntimeRecord):
    type: str = "evidence_file"
    id: str
    reason: str = ""

    @model_validator(mode="after")
    def validate_evidence_ref_shape(self) -> "FeedbackOptimizationEvidenceRefRecord":
        if not self.id.strip():
            raise ValueError("evidence ref id cannot be empty")
        if not self.type.strip():
            raise ValueError("evidence ref type cannot be empty")
        return self

    def to_payload(self) -> JsonObject:
        return self.model_dump(mode="json", exclude_none=True)


class FeedbackOptimizationAttributionSummaryRecord(StrictRuntimeRecord):
    attribution_job_id: Optional[str] = None
    feedback_case_id: Optional[str] = None
    problem_type: Optional[str] = None
    optimization_object_type: Optional[str] = None
    actionability: Optional[str] = None
    confidence: Optional[str] = None
    rationale: Optional[str] = None
    summary: Optional[str] = None


class FeedbackOptimizationPlanTaskSummaryRecord(StrictRuntimeRecord):
    total: int = Field(default=0, ge=0)
    workspace_execution: int = Field(default=0, ge=0)
    external_webhook: int = Field(default=0, ge=0)


class FeedbackOptimizationBlockedSummaryRecord(StrictRuntimeRecord):
    total: int = Field(default=0, ge=0)


class FeedbackBatchEvalCaseGenerationRecord(StrictRuntimeRecord):
    created: int = Field(default=0, ge=0)
    reused: int = Field(default=0, ge=0)
    updated: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)
    eval_cases: list[JsonObject] = Field(default_factory=list)
    results: list[JsonObject] = Field(default_factory=list)
    eval_case_ids: list[str] = Field(default_factory=list)
    result_ids: list[str] = Field(default_factory=list)

    @field_validator("eval_case_ids", "result_ids")
    @classmethod
    def validate_string_list(cls, value: list[str]) -> list[str]:
        return [str(item) for item in value if item]


class FeedbackBatchAttributionSummaryRecord(StrictRuntimeRecord):
    total: int = Field(default=0, ge=0)
    completed: int = Field(default=0, ge=0)
    running: int = Field(default=0, ge=0)
    needs_review_or_failed: int = Field(default=0, ge=0)
