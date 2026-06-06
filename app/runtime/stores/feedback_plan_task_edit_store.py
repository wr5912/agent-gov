from __future__ import annotations

from typing import Any, Optional

from pydantic import Field
from sqlalchemy import delete, select

from ..errors import BusinessRuleViolation, ConflictError
from ..external_governance_mapping import apply_external_governance_record, external_governance_record_from_row
from ..json_types import JsonObject
from ..records.base import StrictRuntimeRecord
from ..records.batch_plan_records import FeedbackOptimizationPlanTaskRecord
from ..records.common_records import FeedbackOptimizationEvidenceRefRecord, FeedbackOptimizationTaskContextRecord
from ..records.external_governance_records import ExternalGovernanceItemRecord
from ..records.optimization_task_records import OptimizationTaskRecord
from ..runtime_db import AgentJobModel, ExecutionApplicationModel, ExternalGovernanceItemModel, FeedbackOptimizationBatchModel, OptimizationTaskModel, utc_now
from ..state_machines import JOB_IN_PROGRESS_STATES

EDITABLE_PLAN_TASK_FIELDS = {
    "title",
    "description",
    "objective",
    "target_summary",
    "target_type",
    "target_path",
    "actionability",
    "owner",
    "recommendation",
    "recommended_actions",
    "acceptance_criteria",
    "expected_effect",
    "validation",
    "risk",
    "task_context",
    "evidence_summary",
    "evidence_refs",
    "eval_case_ids",
    "edit_note",
}

PLAN_TASK_PROPOSAL_FIELDS = {
    "optimization_plan_id",
    "batch_id",
    "plan_task_id",
    "status",
    "actionability",
    "target_type",
    "target_path",
    "title",
    "description",
    "objective",
    "target_summary",
    "task_context",
    "recommendation",
    "recommended_actions",
    "acceptance_criteria",
    "expected_effect",
    "validation",
    "risk",
    "regeneration_instruction",
    "analysis_summary",
    "evidence_summary",
    "evidence_refs",
    "edit_note",
    "source_batch_id",
    "source_plan_task_id",
    "source_feedback_case_ids",
}

WORKSPACE_EDITABLE_STATUSES = {"pending_execution", "execution_planning", "execution_ready", "execution_failed", "needs_human_review", "failed"}
EXTERNAL_EDITABLE_STATUSES = {"pending_notification", "notification_failed"}
INTERNAL_EDITABLE_STATUSES = {"pending_execution", "needs_human_review", "failed"}


class FeedbackPlanTaskEditResultRecord(StrictRuntimeRecord):
    batch: JsonObject
    plan_task: JsonObject
    optimization_task: Optional[JsonObject] = None
    invalidated_execution_job_ids: list[str] = Field(default_factory=list)
    external_item: Optional[JsonObject] = None


