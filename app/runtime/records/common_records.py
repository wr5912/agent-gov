from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field, field_validator, model_validator

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
    def validate_source_ref(self) -> FeedbackSourceRefRecord:
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
    def validate_source_ref(self) -> EvalCaseSourceRefRecord:
        if not self.source_kind.strip():
            raise ValueError("source_kind cannot be empty")
        if not self.source_id.strip():
            raise ValueError("source_id cannot be empty")
        return self


class EvalCaseOptimizationPlanChangeRecord(StrictRuntimeRecord):
    target: Optional[str] = None
    change: Optional[str] = None
    rationale: Optional[str] = None
    acceptance_criteria: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_change(cls, value: object) -> object:
        if not isinstance(value, dict):
            return {}
        return {
            "target": value.get("target"),
            "change": value.get("change"),
            "rationale": value.get("rationale"),
            "acceptance_criteria": value.get("acceptance_criteria") or [],
        }

    @field_validator("target", "change", "rationale")
    @classmethod
    def normalize_optional_text(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("acceptance_criteria")
    @classmethod
    def normalize_acceptance_criteria(cls, value: list[str]) -> list[str]:
        return [str(item).strip() for item in value if str(item).strip()]


class EvalCaseOptimizationPlanSummaryRecord(StrictRuntimeRecord):
    summary: Optional[str] = None
    changes: list[EvalCaseOptimizationPlanChangeRecord] = Field(default_factory=list)
    risk_level: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def normalize_summary(cls, value: object) -> object:
        if not isinstance(value, dict):
            return None
        return {
            "summary": value.get("summary"),
            "changes": value.get("changes") if isinstance(value.get("changes"), list) else [],
            "risk_level": value.get("risk_level"),
        }

    @field_validator("summary", "risk_level")
    @classmethod
    def normalize_optional_text(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None
