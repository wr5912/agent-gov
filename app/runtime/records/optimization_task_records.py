from __future__ import annotations

from typing import Literal, Optional

from pydantic import ConfigDict, Field, field_validator, model_validator

from app.runtime.runtime_db import OptimizationTaskModel
from app.runtime.state_machines import TASK_STATES, validate_transition

from ..json_types import JsonObject
from .agent_governance_records import AgentChangeSetProjectionRecord
from .agent_job_records import AgentJobProjectionRecord
from .base import StrictRuntimeRecord
from .eval_run_records import EvalRunProjectionRecord
from .execution_records import ExecutionApplicationRecord


OptimizationTaskStatus = Literal[
    "pending_execution",
    "execution_planning",
    "execution_ready",
    "needs_human_review",
    "failed",
    "execution_failed",
    "applied_pending_regression",
    "regression_running",
    "regression_passed",
    "regression_failed",
    "completed",
]


class OptimizationTaskPlanSnapshotRecord(StrictRuntimeRecord):
    """Compatibility snapshot for a task created from an optimization plan."""

    model_config = ConfigDict(extra="ignore")

    optimization_plan_id: Optional[str] = None
    batch_id: Optional[str] = None
    plan_task_id: Optional[str] = None
    status: Optional[str] = None
    actionability: Optional[str] = None
    target_type: Optional[str] = None
    target_path: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    objective: Optional[str] = None
    target_summary: Optional[str] = None
    recommendation: Optional[str] = None
    recommended_actions: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    expected_effect: Optional[str] = None
    validation: Optional[str] = None
    risk: Optional[str] = None
    source_batch_id: Optional[str] = None
    source_plan_task_id: Optional[str] = None
    source_feedback_case_ids: list[str] = Field(default_factory=list)
    regeneration_instruction: Optional[str] = None


class OptimizationTaskRecord(StrictRuntimeRecord):
    """Internal source of truth for optimization task payload_json."""

    optimization_task_id: str
    created_at: str
    status: OptimizationTaskStatus
    proposal_id: Optional[str] = None
    proposal_ids: list[str] = Field(default_factory=list)
    feedback_case_id: Optional[str] = None
    execution_mode: Literal["manual_or_patch"] = "manual_or_patch"
    source: str = "feedback_workbench"
    comment: Optional[str] = None
    target_paths: list[str] = Field(default_factory=list)
    proposal: Optional[OptimizationTaskPlanSnapshotRecord] = None
    baseline_agent_version_id: Optional[str] = None
    execution_job_ids: list[str] = Field(default_factory=list)
    latest_execution_job_id: Optional[str] = None
    latest_execution_job: Optional[AgentJobProjectionRecord] = None
    pre_execution_agent_version_id: Optional[str] = None
    pre_execution_agent_version: Optional[JsonObject] = None
    latest_change_set_id: Optional[str] = None
    latest_change_set: Optional[AgentChangeSetProjectionRecord] = None
    candidate_commit_sha: Optional[str] = None
    applied_at: Optional[str] = None
    applied_agent_version_id: Optional[str] = None
    applied_agent_version: Optional[JsonObject] = None
    application_note: Optional[str] = None
    latest_execution_application_id: Optional[str] = None
    latest_execution_application: Optional[ExecutionApplicationRecord] = None
    regression_run_ids: list[str] = Field(default_factory=list)
    latest_regression_run_id: Optional[str] = None
    latest_regression_run: Optional[EvalRunProjectionRecord] = None
    regression_completed_at: Optional[str] = None
    source_batch_id: Optional[str] = None
    source_plan_task_id: Optional[str] = None
    feedback_case_ids: list[str] = Field(default_factory=list)
    eval_case_ids: list[str] = Field(default_factory=list)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in TASK_STATES:
            raise ValueError(f"unsupported optimization task status: {value}")
        return value

    @field_validator("proposal_ids", "target_paths", "execution_job_ids", "regression_run_ids", "feedback_case_ids", "eval_case_ids")
    @classmethod
    def validate_string_list(cls, value: list[str]) -> list[str]:
        return [str(item) for item in value if item]

    @model_validator(mode="after")
    def validate_task_shape(self) -> "OptimizationTaskRecord":
        if not self.proposal_id and not self.proposal_ids and not self.source_batch_id:
            raise ValueError("optimization task must reference a proposal or optimization batch")
        if not self.target_paths:
            raise ValueError("optimization task must include target_paths")
        if self.applied_agent_version_id and not self.applied_at:
            raise ValueError("applied_at is required when applied_agent_version_id is set")
        if self.latest_execution_job_id and self.latest_execution_job is None:
            raise ValueError("latest_execution_job is required when latest_execution_job_id is set")
        if (
            self.latest_regression_run_id
            and self.status != "regression_running"
            and self.latest_regression_run is None
        ):
            raise ValueError("latest_regression_run is required after regression run completes")
        return self

    def transition_to(
        self,
        status: str,
        *,
        fields: JsonObject | None = None,
    ) -> "OptimizationTaskRecord":
        validate_transition("task", self.status, status)
        payload = self.to_payload()
        payload.update(fields or {})
        payload["status"] = status
        return type(self).model_validate(payload)

    def to_payload(self) -> JsonObject:
        return self.model_dump(mode="json")

    @classmethod
    def from_row(cls, row: OptimizationTaskModel) -> "OptimizationTaskRecord":
        payload = dict(row.payload_json or {})
        payload.update(
            {
                "optimization_task_id": row.optimization_task_id,
                "created_at": row.created_at,
                "status": row.status,
                "proposal_id": row.proposal_id,
                "feedback_case_id": row.feedback_case_id,
            }
        )
        return cls.model_validate(payload)
