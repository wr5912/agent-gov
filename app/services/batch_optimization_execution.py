from __future__ import annotations

import uuid
from typing import cast

from pydantic import BaseModel

from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.errors import BusinessRuleViolation, ConflictError, MainWorkspaceDirtyError, NotFoundError
from app.runtime.feedback_batch_execution_request_schemas import (
    FeedbackOptimizationBatchExecuteAllRequest,
    FeedbackOptimizationBatchExecutionRollbackRequest,
)
from app.runtime.json_types import JsonObject
from app.runtime.records.batch_execution_records import (
    FeedbackBatchExecutionErrorRecord,
    FeedbackBatchExecutionRollbackRecord,
    FeedbackBatchExecutionRunRecord,
    FeedbackBatchExecutionTaskResultRecord,
)
from app.runtime.response_schemas.agent_job_response_schemas import AgentJobResponse
from app.runtime.response_schemas.feedback_workflow_response_schemas import (
    FeedbackOptimizationBatchExecuteAllResponse,
    FeedbackOptimizationBatchExecutionRollbackResponse,
)
from app.runtime.runtime_db import utc_now
from app.runtime.stores.feedback_execution_store import EXECUTION_JOB_ACTIONABILITIES
from app.runtime.stores.feedback_store import FeedbackStore
from app.services.execution_application import BatchWorkspaceApplyResult, BatchWorkspaceExecutionItem, ExecutionApplicationService


class WorkspaceExecutionContext(BaseModel):
    plan_task_id: str
    optimization_task_id: str
    execution_job_id: str
    execution_job: JsonObject
    planned_diff: JsonObject | None = None


