from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy import select

from ..errors import ConflictError
from ..records.batch_records import FeedbackOptimizationBatchRecord
from ..json_types import JsonObject
from ..records.optimization_task_records import OptimizationTaskRecord
from ..runtime_db import FeedbackOptimizationBatchModel, OptimizationTaskModel, utc_now
from ..state_machines import JOB_IN_PROGRESS_STATES


class FeedbackBatchStoreMixin:
    """Store operations for optimization batch lifecycle and execution state snapshots."""

    def create_optimization_batch(
        self,
        source_refs: list[JsonObject],
        *,
        title: Optional[str] = None,
        priority: str = "medium",
    ) -> Optional[JsonObject]:
        refs = self._normalize_source_refs(source_refs)
        if not refs:
            return None
        feedback_cases: list[JsonObject] = []
        cases_to_create: list[JsonObject] = []
        refs_to_mark: list[dict[str, str]] = []
        skipped: list[JsonObject] = []
        for ref in refs:
            feedback_case, should_create = self._prepare_feedback_case_for_source(ref, priority=priority)
            if not feedback_case:
                skipped.append({**ref, "reason": "source cannot create feedback case"})
                continue
            feedback_cases.append(feedback_case)
            if should_create:
                cases_to_create.append(feedback_case)
            refs_to_mark.append(ref)
        if not feedback_cases:
            return None
        now = utc_now()
        feedback_case_ids = self._unique_strings([item.get("feedback_case_id") for item in feedback_cases])
        batch_id = f"fob-{uuid.uuid4()}"
        payload = {
            "schema_version": "feedback-optimization-batch/v1",
            "batch_id": batch_id,
            "created_at": now,
            "updated_at": now,
            "status": "draft",
            "title": title or f"反馈优化批次 {len(feedback_case_ids)} 条反馈",
            "priority": priority or "medium",
            "source_refs": refs,
            "feedback_case_ids": feedback_case_ids,
            "skipped_source_refs": skipped,
            "eval_case_ids": [],
            "eval_case_generation": {"created": 0, "reused": 0, "updated": 0, "skipped": 0, "eval_cases": [], "results": []},
            "eval_case_generation_job_id": None,
            "attribution_job_ids": [],
            "optimization_plan": None,
            "optimization_task_id": None,
            "execution_job_id": None,
            "eval_run_id": None,
        }
        with self.Session.begin() as db:
            for feedback_case in cases_to_create:
                db.add(self._case_model_from_dict(feedback_case))
            for ref in refs_to_mark:
                self._upsert_feedback_source_annotation(db, ref["source_kind"], ref["source_id"], {"status": "in_batch", "priority": priority})
            db.add(self._batch_model_from_payload(payload))
        self.queue_feedback_eval_case_generation_agent_job(batch_id=batch_id)
        return self.find_optimization_batch(batch_id)

    def _batch_model_from_payload(self, payload: JsonObject) -> FeedbackOptimizationBatchModel:
        record = FeedbackOptimizationBatchRecord.model_validate(payload)
        return FeedbackOptimizationBatchModel(
            batch_id=record.batch_id,
            created_at=record.created_at,
            updated_at=record.updated_at,
            status=record.status,
            title=record.title,
            payload_json=record.to_payload(),
        )

    def list_optimization_batches(self, *, status: Optional[str] = None, limit: int = 100) -> list[JsonObject]:
        stmt = select(FeedbackOptimizationBatchModel).order_by(FeedbackOptimizationBatchModel.updated_at.desc()).limit(limit)
        if status:
            stmt = stmt.where(FeedbackOptimizationBatchModel.status == status)
        with self.Session() as db:
            return [self._batch_to_dict(row) for row in db.scalars(stmt).all()]

    def find_optimization_batch(self, batch_id: str) -> Optional[JsonObject]:
        if not batch_id:
            return None
        with self.Session() as db:
            row = db.get(FeedbackOptimizationBatchModel, batch_id)
            return self._batch_to_dict(row) if row else None

    def list_batch_eval_cases(self, batch_id: str) -> Optional[list[JsonObject]]:
        batch = self.find_optimization_batch(batch_id)
        if not batch:
            return None
        eval_cases: list[JsonObject] = []
        for eval_case_id in batch.get("eval_case_ids") or []:
            eval_case = self.find_eval_case(str(eval_case_id))
            if eval_case:
                eval_cases.append(eval_case)
        return eval_cases

    def create_batch_eval_case(self, batch_id: str, fields: JsonObject) -> Optional[JsonObject]:
        with self.Session.begin() as db:
            row = db.get(FeedbackOptimizationBatchModel, batch_id)
            if not row:
                return None
            batch = self._batch_payload_snapshot(row)
            eval_case = self._build_manual_batch_eval_case(batch, fields)
            self._add_eval_case_row(db, eval_case)
            self._update_batch_eval_case_ids_row(db, row, append_id=eval_case["eval_case_id"])
        return self.find_eval_case(eval_case["eval_case_id"])

    def update_batch_eval_case(self, batch_id: str, eval_case_id: str, fields: JsonObject) -> Optional[JsonObject]:
        batch = self.find_optimization_batch(batch_id)
        if not batch or eval_case_id not in set(batch.get("eval_case_ids") or []):
            return None
        return self.update_eval_case(eval_case_id, fields)

    def remove_batch_eval_case(self, batch_id: str, eval_case_id: str) -> Optional[JsonObject]:
        with self.Session.begin() as db:
            row = db.get(FeedbackOptimizationBatchModel, batch_id)
            if not row:
                return None
            eval_case_ids = self._string_list(self._batch_payload_snapshot(row).get("eval_case_ids"))
            if eval_case_id not in eval_case_ids:
                return None
            self._update_batch_eval_case_ids_row(db, row, remove_id=eval_case_id)
        return self.find_optimization_batch(batch_id)

    def record_batch_attribution_jobs(self, batch_id: str, jobs: list[JsonObject]) -> Optional[JsonObject]:
        job_ids = self._unique_strings([job.get("job_id") for job in jobs])
        batch = self.find_optimization_batch(batch_id)
        status = self._batch_attribution_status(batch or {}, jobs)
        return self._update_batch(
            batch_id,
            status=status,
            fields={
                "attribution_job_ids": job_ids,
                "attribution_jobs": jobs,
                "attribution_summary": self._batch_attribution_summary(batch or {}, jobs),
            },
        )

    def reset_batch_attribution(self, batch_id: str) -> Optional[JsonObject]:
        batch = self.find_optimization_batch(batch_id)
        if not batch:
            return None
        cleanup_job_ids: list[str] = []
        fields = {
            "attribution_job_ids": [],
            "attribution_jobs": [],
            "attribution_summary": {"total": len(batch.get("feedback_case_ids") or []), "completed": 0, "running": 0, "needs_review_or_failed": 0},
            "optimization_plan": None,
            "internal_proposal_id": None,
            "optimization_task_id": None,
            "optimization_task": None,
            "execution_job_id": None,
            "execution_job": None,
            "eval_run_id": None,
            "latest_eval_run": None,
            "execution_apply_result": None,
        }
        with self.Session.begin() as db:
            row = db.get(FeedbackOptimizationBatchModel, batch_id)
            if not row:
                return None
            payload = self._batch_payload_snapshot(row)
            task_id = self._string(payload.get("optimization_task_id"))
            task = db.get(OptimizationTaskModel, task_id) if task_id else None
            if task and OptimizationTaskRecord.from_row(task).applied_agent_version_id:
                raise ConflictError("当前批次已应用并产生 Agent 版本，不能原地重新归因；请基于反馈信息创建新批次。")
            for feedback_case_id in payload.get("feedback_case_ids") or []:
                self._discard_current_attribution_row(
                    db,
                    str(feedback_case_id),
                    invalidate_downstream=True,
                    cleanup_job_ids=cleanup_job_ids,
                )
            self._discard_batch_draft_artifacts_row(db, payload, cleanup_job_ids)
            if not self._update_batch_row(db, batch_id, status="draft", fields=fields):
                return None
        for job_id in cleanup_job_ids:
            self._cleanup_job_tmp(job_id)
        return self.find_optimization_batch(batch_id)

    def record_batch_execution_result(
        self,
        batch_id: str,
        *,
        execution_job: Optional[JsonObject] = None,
        optimization_task: Optional[JsonObject] = None,
        applied: Optional[JsonObject] = None,
    ) -> Optional[JsonObject]:
        fields: JsonObject = {}
        status = "execution_planning"
        if execution_job:
            fields["execution_job_id"] = execution_job.get("execution_job_id")
            fields["execution_job"] = execution_job
            plan = execution_job.get("validated_output_json") if isinstance(execution_job.get("validated_output_json"), dict) else {}
            status = "execution_ready" if plan.get("status") == "ready" else str(execution_job.get("status") or status)
        if optimization_task:
            fields["optimization_task_id"] = optimization_task.get("optimization_task_id")
            fields["optimization_task"] = optimization_task
            status = str(optimization_task.get("status") or status)
        if applied:
            fields["execution_apply_result"] = applied
            task = applied.get("optimization_task") if isinstance(applied.get("optimization_task"), dict) else None
            if task:
                fields["optimization_task"] = task
                status = str(task.get("status") or "applied_pending_regression")
        return self._update_batch(batch_id, status=status, fields=fields)

    def record_batch_plan_task_execution_result(
        self,
        batch_id: str,
        plan_task_id: str,
        *,
        execution_job: Optional[JsonObject] = None,
        optimization_task: Optional[JsonObject] = None,
        applied: Optional[JsonObject] = None,
    ) -> Optional[JsonObject]:
        task_updates: JsonObject = {}
        top_level_fields: JsonObject = {}
        status = "execution_planning"
        if execution_job:
            task_updates["execution_job_id"] = execution_job.get("execution_job_id")
            task_updates["latest_execution_job"] = execution_job
            plan = execution_job.get("validated_output_json") if isinstance(execution_job.get("validated_output_json"), dict) else {}
            status = "execution_ready" if plan.get("status") == "ready" else str(execution_job.get("status") or status)
            top_level_fields.update({"execution_job_id": execution_job.get("execution_job_id"), "execution_job": execution_job})
        if optimization_task:
            task_updates["optimization_task_id"] = optimization_task.get("optimization_task_id")
            task_updates["status"] = optimization_task.get("status") or status
            task_updates["applied_agent_version_id"] = optimization_task.get("applied_agent_version_id")
            status = str(optimization_task.get("status") or status)
            top_level_fields.update({"optimization_task_id": optimization_task.get("optimization_task_id"), "optimization_task": optimization_task})
        if applied:
            self._capture_batch_apply_result(task_updates, top_level_fields, applied)
            task = applied.get("optimization_task") if isinstance(applied.get("optimization_task"), dict) else None
            if task:
                status = str(task.get("status") or "applied_pending_regression")
        if "status" not in task_updates:
            task_updates["status"] = status
        return self._update_batch_plan_task(batch_id, plan_task_id, task_updates, batch_status=status, top_level_fields=top_level_fields)

    def record_batch_regression_result(self, batch_id: str, eval_run: JsonObject) -> Optional[JsonObject]:
        result_status = str(eval_run.get("result_status") or eval_run.get("status") or "needs_human_review")
        status = {
            "blocked": "blocked",
            "review_required": "needs_human_review",
            "passed_with_notes": "completed",
            "passed": "completed",
        }.get(result_status, result_status)
        return self._update_batch(
            batch_id,
            status=status,
            fields={
                "eval_run_id": eval_run.get("eval_run_id"),
                "latest_eval_run": eval_run,
                "latest_regression_gate": eval_run.get("gate_result") or {},
            },
        )

    def _capture_batch_apply_result(self, task_updates: JsonObject, top_level_fields: JsonObject, applied: JsonObject) -> None:
        task_updates["execution_apply_result"] = applied
        top_level_fields["execution_apply_result"] = applied
        applied_job = applied.get("execution_job") if isinstance(applied.get("execution_job"), dict) else None
        if applied_job:
            task_updates["execution_job_id"] = applied_job.get("execution_job_id")
            task_updates["latest_execution_job"] = applied_job
            top_level_fields["execution_job_id"] = applied_job.get("execution_job_id")
            top_level_fields["execution_job"] = applied_job
        task = applied.get("optimization_task") if isinstance(applied.get("optimization_task"), dict) else None
        if task:
            task_updates["optimization_task_id"] = task.get("optimization_task_id")
            task_updates["status"] = task.get("status") or "applied_pending_regression"
            task_updates["applied_agent_version_id"] = task.get("applied_agent_version_id")
            top_level_fields["optimization_task"] = task

    def _batch_to_dict(self, row: FeedbackOptimizationBatchModel) -> JsonObject:
        payload = FeedbackOptimizationBatchRecord.from_row(row).to_payload()
        task_id = self._string(payload.get("optimization_task_id"))
        execution_job_id = self._string(payload.get("execution_job_id"))
        eval_case_generation_job_id = self._string(payload.get("eval_case_generation_job_id"))
        eval_run_id = self._string(payload.get("eval_run_id"))
        plan = payload.get("optimization_plan") if isinstance(payload.get("optimization_plan"), dict) else None
        if plan is not None:
            payload["optimization_plan"] = self._normalize_plan_task_collections(payload, plan)
        if eval_case_generation_job_id:
            eval_case_generation_job = self.get_agent_job(eval_case_generation_job_id)
            if eval_case_generation_job:
                payload["eval_case_generation_job"] = eval_case_generation_job
        payload = self._refresh_batch_attribution_jobs(payload)
        task = self.find_task(task_id) if task_id else None
        if task:
            payload["optimization_task"] = task
            latest_execution = task.get("latest_execution_job") if isinstance(task.get("latest_execution_job"), dict) else None
            if latest_execution:
                payload["execution_job"] = latest_execution
                payload["execution_job_id"] = latest_execution.get("execution_job_id")
            if not eval_run_id:
                task_status = self._string(task.get("status"))
                if task_status in {"execution_planning", "execution_ready", "execution_failed", "needs_human_review", "failed", "applied_pending_regression", "regression_running"}:
                    payload["status"] = task_status
        elif execution_job_id and not isinstance(payload.get("execution_job"), dict):
            payload["execution_job"] = self.get_execution_job(execution_job_id)
        if eval_run_id and not isinstance(payload.get("latest_eval_run"), dict):
            payload["latest_eval_run"] = self.get_eval_run(eval_run_id)
        return payload

    def _refresh_batch_attribution_jobs(self, payload: JsonObject) -> JsonObject:
        job_ids = self._unique_strings(payload.get("attribution_job_ids") or [])
        if not job_ids:
            return payload
        jobs = [job for job in (self.get_job(job_id) for job_id in job_ids) if job]
        if not jobs:
            return payload
        refreshed = dict(payload)
        refreshed["attribution_jobs"] = jobs
        refreshed["attribution_summary"] = self._batch_attribution_summary(refreshed, jobs)
        if self._batch_can_project_attribution_status(refreshed):
            refreshed["status"] = self._batch_attribution_status(refreshed, jobs)
        return refreshed

    def _batch_attribution_summary(self, batch: JsonObject, jobs: list[JsonObject]) -> JsonObject:
        completed = [job for job in jobs if job.get("status") == "completed"]
        failed = [job for job in jobs if job.get("status") in {"failed", "needs_human_review", "timeout"}]
        running = [job for job in jobs if job.get("status") in JOB_IN_PROGRESS_STATES]
        expected_total = len(batch.get("feedback_case_ids") or [])
        total = max(expected_total, len(jobs))
        return {
            "total": total,
            "completed": len(completed),
            "running": len(running),
            "needs_review_or_failed": len(failed),
        }

    def _batch_attribution_status(self, batch: JsonObject, jobs: list[JsonObject]) -> str:
        summary = self._batch_attribution_summary(batch, jobs)
        if summary["total"] and summary["completed"] == summary["total"]:
            return "attribution_completed"
        if summary["needs_review_or_failed"]:
            return "needs_human_review"
        return "attribution_running"

    def _batch_can_project_attribution_status(self, payload: JsonObject) -> bool:
        if payload.get("optimization_plan") or payload.get("optimization_plan_job_id"):
            return False
        if payload.get("optimization_task_id") or payload.get("execution_job_id") or payload.get("eval_run_id"):
            return False
        if payload.get("optimization_task") or payload.get("execution_job") or payload.get("latest_eval_run"):
            return False
        return self._string(payload.get("status")) in {
            "draft",
            "attribution_running",
            "attribution_completed",
            "attribution_failed",
            "needs_human_review",
        }

    def _update_batch(self, batch_id: str, *, status: str, fields: JsonObject) -> Optional[JsonObject]:
        with self.Session.begin() as db:
            if not self._update_batch_row(db, batch_id, status=status, fields=fields):
                return None
        return self.find_optimization_batch(batch_id)

    def _update_batch_row(self, db: Any, batch_id: str, *, status: str, fields: JsonObject) -> Optional[FeedbackOptimizationBatchModel]:
        now = utc_now()
        row = db.get(FeedbackOptimizationBatchModel, batch_id)
        if not row:
            return None
        current = FeedbackOptimizationBatchRecord.from_row(row)
        next_fields = {**fields, "updated_at": now}
        plan = next_fields.get("optimization_plan")
        if isinstance(plan, dict):
            next_fields["optimization_plan"] = self._normalize_plan_task_collections(current.to_payload(), plan)
        record = current.transition_to(status, fields=next_fields)
        row.status = record.status
        row.updated_at = record.updated_at
        row.title = self._string(record.title) or row.title
        row.payload_json = record.to_payload()
        return row

    def _batch_payload_snapshot(self, row: FeedbackOptimizationBatchModel) -> JsonObject:
        return FeedbackOptimizationBatchRecord.from_row(row).to_payload()

    def _update_batch_eval_case_ids_row(
        self,
        db: Any,
        row: FeedbackOptimizationBatchModel,
        *,
        append_id: Optional[str] = None,
        remove_id: Optional[str] = None,
    ) -> None:
        payload = self._batch_payload_snapshot(row)
        eval_case_ids = self._unique_strings(payload.get("eval_case_ids") or [])
        if append_id:
            eval_case_ids = self._unique_strings([*eval_case_ids, append_id])
        if remove_id:
            eval_case_ids = [item for item in eval_case_ids if item != remove_id]
        fields = {"eval_case_ids": eval_case_ids}
        plan = payload.get("optimization_plan") if isinstance(payload.get("optimization_plan"), dict) else None
        if plan is not None:
            fields["optimization_plan"] = self._sync_plan_eval_case_ids(plan, append_id=append_id, remove_id=remove_id)
        self._update_batch_row(db, row.batch_id, status=row.status, fields=fields)

    def _sync_plan_eval_case_ids(
        self,
        plan: JsonObject,
        *,
        append_id: Optional[str] = None,
        remove_id: Optional[str] = None,
    ) -> JsonObject:
        synced = dict(plan)

        def sync_values(values: Any) -> list[str]:
            items = self._unique_strings(values or [])
            if append_id:
                items = self._unique_strings([*items, append_id])
            if remove_id:
                items = [item for item in items if item != remove_id]
            return items

        synced["eval_case_ids"] = sync_values(synced.get("eval_case_ids"))
        for key in ("tasks", "blocked_items"):
            if not isinstance(synced.get(key), list):
                continue
            synced[key] = [
                {**item, "eval_case_ids": sync_values(item.get("eval_case_ids"))} if isinstance(item, dict) else item
                for item in synced[key]
            ]
        return synced

    def _batch_attribution_outputs(self, batch: JsonObject) -> list[JsonObject]:
        job_ids = self._unique_strings(batch.get("attribution_job_ids") or [])
        if not job_ids:
            for feedback_case_id in batch.get("feedback_case_ids") or []:
                feedback_case = self.find_case(str(feedback_case_id))
                job_id = self._latest((feedback_case or {}).get("attribution_job_ids"))
                if job_id:
                    job_ids.append(job_id)
        outputs: list[JsonObject] = []
        for job_id in job_ids:
            output = self.get_job_output(job_id, "attribution")
            if output:
                outputs.append({**output, "_job_id": job_id})
        return outputs

    def _assert_batch_plan_can_regenerate(self, batch: JsonObject) -> None:
        plan = batch.get("optimization_plan") if isinstance(batch.get("optimization_plan"), dict) else None
        plan_status = self._string((plan or {}).get("status"))
        if (
            plan_status == "approved"
            or batch.get("optimization_task_id")
            or batch.get("execution_job_id")
            or batch.get("execution_apply_result")
        ):
            raise ConflictError("当前优化方案已执行或进入执行链路，不能原地重新生成；请基于反馈信息创建新批次。")
