from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.runtime.runtime_db import ExecutionCompensationModel

from .json_types import JsonObject


EXECUTION_COMPENSATION_SCHEMA_VERSION = "execution-compensation/v1"
EXECUTION_COMPENSATION_TYPE = "execution_apply_post_write_failure"

ExecutionCompensationStatus = Literal["resolved", "pending_manual_recovery"]
ExecutionRestoreStatus = Literal["restored", "restore_failed"]


def status_for_restore_status(
    restore_status: ExecutionRestoreStatus,
) -> ExecutionCompensationStatus:
    return "resolved" if restore_status == "restored" else "pending_manual_recovery"


class ExecutionCompensationRecord(BaseModel):
    """Internal source of truth for execution-application compensation payloads."""

    model_config = ConfigDict(extra="allow")

    schema_version: Literal["execution-compensation/v1"] = EXECUTION_COMPENSATION_SCHEMA_VERSION
    compensation_id: str
    created_at: str
    updated_at: str
    status: ExecutionCompensationStatus
    compensation_type: Literal["execution_apply_post_write_failure"] = EXECUTION_COMPENSATION_TYPE
    optimization_task_id: str
    execution_job_id: str
    pre_execution_agent_version_id: Optional[str] = None
    restore_status: ExecutionRestoreStatus
    original_error: str
    restore_error: Optional[str] = None
    manual_restore_result: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_status_matches_restore_status(self) -> "ExecutionCompensationRecord":
        expected = status_for_restore_status(self.restore_status)
        if self.status != expected:
            raise ValueError(
                f"status must be {expected!r} when restore_status is {self.restore_status!r}"
            )
        return self

    @classmethod
    def post_write_failure(
        cls,
        *,
        compensation_id: str,
        now: str,
        optimization_task_id: str,
        execution_job_id: str,
        pre_execution_agent_version_id: str | None,
        restore_status: ExecutionRestoreStatus,
        original_error: str,
        restore_error: str | None = None,
    ) -> "ExecutionCompensationRecord":
        return cls(
            compensation_id=compensation_id,
            created_at=now,
            updated_at=now,
            status=status_for_restore_status(restore_status),
            optimization_task_id=optimization_task_id,
            execution_job_id=execution_job_id,
            pre_execution_agent_version_id=pre_execution_agent_version_id,
            restore_status=restore_status,
            original_error=original_error,
            restore_error=restore_error,
        )

    def mark_resolved(
        self,
        *,
        updated_at: str,
        restore_result: JsonObject | None = None,
    ) -> "ExecutionCompensationRecord":
        payload = self.model_dump(mode="json")
        payload.update(
            {
                "updated_at": updated_at,
                "status": "resolved",
                "restore_status": "restored",
                "restore_error": None,
                "manual_restore_result": restore_result or {},
            }
        )
        return type(self).model_validate(payload)

    def mark_restore_failed(
        self,
        *,
        updated_at: str,
        restore_error: str,
    ) -> "ExecutionCompensationRecord":
        payload = self.model_dump(mode="json")
        payload.update(
            {
                "updated_at": updated_at,
                "status": "pending_manual_recovery",
                "restore_status": "restore_failed",
                "restore_error": restore_error,
            }
        )
        return type(self).model_validate(payload)

    def to_payload(self) -> JsonObject:
        return self.model_dump(mode="json")

    @classmethod
    def from_row(cls, row: ExecutionCompensationModel) -> "ExecutionCompensationRecord":
        payload = dict(row.payload_json or {})
        payload.update(
            {
                "compensation_id": row.compensation_id,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
                "status": row.status,
                "compensation_type": row.compensation_type,
                "optimization_task_id": row.optimization_task_id,
                "execution_job_id": row.execution_job_id,
                "pre_execution_agent_version_id": row.pre_execution_agent_version_id,
                "restore_status": row.restore_status,
            }
        )
        return cls.model_validate(payload)


def apply_execution_compensation_record(
    row: ExecutionCompensationModel,
    record: ExecutionCompensationRecord,
) -> None:
    row.updated_at = record.updated_at
    row.status = record.status
    row.restore_status = record.restore_status
    row.payload_json = record.to_payload()
