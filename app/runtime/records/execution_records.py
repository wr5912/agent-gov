from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field, field_validator, model_validator

from app.runtime.runtime_db import ExecutionApplicationModel
from app.runtime.state_machines import EXECUTION_APPLICATION_STATES, validate_transition

from .json_types import JsonObject, StrictRuntimeRecord


ExecutionApplicationStatus = Literal[
    "created",
    "applied",
    "failed",
    "pending_manual_recovery",
    "compensated",
]


class ExecutionApplicationRecord(StrictRuntimeRecord):
    """Internal source of truth for execution plan application records."""

    schema_version: Literal["execution-application/v1"] = "execution-application/v1"
    application_id: str
    execution_job_id: str
    optimization_task_id: str
    created_at: str
    completed_at: Optional[str] = None
    status: ExecutionApplicationStatus
    pre_execution_agent_version_id: Optional[str] = None
    pre_execution_agent_version: Optional[JsonObject] = None
    applied_agent_version_id: Optional[str] = None
    applied_agent_version: Optional[JsonObject] = None
    applied_diff: JsonObject = Field(default_factory=dict)
    error_json: Optional[JsonObject] = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in EXECUTION_APPLICATION_STATES:
            raise ValueError(f"unsupported execution application status: {value}")
        return value

    @model_validator(mode="after")
    def validate_completion(self) -> "ExecutionApplicationRecord":
        if self.status != "created" and not self.completed_at:
            raise ValueError("completed_at is required after execution application leaves created")
        if self.status == "applied" and not self.applied_agent_version_id:
            raise ValueError("applied_agent_version_id is required for applied execution application")
        if self.status in {"failed", "pending_manual_recovery"} and not self.error_json:
            raise ValueError("error_json is required for failed execution application")
        return self

    def transition_to(
        self,
        status: str,
        *,
        fields: JsonObject | None = None,
    ) -> "ExecutionApplicationRecord":
        validate_transition("execution_application", self.status, status)
        payload = self.to_payload()
        payload.update(fields or {})
        payload["status"] = status
        return type(self).model_validate(payload)

    def to_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json")

    @classmethod
    def from_payload(cls, payload: JsonObject) -> "ExecutionApplicationRecord":
        return cls.model_validate(payload)

    @classmethod
    def from_row(cls, row: ExecutionApplicationModel) -> "ExecutionApplicationRecord":
        payload = dict(row.payload_json or {})
        payload.update(
            {
                "application_id": row.application_id,
                "execution_job_id": row.execution_job_id,
                "optimization_task_id": row.optimization_task_id,
                "created_at": row.created_at,
                "completed_at": row.completed_at,
                "status": row.status,
            }
        )
        return cls.model_validate(payload)
