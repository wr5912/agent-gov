from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field, field_validator, model_validator

from app.runtime.runtime_db import EvalRunItemModel, EvalRunModel
from app.runtime.state_machines import EVAL_RUN_STATES, validate_transition

from .json_types import JsonObject, StrictRuntimeRecord


EvalRunStatus = Literal["running", "completed", "failed"]
EvalRunResultStatus = Literal[
    "running",
    "passed",
    "failed",
    "needs_human_review",
    "blocked",
    "review_required",
    "passed_with_notes",
]

EvalRunItemStatus = Literal["passed", "failed", "needs_human_review"]


class EvalRunRecord(StrictRuntimeRecord):
    """Internal source of truth for persisted eval run payload_json."""

    eval_run_id: str
    created_at: str
    completed_at: Optional[str] = None
    status: EvalRunStatus
    result_status: EvalRunResultStatus = "running"
    agent_version_id: Optional[str] = None
    optimization_task_id: Optional[str] = None
    source: str
    regression_plan_id: Optional[str] = None
    eval_case_ids: list[str] = Field(default_factory=list)
    item_ids: list[str] = Field(default_factory=list)
    summary: JsonObject = Field(default_factory=dict)
    gate_result: JsonObject = Field(default_factory=dict)
    error_json: Optional[JsonObject] = None
    gate_overridden_at: Optional[str] = None
    gate_override_id: Optional[str] = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in EVAL_RUN_STATES:
            raise ValueError(f"unsupported eval run status: {value}")
        return value

    @field_validator("eval_case_ids", "item_ids")
    @classmethod
    def validate_string_list(cls, value: list[str]) -> list[str]:
        return [str(item) for item in value if item]

    @model_validator(mode="after")
    def validate_lifecycle_shape(self) -> "EvalRunRecord":
        if self.status == "running":
            if self.completed_at:
                raise ValueError("completed_at must not be set while eval run is running")
            if self.result_status != "running":
                raise ValueError("running eval runs must have running result_status")
            return self
        if not self.completed_at:
            raise ValueError("completed_at is required for terminal eval run states")
        if self.status == "completed" and self.result_status == "running":
            raise ValueError("completed eval runs must have a final result_status")
        if self.status == "failed":
            if self.result_status != "failed":
                raise ValueError("failed eval runs must have failed result_status")
            if self.error_json is None:
                raise ValueError("error_json is required for failed eval runs")
        return self

    def transition_to(
        self,
        status: str,
        *,
        fields: JsonObject | None = None,
    ) -> "EvalRunRecord":
        validate_transition("eval_run", self.status, status)
        payload = self.to_payload()
        payload.update(fields or {})
        payload["status"] = status
        return type(self).model_validate(payload)

    def to_payload(self) -> JsonObject:
        return self.model_dump(mode="json")

    def to_response(self, *, items: list[JsonObject]) -> JsonObject:
        payload = self.to_payload()
        payload["items"] = items
        return payload

    @classmethod
    def from_payload(cls, payload: JsonObject) -> "EvalRunRecord":
        normalized = dict(payload)
        normalized.pop("items", None)
        return cls.model_validate(normalized)

    @classmethod
    def from_row(cls, row: EvalRunModel) -> "EvalRunRecord":
        payload = dict(row.payload_json or {})
        payload.pop("items", None)
        payload.update(
            {
                "eval_run_id": row.eval_run_id,
                "created_at": row.created_at,
                "completed_at": row.completed_at,
                "status": row.status,
                "agent_version_id": row.agent_version_id,
                "optimization_task_id": row.optimization_task_id,
                "source": row.source,
                "regression_plan_id": row.regression_plan_id,
            }
        )
        return cls.model_validate(payload)


class EvalRunItemRecord(StrictRuntimeRecord):
    """Internal source of truth for one persisted eval run item."""

    eval_run_item_id: str
    eval_run_id: str
    eval_case_id: str
    source_feedback_case_id: Optional[str] = None
    agent_run_id: Optional[str] = None
    agent_version_id: Optional[str] = None
    status: EvalRunItemStatus
    score: Optional[float] = None
    check_results: list[JsonObject] = Field(default_factory=list)
    eval_case_snapshot: JsonObject = Field(default_factory=dict)
    answer_summary: Optional[str] = None
    error_json: Optional[JsonObject] = None
    created_at: str

    @model_validator(mode="after")
    def validate_result_shape(self) -> "EvalRunItemRecord":
        if self.score is not None and not 0 <= self.score <= 1:
            raise ValueError("eval run item score must be between 0 and 1")
        return self

    def to_payload(self) -> JsonObject:
        return self.model_dump(mode="json")

    @classmethod
    def from_row(cls, row: EvalRunItemModel) -> "EvalRunItemRecord":
        payload = dict(row.payload_json or {})
        payload.update(
            {
                "eval_run_item_id": row.eval_run_item_id,
                "eval_run_id": row.eval_run_id,
                "eval_case_id": row.eval_case_id,
                "agent_run_id": row.agent_run_id,
                "status": row.status,
                "score": row.score,
            }
        )
        return cls.model_validate(payload)
