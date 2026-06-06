from __future__ import annotations

from typing import Any, Optional

from ..json_types import JsonObject
from ..records.batch_execution_records import FeedbackBatchExecutionRunRecord
from ..runtime_db import FeedbackOptimizationBatchModel, OptimizationTaskModel

MAX_BATCH_EXECUTION_RUNS = 20


class FeedbackBatchExecutionStoreMixin:
    """Persist one-click batch execution runs on optimization batches."""

    def latest_batch_execution_run(self, batch_id: str) -> Optional[FeedbackBatchExecutionRunRecord]:
        batch = self.find_optimization_batch(batch_id)
        run = batch.get("latest_execution_run") if isinstance((batch or {}).get("latest_execution_run"), dict) else None
        return FeedbackBatchExecutionRunRecord.model_validate(run) if run else None

    def find_batch_execution_run(self, batch_id: str, execution_run_id: str) -> Optional[FeedbackBatchExecutionRunRecord]:
        batch = self.find_optimization_batch(batch_id)
        if not batch:
            return None
        for item in batch.get("execution_runs") or []:
            if isinstance(item, dict) and self._string(item.get("execution_run_id")) == execution_run_id:
                return FeedbackBatchExecutionRunRecord.model_validate(item)
        return None

    def record_batch_execution_run(
        self,
        run: FeedbackBatchExecutionRunRecord,
        *,
        batch_status: Optional[str] = None,
    ) -> Optional[FeedbackBatchExecutionRunRecord]:
        with self.Session.begin() as db:
            row = db.get(FeedbackOptimizationBatchModel, run.batch_id)
            if not row:
                return None
            payload = self._batch_payload_snapshot(row)
            fields = self._batch_execution_run_fields(payload, run)
            self._update_batch_row(db, run.batch_id, status=batch_status or str(payload.get("status") or "pending_execution"), fields=fields)
        return self.find_batch_execution_run(run.batch_id, run.execution_run_id)

    def record_batch_execution_run_rollback(
        self,
        run: FeedbackBatchExecutionRunRecord,
        *,
        task_ids_to_reset: list[str],
    ) -> Optional[FeedbackBatchExecutionRunRecord]:
        with self.Session.begin() as db:
            row = db.get(FeedbackOptimizationBatchModel, run.batch_id)
            if not row:
                return None
            payload = self._batch_payload_snapshot(row)
            fields = self._batch_execution_run_fields(payload, run)
            fields["optimization_plan"] = self._rollback_plan_task_payload(payload, run)
            fields.update(
                {
                    "applied_agent_version_id": None,
                    "execution_apply_result": None,
                }
            )
            for task_id in self._unique_strings(task_ids_to_reset):
                self._reset_batch_execution_task_row(db, task_id)
            self._update_batch_row(db, run.batch_id, status="pending_execution", fields=fields)
        return self.find_batch_execution_run(run.batch_id, run.execution_run_id)

    def _batch_execution_run_fields(self, batch: JsonObject, run: FeedbackBatchExecutionRunRecord) -> JsonObject:
        run_payload = run.to_payload()
        runs: list[JsonObject] = []
        replaced = False
        for item in batch.get("execution_runs") or []:
            if not isinstance(item, dict):
                continue
            if self._string(item.get("execution_run_id")) == run.execution_run_id:
                runs.append(run_payload)
                replaced = True
            else:
                runs.append(item)
        if not replaced:
            runs.append(run_payload)
        return {
            "execution_runs": runs[-MAX_BATCH_EXECUTION_RUNS:],
            "latest_execution_run": run_payload,
        }

    def _rollback_plan_task_payload(self, batch: JsonObject, run: FeedbackBatchExecutionRunRecord) -> JsonObject | None:
        plan = batch.get("optimization_plan") if isinstance(batch.get("optimization_plan"), dict) else None
        if not plan:
            return None
        reset_task_ids = {result.plan_task_id for result in run.task_results if result.execution_kind == "workspace_execution" and result.status == "completed"}
        tasks = []
        for item in plan.get("tasks") or []:
            if not isinstance(item, dict):
                continue
            if self._string(item.get("plan_task_id")) not in reset_task_ids:
                tasks.append(item)
                continue
            tasks.append(
                {
                    **item,
                    "status": "pending_execution",
                    "execution_job_id": None,
                    "latest_execution_job": None,
                    "applied_agent_version_id": None,
                }
            )
        return {**plan, "tasks": tasks, "task_summary": self._plan_task_summary(tasks)}

    def _reset_batch_execution_task_row(self, db: Any, task_id: str) -> None:
        row = db.get(OptimizationTaskModel, task_id, with_for_update=True)
        if not row:
            return
        self._update_task_payload_row(
            db,
            task_id,
            status="pending_execution",
            fields={
                "latest_execution_job_id": None,
                "latest_execution_job": None,
                "pre_execution_agent_version_id": None,
                "pre_execution_agent_version": None,
                "latest_change_set_id": None,
                "latest_change_set": None,
                "candidate_commit_sha": None,
                "applied_at": None,
                "applied_agent_version_id": None,
                "applied_agent_version": None,
                "application_note": None,
                "latest_execution_application_id": None,
                "latest_execution_application": None,
            },
        )
