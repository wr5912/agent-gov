from __future__ import annotations

from typing import Literal, Optional

from pydantic import ConfigDict, Field, field_validator, model_validator

from app.runtime.agent_job_types import AgentJobType
from app.runtime.runtime_db import AgentJobModel
from app.runtime.state_machines import AGENT_JOB_STATES, validate_transition

from ..json_types import JsonObject
from .base import StrictRuntimeRecord


AgentJobStatus = Literal[
    "created",
    "queued",
    "running",
    "schema_validating",
    "evidence_packaging",
    "completed",
    "failed",
    "needs_human_review",
    "timeout",
]


class AgentJobRecord(StrictRuntimeRecord):
    """Internal source of truth for one persisted generic agent job."""

    job_id: str
    job_type: AgentJobType
    scope_kind: str
    scope_id: str
    status: AgentJobStatus
    profile_name: str
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    input_path: str
    raw_output_path: str
    validated_output_path: str
    error_path: str
    runtime_version: str
    schema_version: str
    timeout_seconds: int = 300
    retry_count: int = 0
    profile_version: Optional[JsonObject] = None
    input_json: Optional[JsonObject] = None
    raw_output_json: Optional[JsonObject] = None
    validated_output_json: Optional[JsonObject] = None
    error_json: Optional[JsonObject] = None

    feedback_case_id: Optional[str] = None
    evidence_package_id: Optional[str] = None
    attribution_job_id: Optional[str] = None
    batch_id: Optional[str] = None
    optimization_task_id: Optional[str] = None
    execution_job_id: Optional[str] = None
    baseline_agent_version_id: Optional[str] = None
    eval_run_id: Optional[str] = None
    regression_plan_id: Optional[str] = None
    compensations: list[JsonObject] = Field(default_factory=list)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in AGENT_JOB_STATES:
            raise ValueError(f"unsupported agent job status: {value}")
        return value

    @model_validator(mode="after")
    def validate_timestamps(self) -> "AgentJobRecord":
        if self.status in {"completed", "failed", "timeout"} and not self.completed_at:
            raise ValueError("completed_at is required for terminal agent job states")
        if self.status in {"running", "evidence_packaging"} and not self.started_at:
            raise ValueError("started_at is required for in-progress agent job states")
        return self

    def transition_to(
        self,
        status: str,
        *,
        started_at: Optional[str] = None,
        completed_at: Optional[str] = None,
    ) -> "AgentJobRecord":
        validate_transition("agent_job", self.status, status)
        payload = self.to_payload()
        payload["status"] = status
        if started_at is not None:
            payload["started_at"] = started_at
        if completed_at is not None:
            payload["completed_at"] = completed_at
        return type(self).model_validate(payload)

    def to_payload(self) -> JsonObject:
        return self.model_dump(mode="json")

    @classmethod
    def from_row(
        cls,
        row: AgentJobModel,
        *,
        compensations: list[JsonObject] | None = None,
    ) -> "AgentJobRecord":
        input_json = row.input_json if isinstance(row.input_json, dict) else {}
        payload: dict[str, object] = {
            "job_id": row.job_id,
            "job_type": row.job_type,
            "scope_kind": row.scope_kind,
            "scope_id": row.scope_id,
            "status": row.status,
            "profile_name": row.profile_name,
            "created_at": row.created_at,
            "started_at": row.started_at,
            "completed_at": row.completed_at,
            "input_path": row.input_path,
            "raw_output_path": row.raw_output_path,
            "validated_output_path": row.validated_output_path,
            "error_path": row.error_path,
            "runtime_version": row.runtime_version,
            "schema_version": row.schema_version,
            "timeout_seconds": row.timeout_seconds,
            "retry_count": row.retry_count,
            "profile_version": row.profile_version_json,
            "input_json": row.input_json,
            "raw_output_json": row.raw_output_json,
            "validated_output_json": row.validated_output_json,
            "error_json": row.error_json,
        }
        for key in (
            "feedback_case_id",
            "evidence_package_id",
            "attribution_job_id",
            "batch_id",
            "optimization_task_id",
            "execution_job_id",
            "baseline_agent_version_id",
            "eval_run_id",
            "regression_plan_id",
        ):
            if input_json.get(key) is not None:
                payload[key] = input_json.get(key)
        if row.job_type == "execution":
            payload["execution_job_id"] = payload.get("execution_job_id") or row.job_id
            payload["compensations"] = compensations or []
        return cls.model_validate(payload)


class AgentJobValidationErrorRecord(StrictRuntimeRecord):
    """Validation error snapshot embedded in an agent job projection."""

    model_config = ConfigDict(extra="allow")

    type: Optional[str] = None
    loc: list[str | int] = Field(default_factory=list)
    msg: Optional[str] = None
    input: object | None = None
    ctx: object | None = None
    url: Optional[str] = None


class AgentJobErrorRecord(StrictRuntimeRecord):
    """Agent job error snapshot embedded in projected records."""

    model_config = ConfigDict(extra="allow")

    error_code: Optional[str] = None
    message: Optional[str] = None
    created_at: Optional[str] = None
    job_id: Optional[str] = None
    validation_errors: list[AgentJobValidationErrorRecord] = Field(default_factory=list)


class AgentJobProjectionRecord(StrictRuntimeRecord):
    """Embedded agent job snapshot used by batch/task projections.

    Persisted agent job rows are validated by AgentJobRecord. Embedded snapshots
    can be historical, partial, or decorated with display fields, so they keep a
    narrower contract and do not inherit row lifecycle invariants.
    """

    model_config = ConfigDict(extra="allow")

    job_id: str
    job_type: str
    scope_kind: Optional[str] = None
    scope_id: Optional[str] = None
    status: str
    profile_name: str
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    input_path: str = ""
    raw_output_path: str = ""
    validated_output_path: str = ""
    error_path: str = ""
    runtime_version: Optional[str] = None
    schema_version: Optional[str] = None
    timeout_seconds: int = 300
    retry_count: int = 0
    profile_version: Optional[JsonObject] = None
    input_json: Optional[JsonObject] = None
    raw_output_json: Optional[JsonObject] = None
    validated_output_json: Optional[JsonObject] = None
    error_json: Optional[AgentJobErrorRecord] = None

    feedback_case_id: Optional[str] = None
    evidence_package_id: Optional[str] = None
    attribution_job_id: Optional[str] = None
    batch_id: Optional[str] = None
    optimization_task_id: Optional[str] = None
    execution_job_id: Optional[str] = None
    baseline_agent_version_id: Optional[str] = None
    eval_run_id: Optional[str] = None
    regression_plan_id: Optional[str] = None
    compensations: list[JsonObject] = Field(default_factory=list)
