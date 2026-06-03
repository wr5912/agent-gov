from __future__ import annotations

from typing import Literal, Optional

from pydantic import ConfigDict, Field, field_validator, model_validator

from app.runtime.runtime_db import RegressionImpactAnalysisModel
from app.runtime.state_machines import REGRESSION_IMPACT_ANALYSIS_STATES, validate_transition

from ..json_types import JsonObject
from .base import StrictRuntimeRecord


RegressionImpactAnalysisStatus = Literal["pending", "completed", "needs_human_review", "failed"]
RegressionImpactAnalysisSchemaVersion = Literal[
    "regression-impact-analysis/v1",
    "regression-impact-analysis-output/v1",
]


class RegressionImpactedAssetRecord(StrictRuntimeRecord):
    """One asset or eval case impacted by a regression run result."""

    model_config = ConfigDict(extra="allow")

    summary: Optional[str] = None
    eval_case_id: Optional[str] = None
    asset_id: Optional[str] = None
    status: Optional[str] = None
    asset_layer: Optional[str] = None
    blocking_policy: Optional[str] = None
    labels: list[str] = Field(default_factory=list)
    answer_summary: Optional[str] = None

    @field_validator("labels")
    @classmethod
    def validate_labels(cls, value: list[str]) -> list[str]:
        return [str(item).strip() for item in value if str(item).strip()]

    @model_validator(mode="after")
    def validate_asset_shape(self) -> "RegressionImpactedAssetRecord":
        if not any((self.summary, self.eval_case_id, self.asset_id)):
            raise ValueError("impacted asset must include summary, eval_case_id, or asset_id")
        return self

    def to_payload(self) -> JsonObject:
        return self.model_dump(mode="json", exclude_none=True)


class RegressionImpactAnalysisRecord(StrictRuntimeRecord):
    """Internal source of truth for regression impact analysis payload_json."""

    schema_version: RegressionImpactAnalysisSchemaVersion = "regression-impact-analysis/v1"
    impact_analysis_id: str
    eval_run_id: str
    created_at: str
    completed_at: Optional[str] = None
    status: RegressionImpactAnalysisStatus
    job_id: Optional[str] = None
    result_status: Optional[str] = None
    gate_result: JsonObject = Field(default_factory=dict)
    impacted_assets: list[RegressionImpactedAssetRecord] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    summary: Optional[str] = None
    risk_assessment: Optional[str] = None
    next_steps: list[str] = Field(default_factory=list)
    no_action_reason: Optional[str] = None
    error_json: Optional[JsonObject] = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in REGRESSION_IMPACT_ANALYSIS_STATES:
            raise ValueError(f"unsupported regression impact analysis status: {value}")
        return value

    @field_validator("recommendations", "next_steps")
    @classmethod
    def validate_string_list(cls, value: list[str]) -> list[str]:
        return [str(item) for item in value if item]

    @model_validator(mode="after")
    def validate_lifecycle_shape(self) -> "RegressionImpactAnalysisRecord":
        if self.status == "pending":
            if self.completed_at:
                raise ValueError("completed_at must not be set while regression impact analysis is pending")
            return self
        if not self.completed_at:
            raise ValueError("completed_at is required for finished regression impact analysis states")
        if self.status == "failed" and self.error_json is None:
            raise ValueError("error_json is required for failed regression impact analysis")
        return self

    def transition_to(
        self,
        status: str,
        *,
        fields: JsonObject | None = None,
    ) -> "RegressionImpactAnalysisRecord":
        validate_transition("regression_impact_analysis", self.status, status)
        payload = self.to_payload()
        payload.update(fields or {})
        payload["status"] = status
        return type(self).model_validate(payload)

    def to_payload(self) -> JsonObject:
        return self.model_dump(mode="json")

    @classmethod
    def from_row(cls, row: RegressionImpactAnalysisModel) -> "RegressionImpactAnalysisRecord":
        payload = dict(row.payload_json or {})
        payload.update(
            {
                "impact_analysis_id": row.impact_analysis_id,
                "eval_run_id": row.eval_run_id,
                "created_at": row.created_at,
                "completed_at": row.completed_at,
                "status": row.status,
                "job_id": row.job_id,
            }
        )
        return cls.model_validate(payload)


def apply_regression_impact_analysis_record(
    row: RegressionImpactAnalysisModel,
    record: RegressionImpactAnalysisRecord,
) -> None:
    row.completed_at = record.completed_at
    row.status = record.status
    row.job_id = record.job_id
    row.payload_json = record.to_payload()
