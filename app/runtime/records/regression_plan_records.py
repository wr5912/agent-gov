from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field, field_validator, model_validator

from app.runtime.runtime_db import RegressionGateOverrideModel, RegressionPlanModel

from .json_types import JsonObject, StrictRuntimeRecord


RegressionPlanStatus = Literal["created"]


class RegressionPlanRecord(StrictRuntimeRecord):
    """Internal source of truth for one regression plan row."""

    schema_version: str = "regression-plan/v1"
    regression_plan_id: str
    batch_id: str
    created_at: str
    status: RegressionPlanStatus
    applied_agent_version_id: Optional[str] = None
    selection_fingerprint: str
    base_selection_fingerprint: str
    eval_case_ids: list[str] = Field(default_factory=list)
    selected_cases: list[JsonObject] = Field(default_factory=list)
    selection_summary: JsonObject = Field(default_factory=dict)
    change_summary: JsonObject = Field(default_factory=dict)

    @field_validator("eval_case_ids")
    @classmethod
    def validate_eval_case_ids(cls, value: list[str]) -> list[str]:
        return [str(item) for item in value if item]

    @field_validator("selection_fingerprint", "base_selection_fingerprint")
    @classmethod
    def validate_fingerprint(cls, value: str) -> str:
        if len(value) != 64:
            raise ValueError("regression plan fingerprint must be 64 hex characters")
        int(value, 16)
        return value

    @model_validator(mode="after")
    def validate_shape(self) -> "RegressionPlanRecord":
        for key, value in (
            ("regression_plan_id", self.regression_plan_id),
            ("batch_id", self.batch_id),
            ("created_at", self.created_at),
        ):
            if not value.strip():
                raise ValueError(f"{key} cannot be empty")
        if not self.eval_case_ids:
            raise ValueError("regression plan must include eval_case_ids")
        return self

    def to_payload(self) -> JsonObject:
        return self.model_dump(mode="json")

    @classmethod
    def from_row(cls, row: RegressionPlanModel) -> "RegressionPlanRecord":
        payload = dict(row.payload_json or {})
        payload.update(
            {
                "regression_plan_id": row.regression_plan_id,
                "batch_id": row.batch_id,
                "created_at": row.created_at,
                "status": row.status,
                "applied_agent_version_id": row.applied_agent_version_id,
                "selection_fingerprint": row.selection_fingerprint,
            }
        )
        return cls.model_validate(payload)


class RegressionGateOverrideRecord(StrictRuntimeRecord):
    """Internal source of truth for one regression gate override row."""

    override_id: str
    batch_id: str
    eval_run_id: str
    operator: str
    reason: str
    expires_at: str
    created_at: str
    before: JsonObject = Field(default_factory=dict)
    after: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_shape(self) -> "RegressionGateOverrideRecord":
        for key, value in (
            ("override_id", self.override_id),
            ("batch_id", self.batch_id),
            ("eval_run_id", self.eval_run_id),
            ("operator", self.operator),
            ("reason", self.reason),
            ("expires_at", self.expires_at),
            ("created_at", self.created_at),
        ):
            if not value.strip():
                raise ValueError(f"{key} cannot be empty")
        return self

    def to_payload(self) -> JsonObject:
        return self.model_dump(mode="json")

    @classmethod
    def from_row(cls, row: RegressionGateOverrideModel) -> "RegressionGateOverrideRecord":
        return cls.model_validate(
            {
                "override_id": row.override_id,
                "batch_id": row.batch_id,
                "eval_run_id": row.eval_run_id,
                "operator": row.operator,
                "reason": row.reason,
                "expires_at": row.expires_at,
                "created_at": row.created_at,
                "before": row.before_json or {},
                "after": row.after_json or {},
            }
        )
