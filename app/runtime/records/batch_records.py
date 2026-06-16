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

_EXECUTABLE_PLAN_KINDS = {"workspace_execution", "external_webhook"}

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
    agent_id: str = "main-agent"
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
        payload = _sanitize_legacy_batch_payload(dict(row.payload_json or {}))
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


def _sanitize_legacy_batch_payload(batch_payload: dict[str, object]):
    plan = batch_payload.get("optimization_plan")
    if isinstance(plan, dict):
        batch_payload["optimization_plan"] = _sanitize_legacy_optimization_plan(plan)
    if isinstance(batch_payload.get("execution_runs"), list):
        batch_payload["execution_runs"] = [
            _sanitize_legacy_execution_run(item) for item in batch_payload["execution_runs"] if isinstance(item, dict)
        ]
    latest_run = batch_payload.get("latest_execution_run")
    if isinstance(latest_run, dict):
        batch_payload["latest_execution_run"] = _sanitize_legacy_execution_run(latest_run)
    return batch_payload


def _sanitize_legacy_optimization_plan(plan: dict[str, object]):
    sanitized = dict(plan)
    tasks: list[dict[str, object]] = []
    blocked_items = [dict(item) for item in sanitized.get("blocked_items") or [] if isinstance(item, dict)]
    for index, item in enumerate(sanitized.get("tasks") or [], start=1):
        if not isinstance(item, dict):
            continue
        task = dict(item)
        task.pop("internal_action", None)
        if task.get("execution_kind") in _EXECUTABLE_PLAN_KINDS:
            tasks.append(task)
        else:
            blocked_items.append(_legacy_internal_action_blocked_item(item, index))
    sanitized["tasks"] = tasks
    sanitized["blocked_items"] = blocked_items
    sanitized["task_summary"] = {
        "total": len(tasks),
        "workspace_execution": sum(1 for item in tasks if item.get("execution_kind") == "workspace_execution"),
        "external_webhook": sum(1 for item in tasks if item.get("execution_kind") == "external_webhook"),
    }
    sanitized["blocked_summary"] = {"total": len(blocked_items)}
    return sanitized


def _legacy_internal_action_blocked_item(task: dict[str, object], index: int):
    blocked: dict[str, object] = {
        "blocked_item_id": task.get("blocked_item_id") or task.get("plan_task_id") or f"legacy-internal-action-{index}",
        "source_index": int(task.get("source_index") or index),
        "status": "blocked",
        "title": task.get("title") or "历史内部动作已停用",
        "target_type": task.get("target_type") or "legacy_internal_action",
        "reason": _legacy_internal_action_reason(task),
    }
    for key in (
        "target_path",
        "owner",
        "actionability",
        "confidence",
        "problem_type",
        "analysis_summary",
        "evidence_summary",
        "evidence_refs",
        "recommendation",
        "feedback_case_ids",
        "eval_case_ids",
        "attribution_job_ids",
        "task_context",
        "created_at",
        "updated_at",
    ):
        if task.get(key) is not None:
            blocked[key] = task[key]
    return blocked


def _legacy_internal_action_reason(task: dict[str, object]) -> str:
    execution_kind = str(task.get("execution_kind") or "")
    internal_action = str(task.get("internal_action") or "")
    legacy_name = internal_action or execution_kind or "unknown"
    return f"历史内部动作 {legacy_name} 已停用；评估用例治理需通过回归测试用例界面手动执行。"


def _sanitize_legacy_execution_run(run: dict[str, object]):
    sanitized = dict(run)
    results: list[dict[str, object]] = []
    omitted = 0
    for item in sanitized.get("task_results") or []:
        if not isinstance(item, dict):
            continue
        result = dict(item)
        result.pop("internal_action", None)
        if result.get("execution_kind") in _EXECUTABLE_PLAN_KINDS:
            results.append(result)
        else:
            omitted += 1
    if omitted:
        warnings = [str(item) for item in sanitized.get("warnings") or [] if item]
        warnings.append(f"已隐藏 {omitted} 条停用的历史内部动作执行结果。")
        sanitized["warnings"] = warnings
        if not results and sanitized.get("status") == "completed":
            sanitized["status"] = "partial_failed"
    sanitized["task_results"] = results
    return sanitized