class BatchOptimizationExecutionService:
    """Runs all actionable tasks in one optimization batch from one UI action."""

    def __init__(
        self,
        *,
        feedback_store: FeedbackStore,
        runtime: ClaudeRuntime,
        execution_application: ExecutionApplicationService,
    ) -> None:
        self.feedback_store = feedback_store
        self.runtime = runtime
        self.execution_application = execution_application

    async def execute_all(
        self,
        batch_id: str,
        request: FeedbackOptimizationBatchExecuteAllRequest,
    ) -> FeedbackOptimizationBatchExecuteAllResponse:
        batch, tasks = self._batch_tasks(batch_id)
        self._assert_no_active_run(batch_id)
        latest = self.feedback_store.latest_batch_execution_run(batch_id)
        if latest and self._latest_run_covers_current_tasks(latest, tasks):
            return FeedbackOptimizationBatchExecuteAllResponse(batch=batch, execution_run=latest.to_payload())
        self._prevalidate_tasks(tasks, request)
        self._assert_main_workspace_clean(batch_id)
        run = self._new_run(batch_id, request)
        self.feedback_store.record_batch_execution_run(run, batch_status="execution_planning")
        try:
            run = await self._execute_all_tasks(run, tasks, request)
        except Exception as exc:
            run = self._failed_run(run, exc)
            self.feedback_store.record_batch_execution_run(run, batch_status="execution_failed")
            raise
        batch_status = "applied_pending_regression" if run.applied_agent_version_id else "completed"
        persisted = self.feedback_store.record_batch_execution_run(run, batch_status=batch_status) or run
        return FeedbackOptimizationBatchExecuteAllResponse(
            batch=self.feedback_store.find_optimization_batch(batch_id),
            execution_run=persisted.to_payload(),
        )

    def rollback(
        self,
        batch_id: str,
        execution_run_id: str,
        request: FeedbackOptimizationBatchExecutionRollbackRequest,
    ) -> FeedbackOptimizationBatchExecutionRollbackResponse:
        run = self.feedback_store.find_batch_execution_run(batch_id, execution_run_id)
        if not run:
            raise NotFoundError("Batch execution run not found")
        if run.status == "rolled_back":
            return FeedbackOptimizationBatchExecutionRollbackResponse(
                batch=self.feedback_store.find_optimization_batch(batch_id), execution_run=run.to_payload()
            )
        target_version_id = run.pre_execution_agent_version_id
        if not target_version_id:
            raise ConflictError("Batch execution run has no pre-execution Agent version")
        try:
            restore_result = self._batch_version_store(batch_id).restore_version(
                target_version_id,
                note=request.note or f"回滚批次执行 {execution_run_id} 到应用前版本。",
            )
        except Exception as exc:
            failed = self._rollback_failed_run(run, exc, target_version_id)
            self.feedback_store.record_batch_execution_run(failed, batch_status="execution_failed")
            raise ConflictError(f"Batch execution rollback failed: {exc}") from exc
        rolled_back = self._rolled_back_run(run, target_version_id, restore_result or {})
        reset_task_ids = [
            result.optimization_task_id
            for result in run.task_results
            if result.execution_kind == "workspace_execution" and result.optimization_task_id and result.status == "completed"
        ]
        persisted = self.feedback_store.record_batch_execution_run_rollback(rolled_back, task_ids_to_reset=reset_task_ids) or rolled_back
        return FeedbackOptimizationBatchExecutionRollbackResponse(
            batch=self.feedback_store.find_optimization_batch(batch_id),
            execution_run=persisted.to_payload(),
        )

    async def _execute_all_tasks(
        self,
        run: FeedbackBatchExecutionRunRecord,
        tasks: list[JsonObject],
        request: FeedbackOptimizationBatchExecuteAllRequest,
    ) -> FeedbackBatchExecutionRunRecord:
        workspace_contexts: list[WorkspaceExecutionContext] = []
        results: list[FeedbackBatchExecutionTaskResultRecord] = []
        for task in tasks:
            if task.get("execution_kind") != "workspace_execution":
                continue
            context = await self._run_workspace_task(run.batch_id, task, request.force)
            workspace_contexts.append(context)
            results.append(self._workspace_task_result(task, context))
        apply_result = self._apply_workspace_contexts(run.batch_id, workspace_contexts, request.note)
        results = self._attach_applied_version(results, apply_result)
        results.extend(self._execute_external_tasks(run.batch_id, tasks, request))
        status = self._run_status_from_results(results)
        warnings = self._run_warnings(results)
        return self._completed_run(run, results, apply_result, status=status, warnings=warnings)

    async def _run_workspace_task(
        self,
        batch_id: str,
        plan_task: JsonObject,
        force: bool,
    ) -> WorkspaceExecutionContext:
        plan_task_id = str(plan_task["plan_task_id"])
        prepared = self.feedback_store.prepare_batch_plan_task_execution(
            batch_id,
            plan_task_id,
            comment=f"一键执行优化批次 {batch_id} 的任务 {plan_task_id}",
        )
        if not prepared:
            raise NotFoundError("Optimization plan task not found")
        task = prepared["optimization_task"]
        blocker = self.feedback_store.execution_job_queue_blocker(str(task["optimization_task_id"]))
        if blocker:
            raise ConflictError(blocker)
        job = await self.runtime.run_execution_job(str(task["optimization_task_id"]), force=force)
        job_payload = self._agent_job_payload(job)
        if not job_payload:
            raise ConflictError("Execution optimizer could not be queued")
        self._assert_execution_job_ready(job_payload)
        plan = cast(JsonObject, job_payload.get("validated_output_json") or {})
        return WorkspaceExecutionContext(
            plan_task_id=plan_task_id,
            optimization_task_id=str(task["optimization_task_id"]),
            execution_job_id=str(job_payload.get("execution_job_id") or job_payload.get("job_id")),
            execution_job=job_payload,
            planned_diff=plan.get("planned_diff") if isinstance(plan.get("planned_diff"), dict) else None,
        )

    def _apply_workspace_contexts(
        self,
        batch_id: str,
        contexts: list[WorkspaceExecutionContext],
        note: str | None,
    ) -> BatchWorkspaceApplyResult | None:
        if not contexts:
            return None
        return self.execution_application.apply_ready_batch_execution_jobs(
            batch_id,
            [
                BatchWorkspaceExecutionItem(
                    plan_task_id=context.plan_task_id,
                    optimization_task_id=context.optimization_task_id,
                    execution_job_id=context.execution_job_id,
                )
                for context in contexts
            ],
            note=note or f"一键执行优化批次 {batch_id}。",
        )

    def _execute_external_tasks(
        self,
        batch_id: str,
        tasks: list[JsonObject],
        request: FeedbackOptimizationBatchExecuteAllRequest,
    ) -> list[FeedbackBatchExecutionTaskResultRecord]:
        results: list[FeedbackBatchExecutionTaskResultRecord] = []
        for task in tasks:
            if str(task.get("execution_kind") or "") == "external_webhook":
                alias = request.webhook_alias_by_task_id[str(task["plan_task_id"])]
                results.append(self._execute_external_task(batch_id, task, alias))
        return results

    def _execute_external_task(self, batch_id: str, plan_task: JsonObject, alias: str) -> FeedbackBatchExecutionTaskResultRecord:
        started_at = utc_now()
        result = self.feedback_store.notify_batch_plan_task_external(batch_id, str(plan_task["plan_task_id"]), webhook_alias=alias)
        if not result:
            raise NotFoundError("Optimization plan task not found")
        external_item = result.get("external_item") if isinstance(result.get("external_item"), dict) else {}
        status = "completed" if external_item.get("status") == "notified" else "failed"
        error = None
        if status == "failed":
            error = self._error_record(
                "EXTERNAL_NOTIFICATION_FAILED", str((external_item.get("latest_notification") or {}).get("error") or "External notification failed")
            )
        return FeedbackBatchExecutionTaskResultRecord(
            plan_task_id=str(plan_task["plan_task_id"]),
            execution_kind="external_webhook",
            status=status,
            started_at=started_at,
            completed_at=utc_now() if status == "completed" else None,
            external_item_id=str(external_item.get("external_item_id") or ""),
            webhook_alias=alias,
            rollback_supported=False,
            rollback_note="外部 webhook 已产生系统外副作用，回滚只恢复 Agent 工作区和本地任务投影，不能撤回外部通知。",
            error_json=error,
        )

    def _batch_tasks(self, batch_id: str) -> tuple[JsonObject, list[JsonObject]]:
        batch = self.feedback_store.find_optimization_batch(batch_id)
        if not batch:
            raise NotFoundError("Feedback optimization batch not found")
        plan = batch.get("optimization_plan") if isinstance(batch.get("optimization_plan"), dict) else None
        if not plan:
            raise BusinessRuleViolation("Optimization plan has not been generated")
        tasks = [dict(item) for item in plan.get("tasks") or [] if isinstance(item, dict)]
        executable = [item for item in tasks if item.get("execution_kind") in {"workspace_execution", "external_webhook"}]
        if not executable:
            raise ConflictError("Optimization plan has no executable tasks")
        return batch, executable

    def _prevalidate_tasks(self, tasks: list[JsonObject], request: FeedbackOptimizationBatchExecuteAllRequest) -> None:
        for task in tasks:
            kind = str(task.get("execution_kind") or "")
            if kind == "workspace_execution":
                self._prevalidate_workspace_task(task)
            elif kind == "external_webhook" and not request.webhook_alias_by_task_id.get(str(task.get("plan_task_id"))):
                raise BusinessRuleViolation(f"Webhook alias is required for external task {task.get('plan_task_id')}")

    def _prevalidate_workspace_task(self, task: JsonObject) -> None:
        target_path = str(task.get("target_path") or "").strip()
        if not target_path or not self.feedback_store.target_allowed(target_path):
            raise ConflictError(f"Optimization task target_path is not executable: {target_path or '-'}")
        actionability = str(task.get("actionability") or "")
        if actionability not in EXECUTION_JOB_ACTIONABILITIES:
            raise ConflictError(f"Optimization task actionability is not executable: {actionability or '-'}")
        context = task.get("task_context") if isinstance(task.get("task_context"), dict) else {}
        target_file = str(context.get("target_file") or "").strip()
        if target_file and target_path != target_file:
            raise ConflictError(f"Optimization task target_path '{target_path}' does not match task_context.target_file '{target_file}'")

    def _assert_no_active_run(self, batch_id: str) -> None:
        latest = self.feedback_store.latest_batch_execution_run(batch_id)
        if latest and latest.status == "running":
            raise ConflictError(f"Batch execution is already running: {latest.execution_run_id}")

    def _batch_version_store(self, batch_id: str):
        """按 batch 归属的 agent_id 路由到对应版本库（缺陷②：批次执行不再恒走主库）。"""
        batch = self.feedback_store.find_optimization_batch(batch_id) or {}
        return self.execution_application._version_store(batch.get("agent_id"))

    def _assert_main_workspace_clean(self, batch_id: str) -> None:
        repository_status = self._batch_version_store(batch_id).repository_status()
        if repository_status.get("dirty"):
            raise MainWorkspaceDirtyError(repository_status)

    def _latest_run_covers_current_tasks(self, run: FeedbackBatchExecutionRunRecord, tasks: list[JsonObject]) -> bool:
        if run.status not in {"completed", "partial_failed"}:
            return False
        current_ids = {str(item.get("plan_task_id") or "") for item in tasks}
        result_ids = {item.plan_task_id for item in run.task_results}
        return current_ids and current_ids.issubset(result_ids)

    def _new_run(self, batch_id: str, request: FeedbackOptimizationBatchExecuteAllRequest) -> FeedbackBatchExecutionRunRecord:
        now = utc_now()
        store = self._batch_version_store(batch_id)
        current_version_id = store.current_version_id() or ""
        pre_version = store.version_summary(
            current_version_id,
            reason="batch_execution_base",
            note=f"批次 {batch_id} 一键执行前版本。",
        )
        return FeedbackBatchExecutionRunRecord(
            execution_run_id=f"fbx-{uuid.uuid4()}",
            batch_id=batch_id,
            created_at=now,
            started_at=now,
            status="running",
            force=request.force,
            note=request.note,
            pre_execution_agent_version_id=current_version_id or None,
            pre_execution_agent_version=pre_version,
        )

    def _workspace_task_result(
        self,
        plan_task: JsonObject,
        context: WorkspaceExecutionContext,
    ) -> FeedbackBatchExecutionTaskResultRecord:
        return FeedbackBatchExecutionTaskResultRecord(
            plan_task_id=str(plan_task["plan_task_id"]),
            execution_kind="workspace_execution",
            status="completed",
            started_at=utc_now(),
            completed_at=utc_now(),
            optimization_task_id=context.optimization_task_id,
            execution_job_id=context.execution_job_id,
            execution_job=context.execution_job,
            planned_diff=context.planned_diff,
            summary="执行方案已生成并纳入批次级应用。",
        )

    def _attach_applied_version(
        self,
        results: list[FeedbackBatchExecutionTaskResultRecord],
        apply_result: BatchWorkspaceApplyResult | None,
    ) -> list[FeedbackBatchExecutionTaskResultRecord]:
        if not apply_result:
            return results
        version_id = str(apply_result.applied_agent_version.get("agent_version_id") or "")
        return [result.model_copy(update={"applied_agent_version_id": version_id}) for result in results]

    def _completed_run(
        self,
        run: FeedbackBatchExecutionRunRecord,
        task_results: list[FeedbackBatchExecutionTaskResultRecord],
        apply_result: BatchWorkspaceApplyResult | None,
        *,
        status: str,
        warnings: list[str],
    ) -> FeedbackBatchExecutionRunRecord:
        fields: JsonObject = {
            "status": status,
            "completed_at": utc_now(),
            "task_results": [item.model_dump(mode="json", exclude_none=True) for item in task_results],
            "warnings": warnings,
        }
        if apply_result:
            fields.update(
                {
                    "pre_execution_agent_version_id": apply_result.pre_execution_agent_version.get("agent_version_id"),
                    "pre_execution_agent_version": apply_result.pre_execution_agent_version,
                    "applied_agent_version_id": apply_result.applied_agent_version.get("agent_version_id"),
                    "applied_agent_version": apply_result.applied_agent_version,
                    "applied_diff": apply_result.applied_diff,
                    "change_set_id": apply_result.change_set.get("change_set_id"),
                    "change_set": apply_result.change_set,
                    "candidate_commit_sha": apply_result.candidate_commit_sha,
                }
            )
        return FeedbackBatchExecutionRunRecord.model_validate({**run.to_payload(), **fields})

    def _failed_run(self, run: FeedbackBatchExecutionRunRecord, exc: Exception) -> FeedbackBatchExecutionRunRecord:
        return FeedbackBatchExecutionRunRecord.model_validate(
            {
                **run.to_payload(),
                "status": "failed",
                "completed_at": utc_now(),
                "error_json": self._error_record("BATCH_EXECUTION_FAILED", str(exc)).model_dump(mode="json"),
            }
        )

    def _rolled_back_run(
        self,
        run: FeedbackBatchExecutionRunRecord,
        target_version_id: str,
        restore_result: JsonObject,
    ) -> FeedbackBatchExecutionRunRecord:
        rollback = FeedbackBatchExecutionRollbackRecord(
            restored_at=utc_now(),
            status="restored",
            target_agent_version_id=target_version_id,
            restore_result=restore_result,
        )
        return FeedbackBatchExecutionRunRecord.model_validate(
            {**run.to_payload(), "status": "rolled_back", "completed_at": utc_now(), "rollback_result": rollback.model_dump(mode="json")}
        )

    def _rollback_failed_run(
        self,
        run: FeedbackBatchExecutionRunRecord,
        exc: Exception,
        target_version_id: str,
    ) -> FeedbackBatchExecutionRunRecord:
        rollback = FeedbackBatchExecutionRollbackRecord(
            restored_at=utc_now(),
            status="failed",
            target_agent_version_id=target_version_id,
            error_json=self._error_record("BATCH_EXECUTION_ROLLBACK_FAILED", str(exc)),
        )
        return FeedbackBatchExecutionRunRecord.model_validate(
            {**run.to_payload(), "status": "rollback_failed", "completed_at": utc_now(), "rollback_result": rollback.model_dump(mode="json")}
        )

    def _run_status_from_results(self, results: list[FeedbackBatchExecutionTaskResultRecord]) -> str:
        failed = [item for item in results if item.status == "failed"]
        if not failed:
            return "completed"
        return "failed" if len(failed) == len(results) else "partial_failed"

    def _run_warnings(self, results: list[FeedbackBatchExecutionTaskResultRecord]) -> list[str]:
        warnings = [item.rollback_note for item in results if item.rollback_note]
        if any(item.status == "failed" for item in results):
            warnings.append("部分任务执行失败；已完成的 workspace 变更仍可通过执行记录回滚。")
        return [str(item) for item in warnings if item]

    def _assert_execution_job_ready(self, job: JsonObject) -> None:
        plan = job.get("validated_output_json") if isinstance(job.get("validated_output_json"), dict) else {}
        if job.get("status") != "completed" or plan.get("status") != "ready":
            error = job.get("error_json") if isinstance(job.get("error_json"), dict) else {}
            detail = str(error.get("message") or plan.get("no_action_reason") or "Execution job is not ready")
            raise ConflictError(detail)

    @staticmethod
    def _agent_job_payload(job: AgentJobResponse | JsonObject | None) -> JsonObject | None:
        if job is None:
            return None
        if isinstance(job, AgentJobResponse):
            return cast(JsonObject, job.model_dump(mode="json"))
        return job

    @staticmethod
    def _error_record(error_code: str, message: str) -> FeedbackBatchExecutionErrorRecord:
        return FeedbackBatchExecutionErrorRecord(error_code=error_code, message=message, created_at=utc_now())
