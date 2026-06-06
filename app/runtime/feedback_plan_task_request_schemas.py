from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, ValidationInfo, field_validator

from app.runtime.feedback_schemas import Actionability
from app.runtime.records.common_records import FeedbackOptimizationEvidenceRefRecord, FeedbackOptimizationTaskContextRecord


class FeedbackOptimizationPlanTaskUpdateRequest(BaseModel):
    title: Optional[str] = Field(default=None, max_length=300)
    description: Optional[str] = Field(default=None, max_length=4000)
    objective: Optional[str] = Field(default=None, max_length=2000)
    target_summary: Optional[str] = Field(default=None, max_length=1000)
    target_type: Optional[str] = Field(default=None, max_length=128)
    target_path: Optional[str] = Field(default=None, max_length=2048)
    actionability: Optional[Actionability] = None
    owner: Optional[str] = Field(default=None, max_length=256)
    recommendation: Optional[str] = Field(default=None, max_length=4000)
    recommended_actions: Optional[list[str]] = None
    acceptance_criteria: Optional[list[str]] = None
    expected_effect: Optional[str] = Field(default=None, max_length=2000)
    validation: Optional[str] = Field(default=None, max_length=2000)
    risk: Optional[str] = Field(default=None, max_length=2000)
    task_context: Optional[FeedbackOptimizationTaskContextRecord] = None
    evidence_summary: Optional[str] = Field(default=None, max_length=2000)
    evidence_refs: Optional[list[FeedbackOptimizationEvidenceRefRecord]] = None
    eval_case_ids: Optional[list[str]] = None
    edit_note: Optional[str] = Field(default=None, max_length=1000)

    @field_validator(
        "title",
        "description",
        "objective",
        "target_summary",
        "target_type",
        "target_path",
        "owner",
        "recommendation",
        "expected_effect",
        "validation",
        "risk",
        "evidence_summary",
        "edit_note",
        mode="before",
    )
    @classmethod
    def _trim_task_text(cls, value: object, info: ValidationInfo) -> object:
        if value is None or not isinstance(value, str):
            return value
        text = value.strip()
        if info.field_name in {"title", "target_type"} and not text:
            raise ValueError(f"{info.field_name} cannot be empty")
        return text

    @field_validator("recommended_actions", "acceptance_criteria", "eval_case_ids", mode="before")
    @classmethod
    def _clean_string_list(cls, value: object) -> object:
        if value is None:
            return value
        if not isinstance(value, list):
            return value
        return [text for item in value if isinstance(item, str) and (text := item.strip())]
