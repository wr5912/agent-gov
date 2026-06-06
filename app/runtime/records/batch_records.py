from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field, field_validator, model_validator

from app.runtime.runtime_db import FeedbackOptimizationBatchModel
from app.runtime.state_machines import BATCH_STATES, validate_transition

from ..json_types import JsonObject
from .agent_job_records import AgentJobProjectionRecord
from .base import StrictRuntimeRecord
from .batch_execution_records import FeedbackBatchExecutionRunRecord
from .batch_plan_records import FeedbackOptimizationPlanRecord
from .common_records import (
    FeedbackBatchAttributionSummaryRecord,
    FeedbackBatchEvalCaseGenerationRecord,
    FeedbackSourceRefRecord,
    SkippedFeedbackSourceRefRecord,
)
from .eval_run_records import EvalRunProjectionRecord
from .optimization_task_records import OptimizationTaskRecord
from .regression_plan_records import RegressionPlanRecord

FeedbackOptimizationBatchStatus = Literal[
    "draft",
    "attribution_running",
    "attribution_completed",
    "attribution_failed",
    "optimization_plan_queued",
    "pending_approval",
    "approved",
    "rejected",
    "execution_planning",
    "execution_ready",
    "needs_human_review",
    "failed",
    "applied_pending_regression",
    "regression_running",
    "regression_passed",
    "regression_failed",
    "completed",
    "blocked",
    "sent",
    "notification_failed",
    "pending_execution",
    "execution_failed",
]


class FeedbackOptimizationBatchRecord(StrictRuntimeRecord):
    """Internal source of truth for feedback optimization batch payload_json."""

    schema_version: Literal["feedback-optimization-batch/v1"] = "feedback-optimization-batch/v1"
    batch_id: str
    created_at: str
    updated_at: str
    status: FeedbackOptimizationBatchStatus
    title: str
    priority: Optional[str] = None
    source_refs: list[FeedbackSourceRefRecord] = Field(default_factory=list)
    feedback_case_ids: list[str] = Field(default_factory=list)
    skipped_source_refs: list[SkippedFeedbackSourceRefRecord] = Field(default_factory=list)
    eval_case_ids: list[str] = Field(default_factory=list)
    eval_case_generation: FeedbackBatchEvalCaseGenerationRecord = Field(default_factory=FeedbackBatchEvalCaseGenerationRecord)
    eval_case_generation_job_id: Optional[str] = None
    eval_case_generation_job: Optional[AgentJobProjectionRecord] = None
    attribution_job_ids: list[str] = Field(default_factory=list)
    attribution_jobs: list[AgentJobProjectionRecord] = Field(default_factory=list)
    attribution_summary: FeedbackBatchAttributionSummaryRecord = Field(default_factory=FeedbackBatchAttributionSummaryRecord)
    optimization_plan: Optional[FeedbackOptimizationPlanRecord] = None
    optimization_plan_job_id: Optional[str] = None
    optimization_plan_job: Optional[AgentJobProjectionRecord] = None
    optimization_plan_error: Optional[JsonObject] = None
    internal_proposal_id: Optional[str] = None
    optimization_task_id: Optional[str] = None
    optimization_task_ids: list[str] = Field(default_factory=list)
    optimization_task: Optional[OptimizationTaskRecord] = None
    execution_job_id: Optional[str] = None
    execution_job: Optional[AgentJobProjectionRecord] = None
    execution_apply_result: Optional[JsonObject] = None
    execution_runs: list[FeedbackBatchExecutionRunRecord] = Field(default_factory=list)
    latest_execution_run: Optional[FeedbackBatchExecutionRunRecord] = None
    eval_run_id: Optional[str] = None
    latest_eval_run: Optional[EvalRunProjectionRecord] = None
    regression_plan_id: Optional[str] = None
    latest_regression_plan: Optional[RegressionPlanRecord] = None
    latest_regression_gate: JsonObject = Field(default_factory=dict)
    applied_agent_version_id: Optional[str] = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in BATCH_STATES:
            raise ValueError(f"unsupported feedback optimization batch status: {value}")
        return value

    @field_validator("feedback_case_ids", "eval_case_ids", "attribution_job_ids", "optimization_task_ids")
    @classmethod
    def validate_string_list(cls, value: list[str]) -> list[str]:
        return [str(item) for item in value if item]

    @model_validator(mode="after")
    def validate_batch_shape(self) -> FeedbackOptimizationBatchRecord:
        if not self.source_refs:
            raise ValueError("feedback optimization batch must include source_refs")
        if not self.feedback_case_ids:
            raise ValueError("feedback optimization batch must include feedback_case_ids")
        if not self.title.strip():
            raise ValueError("feedback optimization batch title cannot be empty")
        return self

    def transition_to(
        self,
        status: str,
        *,
        fields: JsonObject | None = None,
    ) -> FeedbackOptimizationBatchRecord:
        validate_transition("batch", self.status, status)
        payload = self.to_payload()
        payload.update(fields or {})
        payload["status"] = status
        return type(self).model_validate(payload)

    def to_payload(self) -> JsonObject:
        return self.model_dump(mode="json")

    @classmethod
    def from_row(cls, row: FeedbackOptimizationBatchModel) -> FeedbackOptimizationBatchRecord:
        payload = dict(row.payload_json or {})
        payload.update(
            {
                "batch_id": row.batch_id,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
                "status": row.status,
                "title": row.title,
            }
        )
        return cls.model_validate(payload)