class FeedbackPlanTaskEditStoreMixin:
    """User-editable optimization plan task operations."""

    def update_batch_plan_task(
        self,
        batch_id: str,
        plan_task_id: str,
        updates: JsonObject,
    ) -> Optional[FeedbackPlanTaskEditResultRecord]:
        cleanup_job_ids: list[str] = []
        with self.Session.begin() as db:
            metadata = self._update_batch_plan_task_edit_row(
                db,
                batch_id,
                plan_task_id,
                updates,
                cleanup_job_ids=cleanup_job_ids,
            )
            if metadata is None:
                return None
        for job_id in cleanup_job_ids:
            self._cleanup_job_tmp(job_id)
        batch = self.find_optimization_batch(batch_id)
        task_id = self._string(metadata.get("optimization_task_id")) if metadata else None
        external_item_id = self._string(metadata.get("external_item_id")) if metadata else None
        return FeedbackPlanTaskEditResultRecord.model_validate(
            {
                "batch": batch,
                "plan_task": self._plan_task_from_batch(batch, plan_task_id),
                "optimization_task": self.find_task(task_id) if task_id else None,
                "invalidated_execution_job_ids": metadata.get("invalidated_execution_job_ids") or [],
                "external_item": self.find_external_governance_item(external_item_id) if external_item_id else None,
            }
        )

    def _update_batch_plan_task_edit_row(
        self,
        db: Any,
        batch_id: str,
        plan_task_id: str,
        updates: JsonObject,
        *,
        cleanup_job_ids: list[str],
    ) -> Optional[JsonObject]:
        row = db.get(FeedbackOptimizationBatchModel, batch_id)
        if not row:
            return None
        batch = self._batch_payload_snapshot(row)
        plan = batch.get("optimization_plan") if isinstance(batch.get("optimization_plan"), dict) else None
        if not plan:
            return None
        tasks = [dict(item) for item in plan.get("tasks") or [] if isinstance(item, dict)]
        task_index = next((index for index, item in enumerate(tasks) if self._string(item.get("plan_task_id")) == plan_task_id), None)
        if task_index is None:
            return None

        current_task = FeedbackOptimizationPlanTaskRecord.model_validate(tasks[task_index]).to_payload()
        edit_updates = self._normalized_plan_task_edit_updates(updates)
        if not edit_updates:
            raise BusinessRuleViolation("No editable optimization task fields were provided")
        self._assert_plan_task_editable(current_task)

        now = utc_now()
        edited_task = self._edited_plan_task_payload(batch, plan, current_task, edit_updates, updated_at=now)
        invalidated_job_ids: list[str] = []
        optimization_task_id = self._string(edited_task.get("optimization_task_id"))
        if optimization_task_id:
            self._sync_edited_optimization_task_row(db, edited_task, optimization_task_id, invalidated_job_ids, cleanup_job_ids)
        external_item_id = self._sync_edited_external_item_row(db, batch, plan, edited_task, updated_at=now)

        tasks[task_index] = edited_task
        next_plan = {**plan, "tasks": tasks, "updated_at": now, "task_summary": self._plan_task_summary(tasks)}
        batch_status = self._batch_status_after_plan_task_edit(batch, edited_task)
        fields: JsonObject = {"optimization_plan": next_plan}
        if optimization_task_id:
            updated_task = self._task_to_dict(db.get(OptimizationTaskModel, optimization_task_id))
            fields.update(
                {
                    "optimization_task_id": optimization_task_id,
                    "optimization_task": updated_task,
                    "execution_job_id": None,
                    "execution_job": None,
                }
            )
        updated_batch_row = self._update_batch_row(db, batch_id, status=batch_status, fields=fields)
        if not updated_batch_row:
            return None
        return {
            "optimization_task_id": optimization_task_id,
            "external_item_id": external_item_id,
            "invalidated_execution_job_ids": invalidated_job_ids,
        }

    def _normalized_plan_task_edit_updates(self, updates: JsonObject) -> JsonObject:
        normalized: JsonObject = {}
        for field in EDITABLE_PLAN_TASK_FIELDS:
            if field not in updates:
                continue
            value = updates[field]
            if field == "task_context":
                normalized[field] = FeedbackOptimizationTaskContextRecord.model_validate(value or {}).to_payload()
            elif field == "evidence_refs":
                normalized[field] = [FeedbackOptimizationEvidenceRefRecord.model_validate(item).to_payload() for item in value or [] if isinstance(item, dict)]
            elif field in {"recommended_actions", "acceptance_criteria", "eval_case_ids"}:
                normalized[field] = self._string_list(value)
            elif value is None:
                normalized[field] = ""
            else:
                normalized[field] = self._string(value) if isinstance(value, str) else value
        return normalized

    def _assert_plan_task_editable(self, plan_task: JsonObject) -> None:
        execution_kind = self._string(plan_task.get("execution_kind"))
        status = self._string(plan_task.get("status"))
        if plan_task.get("applied_agent_version_id") or plan_task.get("internal_action_result"):
            raise ConflictError("Optimization plan task has already been applied")
        if execution_kind == "workspace_execution" and status not in WORKSPACE_EDITABLE_STATUSES:
            raise ConflictError("Workspace optimization task is not editable from current status")
        if execution_kind == "external_webhook":
            if status == "notified":
                raise ConflictError("External optimization task has already been notified")
            if status not in EXTERNAL_EDITABLE_STATUSES:
                raise ConflictError("External optimization task is not editable from current status")
        if execution_kind == "internal_action" and status not in INTERNAL_EDITABLE_STATUSES:
            raise ConflictError("Internal optimization task is not editable from current status")

    def _edited_plan_task_payload(
        self,
        batch: JsonObject,
        plan: JsonObject,
        current_task: JsonObject,
        updates: JsonObject,
        *,
        updated_at: str,
    ) -> JsonObject:
        execution_kind = self._string(current_task.get("execution_kind"))
        merged = {**current_task, **updates, "updated_at": updated_at}
        if execution_kind == "workspace_execution":
            target_path = self._string(merged.get("target_path"))
            if not target_path or not self._target_allowed(target_path):
                raise ConflictError("Optimization plan task target is not actionable")
            merged.update(
                {
                    "status": "pending_execution",
                    "execution_job_id": None,
                    "latest_execution_job": None,
                }
            )
        elif execution_kind == "external_webhook":
            merged.update(
                {
                    "status": "pending_notification",
                    "latest_webhook_alias": None,
                    "latest_notification": None,
                }
            )
        elif execution_kind == "internal_action":
            self._internal_action_eval_case_ids(batch, merged)
            merged["status"] = "pending_execution"
        else:
            raise ConflictError("Optimization plan task is not editable")
        plan_task = FeedbackOptimizationPlanTaskRecord.model_validate(merged).to_payload()
        if not plan_task.get("target_summary"):
            plan_task["target_summary"] = self._plan_task_target_summary(
                self._string(plan_task.get("target_type")),
                execution_kind,
                self._string(plan_task.get("owner")),
                self._string(plan_task.get("target_path")),
            )
        if plan.get("regeneration_instruction") and not plan_task.get("regeneration_instruction"):
            plan_task["regeneration_instruction"] = plan.get("regeneration_instruction")
        return plan_task

    def _sync_edited_optimization_task_row(
        self,
        db: Any,
        plan_task: JsonObject,
        optimization_task_id: str,
        invalidated_job_ids: list[str],
        cleanup_job_ids: list[str],
    ) -> None:
        task_row = db.get(OptimizationTaskModel, optimization_task_id, with_for_update=True)
        if not task_row:
            raise ConflictError("Linked optimization task was not found")
        task = OptimizationTaskRecord.from_row(task_row)
        if task.applied_agent_version_id:
            raise ConflictError("Optimization task has already been applied")
        execution_rows = db.scalars(
            select(AgentJobModel).where(
                AgentJobModel.job_type == "execution",
                AgentJobModel.scope_kind == "optimization_task",
                AgentJobModel.scope_id == optimization_task_id,
            )
        ).all()
        active_jobs = [row.job_id for row in execution_rows if row.status in JOB_IN_PROGRESS_STATES]
        if active_jobs:
            raise ConflictError("Execution plan generation is still running; edit after it finishes")
        for execution_row in execution_rows:
            invalidated_job_ids.append(execution_row.job_id)
            cleanup_job_ids.append(execution_row.job_id)
            db.delete(execution_row)
        if invalidated_job_ids:
            db.execute(delete(ExecutionApplicationModel).where(ExecutionApplicationModel.optimization_task_id == optimization_task_id))

        proposal = self._proposal_snapshot_from_plan_task(task, plan_task)
        self._update_task_payload_row(
            db,
            optimization_task_id,
            status="pending_execution",
            fields={
                "target_paths": [self._string(plan_task.get("target_path"))],
                "proposal": proposal,
                "execution_job_ids": [],
                "latest_execution_job_id": None,
                "latest_execution_job": None,
                "pre_execution_agent_version_id": None,
                "pre_execution_agent_version": None,
                "latest_execution_application_id": None,
                "latest_execution_application": None,
            },
        )

    def _proposal_snapshot_from_plan_task(self, task: OptimizationTaskRecord, plan_task: JsonObject) -> JsonObject:
        existing = task.proposal.model_dump(mode="json") if task.proposal else {}
        updates = {field: plan_task.get(field) for field in PLAN_TASK_PROPOSAL_FIELDS if field in plan_task}
        updates.update(
            {
                "status": "approved",
                "source_batch_id": plan_task.get("source_batch_id") or task.source_batch_id,
                "source_plan_task_id": plan_task.get("source_plan_task_id") or task.source_plan_task_id,
                "source_feedback_case_ids": plan_task.get("feedback_case_ids") or task.feedback_case_ids,
            }
        )
        return {**existing, **updates}

    def _sync_edited_external_item_row(
        self,
        db: Any,
        batch: JsonObject,
        plan: JsonObject,
        plan_task: JsonObject,
        *,
        updated_at: str,
    ) -> Optional[str]:
        if plan_task.get("execution_kind") != "external_webhook":
            return None
        external_item_id = self._string(plan_task.get("external_item_id"))
        if not external_item_id:
            return None
        row = db.get(ExternalGovernanceItemModel, external_item_id, with_for_update=True)
        if not row:
            return None
        if row.status == "notified":
            raise ConflictError("External optimization task has already been notified")
        current = external_governance_record_from_row(row)
        detail = self._plan_task_external_detail(batch, plan, plan_task)
        payload = {
            **current.to_payload(),
            **detail,
            "updated_at": updated_at,
            "status": "pending_notification",
            "owner": self._string(plan_task.get("owner")) or current.owner,
            "actionability": self._string(plan_task.get("actionability")) or current.actionability,
            "latest_notification_id": None,
            "latest_webhook_alias": None,
            "latest_notification": None,
        }
        apply_external_governance_record(row, ExternalGovernanceItemRecord.model_validate(payload))
        return external_item_id

    def _batch_status_after_plan_task_edit(self, batch: JsonObject, plan_task: JsonObject) -> str:
        current = self._string(batch.get("status")) or "pending_approval"
        execution_kind = self._string(plan_task.get("execution_kind"))
        if execution_kind == "workspace_execution":
            if current in {"execution_planning", "execution_ready", "execution_failed", "failed", "needs_human_review", "pending_execution"}:
                return "pending_execution"
            return current
        if execution_kind == "external_webhook":
            return "pending_approval" if current in {"sent", "notification_failed", "needs_human_review"} else current
        return current
