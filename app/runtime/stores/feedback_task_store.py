from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy import select

from ..errors import ConflictError
from ..json_types import JsonObject
from ..records.optimization_task_records import OptimizationTaskRecord
from ..runtime_db import OptimizationTaskModel, utc_now


MANUAL_APPLY_TASK_STATES = {"pending_execution", "failed", "needs_human_review"}


OptimizationTaskPayload = JsonObject


class FeedbackTaskStoreMixin:
    """Store operations for optimization task records and task state snapshots."""

    def create_task(self, *, proposal_id: str, execution_mode: str = "manual_or_patch", comment: Optional[str] = None) -> Optional[JsonObject]:
        proposal = self.find_proposal(proposal_id)
        if not proposal or proposal.get("status") != "approved":
            return None
        if proposal.get("actionability") == "external_guidance":
            return None
        target_path = self._string(proposal.get("target_path"))
        if not target_path or not self._target_allowed(target_path):
            return None
        existing_task = self._find_latest_task_for_proposal(proposal_id)
        if existing_task:
            return existing_task
        task = self._scrub_record(
            {
                "optimization_task_id": f"opt-{uuid.uuid4()}",
                "created_at": utc_now(),
                "status": "pending_execution",
                "proposal_id": proposal_id,
                "proposal_ids": [proposal_id],
                "feedback_case_id": proposal.get("feedback_case_id"),
                "execution_mode": execution_mode,
                "source": "feedback_workbench",
                "comment": comment,
                "target_paths": [target_path],
                "proposal": proposal,
                "baseline_agent_version_id": proposal.get("base_agent_version_id") or self._current_agent_version_id(),
                "execution_job_ids": [],
                "latest_execution_job_id": None,
                "latest_execution_job": None,
            }
        )
        record = OptimizationTaskRecord.model_validate(task)
        with self.Session.begin() as db:
            db.add(
                OptimizationTaskModel(
                    optimization_task_id=record.optimization_task_id,
                    created_at=record.created_at,
                    status=record.status,
                    proposal_id=record.proposal_id,
                    feedback_case_id=record.feedback_case_id,
                    payload_json=record.to_payload(),
                )
            )
        return record.to_payload()

    def create_task_from_optimization_plan(
        self,
        *,
        batch: JsonObject,
        plan: JsonObject,
        plan_task: Optional[JsonObject] = None,
        execution_mode: str = "manual_or_patch",
        comment: Optional[str] = None,
    ) -> Optional[OptimizationTaskPayload]:
        source = plan_task if isinstance(plan_task, dict) else plan
        target_path = self._string(source.get("target_path")) or self._string(plan.get("target_path"))
        if not target_path or not self._target_allowed(target_path):
            return None
        batch_id = self._string(batch.get("batch_id"))
        if not batch_id:
            return None
        plan_task_id = self._string(source.get("plan_task_id")) if plan_task else None
        existing_task = self._find_latest_task_for_plan_source(batch_id, plan_task_id)
        if existing_task:
            return existing_task
        feedback_case_ids = self._unique_strings(source.get("feedback_case_ids") or batch.get("feedback_case_ids") or [])
        eval_case_ids = self._unique_strings(source.get("eval_case_ids") or batch.get("eval_case_ids") or [])
        feedback_case_id = self._latest(feedback_case_ids) or self._latest(batch.get("feedback_case_ids"))
        snapshot = self._optimization_task_plan_snapshot(
            batch=batch,
            plan=plan,
            source=source,
            target_path=target_path,
            plan_task_id=plan_task_id,
        )
        task = self._scrub_record(
            {
                "optimization_task_id": f"opt-{uuid.uuid4()}",
                "created_at": utc_now(),
                "status": "pending_execution",
                "proposal_id": None,
                "proposal_ids": [],
                "feedback_case_id": feedback_case_id,
                "execution_mode": execution_mode,
                "source": "feedback_optimization_batch",
                "comment": comment,
                "target_paths": [target_path],
                "proposal": snapshot,
                "baseline_agent_version_id": source.get("base_agent_version_id") or self._current_agent_version_id(),
                "execution_job_ids": [],
                "latest_execution_job_id": None,
                "latest_execution_job": None,
                "source_batch_id": batch_id,
                "source_plan_task_id": plan_task_id,
                "feedback_case_ids": feedback_case_ids,
                "eval_case_ids": eval_case_ids,
            }
        )
        record = OptimizationTaskRecord.model_validate(task)
        with self.Session.begin() as db:
            db.add(
                OptimizationTaskModel(
                    optimization_task_id=record.optimization_task_id,
                    created_at=record.created_at,
                    status=record.status,
                    proposal_id=record.proposal_id,
                    feedback_case_id=record.feedback_case_id,
                    payload_json=record.to_payload(),
                )
            )
        return record.to_payload()

    def _find_latest_task_for_proposal(self, proposal_id: str) -> Optional[JsonObject]:
        with self.Session() as db:
            row = db.scalars(
                select(OptimizationTaskModel)
                .where(OptimizationTaskModel.proposal_id == proposal_id)
                .order_by(OptimizationTaskModel.created_at.desc())
            ).first()
            return self._task_to_dict(row) if row else None

    def _find_latest_task_for_plan_source(self, batch_id: str, plan_task_id: Optional[str]) -> Optional[JsonObject]:
        with self.Session() as db:
            rows = db.scalars(
                select(OptimizationTaskModel)
                .order_by(OptimizationTaskModel.created_at.desc())
                .limit(1000)
            ).all()
            for row in rows:
                task = self._task_to_dict(row)
                if task.get("source_batch_id") != batch_id:
                    continue
                if plan_task_id:
                    if task.get("source_plan_task_id") == plan_task_id:
                        return task
                elif not task.get("source_plan_task_id"):
                    return task
        return None

    def _optimization_task_plan_snapshot(
        self,
        *,
        batch: JsonObject,
        plan: JsonObject,
        source: JsonObject,
        target_path: str,
        plan_task_id: Optional[str],
    ) -> JsonObject:
        return self._scrub_record(
            {
                "optimization_plan_id": plan.get("optimization_plan_id"),
                "batch_id": batch.get("batch_id"),
                "plan_task_id": plan_task_id,
                "status": "approved",
                "actionability": source.get("actionability") or plan.get("actionability") or "direct_workspace_change",
                "target_type": source.get("target_type") or plan.get("target_type") or plan.get("optimization_object_type"),
                "target_path": target_path,
                "title": source.get("title") or plan.get("title") or "反馈优化任务",
                "description": source.get("description") or "",
                "objective": source.get("objective") or "",
                "target_summary": source.get("target_summary") or "",
                "task_context": source.get("task_context") if isinstance(source.get("task_context"), dict) else {},
                "recommendation": source.get("recommendation") or plan.get("recommendation") or "",
                "recommended_actions": source.get("recommended_actions") or [],
                "acceptance_criteria": source.get("acceptance_criteria") or [],
                "expected_effect": source.get("expected_effect") or plan.get("expected_effect") or "",
                "validation": source.get("validation") or plan.get("validation") or "",
                "risk": source.get("risk") or plan.get("risk") or "",
                "regeneration_instruction": plan.get("regeneration_instruction"),
                "analysis_summary": source.get("analysis_summary") or "",
                "evidence_summary": source.get("evidence_summary") or "",
                "evidence_refs": source.get("evidence_refs") or plan.get("evidence_refs") or [],
                "requires_approval": True,
                "base_agent_version_id": self._current_agent_version_id(),
                "source_batch_id": batch.get("batch_id"),
                "source_plan_task_id": plan_task_id,
                "source_feedback_case_ids": source.get("feedback_case_ids") or batch.get("feedback_case_ids") or [],
                "source_refs": batch.get("source_refs") or [],
            }
        )

    def list_tasks(self, *, feedback_case_id: Optional[str] = None, status: Optional[str] = None, limit: int = 100) -> list[JsonObject]:
        stmt = select(OptimizationTaskModel).order_by(OptimizationTaskModel.created_at.desc()).limit(limit)
        if feedback_case_id:
            stmt = stmt.where(OptimizationTaskModel.feedback_case_id == feedback_case_id)
        if status:
            stmt = stmt.where(OptimizationTaskModel.status == status)
        with self.Session() as db:
            return [self._task_to_dict(row) for row in db.scalars(stmt).all()]

    def target_allowed(self, target_path: str) -> bool:
        return self._target_allowed(target_path)

    def find_task(self, task_id: str) -> Optional[JsonObject]:
        record = self.find_task_record(task_id)
        return record.to_payload() if record else None

    def find_task_record(self, task_id: str) -> Optional[OptimizationTaskRecord]:
        if not task_id:
            return None
        with self.Session() as db:
            row = db.get(OptimizationTaskModel, task_id)
            return OptimizationTaskRecord.from_row(row) if row else None

    def mark_task_applied(
        self,
        task_id: str,
        *,
        agent_version: JsonObject,
        note: Optional[str] = None,
        pre_execution_version: Optional[JsonObject] = None,
        execution_job: Optional[JsonObject] = None,
    ) -> Optional[JsonObject]:
        record = self.mark_task_applied_record(
            task_id,
            agent_version=agent_version,
            note=note,
            pre_execution_version=pre_execution_version,
            execution_job=execution_job,
        )
        return record.to_payload() if record else None

    def mark_task_applied_record(
        self,
        task_id: str,
        *,
        agent_version: JsonObject,
        note: Optional[str] = None,
        pre_execution_version: Optional[JsonObject] = None,
        execution_job: Optional[JsonObject] = None,
    ) -> Optional[OptimizationTaskRecord]:
        task = self.find_task_record(task_id)
        if not task:
            return None
        if task.applied_agent_version_id:
            return task
        if execution_job is None:
            self._assert_task_can_mark_applied_manually(task)
        with self.Session.begin() as db:
            row = self._mark_task_applied_row(
                db,
                task,
                agent_version=agent_version,
                note=note,
                pre_execution_version=pre_execution_version,
                execution_job=execution_job,
            )
            if not row:
                return None
            updated_task = OptimizationTaskRecord.from_row(row)
            self._sync_task_execution_to_source_batch_row(db, updated_task.to_payload(), execution_job)
        return self.find_task_record(task_id)

    def ensure_task_can_mark_applied_manually(self, task_id: str) -> Optional[JsonObject]:
        task = self.ensure_task_can_mark_applied_manually_record(task_id)
        return task.to_payload() if task else None

    def ensure_task_can_mark_applied_manually_record(self, task_id: str) -> Optional[OptimizationTaskRecord]:
        task = self.find_task_record(task_id)
        if not task:
            return None
        if not task.applied_agent_version_id:
            self._assert_task_can_mark_applied_manually(task)
        return task

    def _assert_task_can_mark_applied_manually(self, task: OptimizationTaskRecord) -> None:
        if task.status not in MANUAL_APPLY_TASK_STATES:
            raise ConflictError("Task cannot be marked applied from current status")

    def update_task_status(
        self,
        task_id: str,
        *,
        status: str,
        fields: Optional[JsonObject] = None,
    ) -> Optional[JsonObject]:
        return self._update_task_payload(task_id, status=status, fields=fields or {})


    def _update_task_payload(
        self,
        task_id: str,
        *,
        status: str,
        fields: JsonObject,
    ) -> Optional[JsonObject]:
        with self.Session.begin() as db:
            if not self._update_task_payload_row(db, task_id, status=status, fields=fields):
                return None
        return self.find_task(task_id)

    def _update_task_payload_row(self, db: Any, task_id: str, *, status: str, fields: JsonObject) -> Optional[OptimizationTaskModel]:
        row = db.get(OptimizationTaskModel, task_id)
        if not row:
            return None
        record = OptimizationTaskRecord.from_row(row).transition_to(status, fields=fields)
        row.status = record.status
        row.proposal_id = record.proposal_id
        row.feedback_case_id = record.feedback_case_id
        row.payload_json = record.to_payload()
        return row

    def _task_to_dict(self, row: OptimizationTaskModel) -> JsonObject:
        return OptimizationTaskRecord.from_row(row).to_payload()

    def _mark_task_applied_row(
        self,
        db: Any,
        task: OptimizationTaskRecord,
        *,
        agent_version: JsonObject,
        note: Optional[str] = None,
        pre_execution_version: Optional[JsonObject] = None,
        execution_job: Optional[JsonObject] = None,
    ) -> Optional[OptimizationTaskModel]:
        fields = {
            "applied_at": utc_now(),
            "applied_agent_version_id": self._string(agent_version.get("agent_version_id")),
            "applied_agent_version": agent_version,
            "application_note": note,
        }
        if pre_execution_version:
            fields["pre_execution_agent_version_id"] = self._string(pre_execution_version.get("agent_version_id"))
            fields["pre_execution_agent_version"] = pre_execution_version
        if execution_job:
            job_id = self._string(execution_job.get("execution_job_id"))
            job_ids = [str(item) for item in task.execution_job_ids if item]
            if job_id and job_id not in job_ids:
                job_ids.append(job_id)
            fields["execution_job_ids"] = job_ids
            fields["latest_execution_job_id"] = job_id
            fields["latest_execution_job"] = execution_job
        return self._update_task_payload_row(
            db,
            task.optimization_task_id,
            status="applied_pending_regression",
            fields=fields,
        )

    def _attach_execution_job_to_task(self, task_id: str, job: JsonObject, *, status: str) -> Optional[JsonObject]:
        task = self.find_task(task_id)
        if not task:
            return None
        with self.Session.begin() as db:
            if not self._attach_execution_job_to_task_row(db, task, job, status=status):
                return None
        return self.find_task(task_id)

    def _attach_execution_job_to_task_row(
        self,
        db: Any,
        task: JsonObject,
        job: JsonObject,
        *,
        status: str,
    ) -> Optional[OptimizationTaskModel]:
        job_id = self._string(job.get("execution_job_id"))
        job_ids = [str(item) for item in task.get("execution_job_ids") or [] if item]
        if job_id and job_id not in job_ids:
            job_ids.append(job_id)
        fields = {
            "execution_job_ids": job_ids,
            "latest_execution_job_id": job_id,
            "latest_execution_job": job,
        }
        if job.get("baseline_agent_version_id"):
            fields["baseline_agent_version_id"] = job.get("baseline_agent_version_id")
        if job.get("pre_execution_agent_version_id"):
            fields["pre_execution_agent_version_id"] = job.get("pre_execution_agent_version_id")
            fields["pre_execution_agent_version"] = job.get("pre_execution_agent_version")
        if job.get("applied_agent_version_id"):
            fields["applied_agent_version_id"] = job.get("applied_agent_version_id")
            fields["applied_agent_version"] = job.get("applied_agent_version")
            fields["applied_at"] = job.get("completed_at") or utc_now()
            fields["application_note"] = f"execution-optimizer 应用执行变更 {job.get('execution_job_id')}。"
        return self._update_task_payload_row(db, str(task["optimization_task_id"]), status=status, fields=fields)


    def _attach_task_regression_run(self, task_id: str, eval_run: JsonObject, *, status: str) -> Optional[JsonObject]:
        task = self.find_task(task_id)
        if not task:
            return None
        run_id = self._string(eval_run.get("eval_run_id"))
        run_ids = [str(item) for item in task.get("regression_run_ids") or [] if item]
        if run_id and run_id not in run_ids:
            run_ids.append(run_id)
        return self._update_task_payload(
            task_id,
            status=status,
            fields={
                "regression_run_ids": run_ids,
                "latest_regression_run_id": run_id,
                "latest_regression_run": eval_run,
                "regression_completed_at": eval_run.get("completed_at"),
            },
        )
