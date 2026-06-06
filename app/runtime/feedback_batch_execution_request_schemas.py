from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator


class FeedbackOptimizationBatchExecuteAllRequest(BaseModel):
    force: bool = True
    webhook_alias_by_task_id: dict[str, str] = Field(default_factory=dict)
    note: Optional[str] = Field(default=None, max_length=2000)

    @field_validator("note", mode="before")
    @classmethod
    def trim_note(cls, value: object) -> object:
        if value is None or not isinstance(value, str):
            return value
        text = value.strip()
        return text or None

    @field_validator("webhook_alias_by_task_id", mode="before")
    @classmethod
    def trim_aliases(cls, value: object) -> object:
        if not isinstance(value, dict):
            return {}
        return {str(key): str(item).strip() for key, item in value.items() if str(key).strip() and str(item).strip()}


class FeedbackOptimizationBatchExecutionRollbackRequest(BaseModel):
    note: Optional[str] = Field(default=None, max_length=2000)

    @field_validator("note", mode="before")
    @classmethod
    def trim_note(cls, value: object) -> object:
        if value is None or not isinstance(value, str):
            return value
        text = value.strip()
        return text or None
