from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field, model_validator

from ..json_types import JsonObject
from .agent_governance_records import AgentChangeSetProjectionRecord
from .agent_job_records import AgentJobProjectionRecord
from .base import StrictRuntimeRecord

FeedbackBatchExecutionRunStatus = Literal[
    "running",
    "completed",
    "partial_failed",
    "failed",
    "rolled_back",
    "rollback_failed",
]

FeedbackBatchExecutionTaskStatus = Literal["completed", "failed", "skipped"]


class FeedbackBatchExecutionErrorRecord(StrictRuntimeRecord):
    error_code: str
    message: str
    created_at: str


class FeedbackBatchExecutionTaskResultRecord(StrictRuntimeRecord):
    plan_task_id: str
    execution_kind: Literal["workspace_execution", "external_webhook"]
    status: FeedbackBatchExecutionTaskStatus
    started_at: str
    completed_at: Optional[str] = None
    optimization_task_id: Optional[str] = None
    execution_job_id: Optional[str] = None
    execution_job: Optional[AgentJobProjectionRecord] = None
    external_item_id: Optional[str] = None
    webhook_alias: Optional[str] = None
    summary: Optional[str] = None
    planned_diff: Optional[JsonObject] = None
    applied_agent_version_id: Optional[str] = None
    rollback_supported: bool = True
    rollback_note: Optional[str] = None
    error_json: Optional[FeedbackBatchExecutionErrorRecord] = None

    @model_validator(mode="after")
    def validate_task_result(self) -> FeedbackBatchExecutionTaskResultRecord:
        if self.status == "failed" and self.error_json is None:
            raise ValueError("failed task result requires error_json")
        if self.status == "completed" and not self.completed_at:
            raise ValueError("completed task result requires completed_at")
        if self.execution_kind == "workspace_execution" and self.status == "completed" and not self.execution_job_id:
            raise ValueError("completed workspace task result requires execution_job_id")
        if self.execution_kind == "external_webhook" and self.status == "completed" and not self.webhook_alias:
            raise ValueError("completed external task result requires webhook_alias")
        return self


class FeedbackBatchExecutionRollbackRecord(StrictRuntimeRecord):
    restored_at: str
    status: Literal["restored", "failed"]
    target_agent_version_id: Optional[str] = None
    restore_result: dict[str, object] = Field(default_factory=dict)
    error_json: Optional[FeedbackBatchExecutionErrorRecord] = None

    @model_validator(mode="after")
    def validate_rollback(self) -> FeedbackBatchExecutionRollbackRecord:
        if self.status == "failed" and self.error_json is None:
            raise ValueError("failed rollback requires error_json")
        return self


class FeedbackBatchExecutionRunRecord(StrictRuntimeRecord):
    schema_version: Literal["feedback-batch-execution-run/v1"] = "feedback-batch-execution-run/v1"
    execution_run_id: str
    batch_id: str
    created_at: str
    started_at: str
    completed_at: Optional[str] = None
    status: FeedbackBatchExecutionRunStatus
    force: bool = True
    note: Optional[str] = None
    pre_execution_agent_version_id: Optional[str] = None
    pre_execution_agent_version: Optional[JsonObject] = None
    applied_agent_version_id: Optional[str] = None
    applied_agent_version: Optional[JsonObject] = None
    applied_diff: Optional[JsonObject] = None
    change_set_id: Optional[str] = None
    change_set: Optional[AgentChangeSetProjectionRecord] = None
    candidate_commit_sha: Optional[str] = None
    task_results: list[FeedbackBatchExecutionTaskResultRecord] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    rollback_result: Optional[FeedbackBatchExecutionRollbackRecord] = None
    error_json: Optional[FeedbackBatchExecutionErrorRecord] = None

    @model_validator(mode="after")
    def validate_run(self) -> FeedbackBatchExecutionRunRecord:
        if self.status in {"completed", "partial_failed", "failed", "rolled_back", "rollback_failed"} and not self.completed_at:
            raise ValueError("terminal batch execution run requires completed_at")
        if self.status == "completed" and not self.task_results:
            raise ValueError("completed batch execution run requires task_results")
        if self.status == "failed" and self.error_json is None:
            raise ValueError("failed batch execution run requires error_json")
        if self.status == "rolled_back" and self.rollback_result is None:
            raise ValueError("rolled_back batch execution run requires rollback_result")
        return self

    def to_payload(self) -> JsonObject:
        return self.model_dump(mode="json", exclude_none=True)
