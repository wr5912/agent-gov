from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from threading import Lock
from typing import cast

from pydantic import BaseModel

from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.errors import FeedbackStoreError, MainWorkspaceDirtyError
from app.runtime.json_types import JsonObject
from app.runtime.records.feedback_compensation_records import ExecutionCompensationRecord
from app.runtime.response_schemas.agent_job_response_schemas import AgentJobResponse
from app.runtime.response_schemas.feedback_workflow_response_schemas import (
    ExecutionCompensationResponse,
    OptimizationExecutionApplyResponse,
    OptimizationTaskResponse,
)
from app.runtime.runtime_db import utc_now
from app.runtime.settings import AppSettings
from app.runtime.stores.feedback_store import FeedbackStore
from app.services.agent_governance import AgentGovernanceService

logger = logging.getLogger(__name__)


class ExecutionApplicationError(FeedbackStoreError):
    """Route-safe error raised by execution application services."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        if status_code == 404:
            self.error_code = "NOT_FOUND"
        elif status_code == 409:
            self.error_code = "CONFLICT"
        else:
            self.error_code = "EXECUTION_APPLICATION_ERROR"


class ExecutionRunApplyResult(BaseModel):
    execution_job: AgentJobResponse | None = None
    apply_result: OptimizationExecutionApplyResponse | None = None
    optimization_task: OptimizationTaskResponse | None = None


class BatchWorkspaceExecutionItem(BaseModel):
    plan_task_id: str
    optimization_task_id: str
    execution_job_id: str


class BatchWorkspaceApplyResult(BaseModel):
    pre_execution_agent_version: JsonObject
    applied_agent_version: JsonObject
    applied_diff: JsonObject | None = None
    change_set: JsonObject
    candidate_commit_sha: str
    applications: list[JsonObject] = []


class ExecutionApplicationService:
    """Coordinates execution-plan application across workspace files and store state."""

    def __init__(
        self,
        *,
        settings: AppSettings,
        feedback_store: FeedbackStore,
        agent_version_store: GitAgentVersionStore,
        agent_governance: AgentGovernanceService,
        max_write_bytes: int = 500_000,
    ) -> None:
        self.settings = settings
        self.feedback_store = feedback_store
        self.agent_version_store = agent_version_store
        self.agent_governance = agent_governance
        self.max_write_bytes = max_write_bytes
        self._apply_lock = Lock()

    async def run_and_apply_execution_job(
        self,
        task_id: str,
        *,
        run_execution_job: Callable[..., Awaitable[AgentJobResponse | JsonObject | None]],
        force: bool,
        note: str,
    ) -> ExecutionRunApplyResult:
        execution_job = await run_execution_job(task_id, force=force)
        execution_job_payload = self._agent_job_payload(execution_job)
        apply_result = None
        if execution_job_payload and self._execution_plan_ready(execution_job_payload):
            apply_result = self.apply_ready_execution_job(
                task_id,
                str(execution_job_payload.get("execution_job_id") or execution_job_payload["job_id"]),
                note=note,
            )
        task = self.feedback_store.find_task(task_id)
        return ExecutionRunApplyResult(
            execution_job=AgentJobResponse.model_validate(execution_job_payload) if execution_job_payload else None,
            apply_result=apply_result,
            optimization_task=OptimizationTaskResponse.model_validate(task) if task else None,
        )

    def apply_ready_execution_job(
        self,
        task_id: str,
        execution_job_id: str,
        *,
        note: str | None = None,
    ) -> OptimizationExecutionApplyResponse:
        with self._apply_lock:
            return self._apply_ready_execution_job_locked(task_id, execution_job_id, note=note)

    def apply_ready_batch_execution_jobs(
        self,
        batch_id: str,
        items: list[BatchWorkspaceExecutionItem],
        *,
        note: str | None = None,
    ) -> BatchWorkspaceApplyResult | None:
        if not items:
            return None
        with self._apply_lock:
            return self._apply_ready_batch_execution_jobs_locked(batch_id, items, note=note)

    def mark_task_applied_manually(self, task_id: str, *, note: str | None = None) -> OptimizationTaskResponse:
        with self._apply_lock:
            task = self.feedback_store.ensure_task_can_mark_applied_manually_record(task_id)
            if not task:
                raise ExecutionApplicationError(404, "Optimization task not found")
            if task.applied_agent_version_id:
                return OptimizationTaskResponse.model_validate(task.to_payload())
            change_set = self.agent_governance.create_change_set(
                optimization_task_id=task_id,
                title=f"Manual application for {task_id}",
                note=note or f"优化任务 {task_id} 已人工确认，等待候选提交。",
            )
            version = self.agent_version_store.version_summary(
                str(change_set["base_commit_sha"]),
                reason="manual_candidate_pending",
                note=note,
            )
            updated = self.feedback_store.update_task_status(
                task_id,
                status="applied_pending_regression",
                fields={
                    "applied_at": utc_now(),
                    "applied_agent_version_id": version.get("agent_version_id"),
                    "applied_agent_version": version,
                    "application_note": note,
                    "latest_change_set_id": change_set.get("change_set_id"),
                    "latest_change_set": change_set,
                },
            )
            if not updated:
                raise ExecutionApplicationError(404, "Optimization task not found")
            return OptimizationTaskResponse.model_validate(updated)

    def _apply_ready_execution_job_locked(
        self,
        task_id: str,
        execution_job_id: str,
        *,
        note: str | None = None,
    ) -> OptimizationExecutionApplyResponse:
        _task, _job, plan, baseline_version_id = self._ready_execution_context(task_id, execution_job_id)
        change_set, pre_version, worktree_path = self._prepare_candidate_change_set(
            task_id=task_id,
            execution_job_id=execution_job_id,
            baseline_version_id=baseline_version_id,
            note=note,
        )
        self._apply_operations_to_candidate(
            execution_job_id=execution_job_id,
            task_id=task_id,
            plan=plan,
            worktree_path=worktree_path,
            pre_version=pre_version,
        )
        application, applied_diff = self._commit_candidate_and_record_application(
            task_id=task_id,
            execution_job_id=execution_job_id,
            change_set=change_set,
            worktree_path=worktree_path,
            pre_version=pre_version,
            note=note,
        )
        execution_job = self.feedback_store.get_execution_job(execution_job_id)
        optimization_task = self.feedback_store.find_task(task_id)
        if not execution_job or not optimization_task:
            raise ExecutionApplicationError(404, "Execution application result not found")
        return OptimizationExecutionApplyResponse(
            execution_job=execution_job,
            execution_application=application,
            optimization_task=optimization_task,
            applied_diff=applied_diff,
        )

    def _apply_ready_batch_execution_jobs_locked(
        self,
        batch_id: str,
        items: list[BatchWorkspaceExecutionItem],
        *,
        note: str | None,
    ) -> BatchWorkspaceApplyResult:
        contexts = self._ready_batch_execution_contexts(items)
        baseline_version_id = self._batch_execution_baseline(contexts)
        change_set, pre_version, worktree_path = self._prepare_batch_candidate_change_set(
            batch_id=batch_id,
            baseline_version_id=baseline_version_id,
            note=note,
        )
        for task, job, plan, _baseline in contexts:
            self._apply_operations_to_candidate(
                execution_job_id=str(job["execution_job_id"]),
                task_id=str(task["optimization_task_id"]),
                plan=plan,
                worktree_path=worktree_path,
                pre_version=pre_version,
            )
        return self._commit_batch_candidate_and_record_applications(
            contexts=contexts,
            change_set=change_set,
            worktree_path=worktree_path,
            pre_version=pre_version,
            note=note,
        )

    def _ready_batch_execution_contexts(
        self,
        items: list[BatchWorkspaceExecutionItem],
    ) -> list[tuple[JsonObject, JsonObject, JsonObject, str]]:
        contexts: list[tuple[JsonObject, JsonObject, JsonObject, str]] = []
        seen_jobs: set[str] = set()
        for item in items:
            if item.execution_job_id in seen_jobs:
                raise ExecutionApplicationError(409, f"Duplicate execution job in batch apply: {item.execution_job_id}")
            seen_jobs.add(item.execution_job_id)
            contexts.append(self._ready_execution_context(item.optimization_task_id, item.execution_job_id))
        return contexts

    def _batch_execution_baseline(self, contexts: list[tuple[JsonObject, JsonObject, JsonObject, str]]) -> str:
        repository_status = self.agent_version_store.repository_status()
        if repository_status.get("dirty"):
            raise MainWorkspaceDirtyError(repository_status)
        current_version_id = self.agent_version_store.current_version_id()
        baselines = {baseline for _task, _job, _plan, baseline in contexts if baseline}
        if len(baselines) > 1:
            raise ExecutionApplicationError(409, "Batch execution jobs were generated from different Agent baselines")
        baseline = next(iter(baselines), "") or current_version_id or ""
        if baseline and current_version_id != baseline:
            raise ExecutionApplicationError(409, "Current Agent version differs from execution baseline")
        return baseline

    def _prepare_batch_candidate_change_set(
        self,
        *,
        batch_id: str,
        baseline_version_id: str,
        note: str | None,
    ) -> tuple[JsonObject, JsonObject, Path]:
        change_set = self.agent_governance.create_change_set(
            base_commit_sha=baseline_version_id or None,
            title=f"Batch execution application for {batch_id}",
            note=note or f"一键执行优化批次 {batch_id}。",
        )
        if baseline_version_id and str(change_set.get("base_commit_sha")) != baseline_version_id:
            raise ExecutionApplicationError(409, "Agent change set base differs from execution baseline")
        pre_version = self.agent_version_store.version_summary(
            str(change_set["base_commit_sha"]),
            reason="change_set_base",
            note=note or f"一键执行优化批次 {batch_id} 的候选基线。",
        )
        return change_set, pre_version, self.agent_governance.change_set_worktree_path(change_set)

    def _commit_batch_candidate_and_record_applications(
        self,
        *,
        contexts: list[tuple[JsonObject, JsonObject, JsonObject, str]],
        change_set: JsonObject,
        worktree_path: Path,
        pre_version: JsonObject,
        note: str | None,
    ) -> BatchWorkspaceApplyResult:
        try:
            candidate_commit = self.agent_version_store.commit_worktree(
                worktree_path,
                message=note or f"Apply batch execution plan {change_set['change_set_id']}",
            )
            applied_version = self.agent_version_store.version_summary(
                candidate_commit,
                reason="batch_candidate_change_set",
                note=note or "execution-optimizer 一键执行批次任务的候选提交。",
            )
            applied_diff = self.agent_version_store.diff_versions(str(pre_version["agent_version_id"]), str(applied_version["agent_version_id"]))
            change_set = self.agent_governance.mark_candidate_committed(
                str(change_set["change_set_id"]),
                candidate_commit_sha=candidate_commit,
                execution_job_id=None,
                note=note,
            )
            applications = self._record_batch_execution_applications(contexts, pre_version, applied_version, applied_diff, change_set, candidate_commit, note)
        except Exception as exc:
            detail = self._compensate_batch_post_write_failure(contexts, pre_version, exc)
            raise ExecutionApplicationError(409, detail) from exc
        return BatchWorkspaceApplyResult(
            pre_execution_agent_version=pre_version,
            applied_agent_version=applied_version,
            applied_diff=applied_diff,
            change_set=change_set,
            candidate_commit_sha=candidate_commit,
            applications=applications,
        )

    def _record_batch_execution_applications(
        self,
        contexts: list[tuple[JsonObject, JsonObject, JsonObject, str]],
        pre_version: JsonObject,
        applied_version: JsonObject,
        applied_diff: JsonObject | None,
        change_set: JsonObject,
        candidate_commit: str,
        note: str | None,
    ) -> list[JsonObject]:
        applications: list[JsonObject] = []
        for task, job, _plan, _baseline in contexts:
            application = self.feedback_store.record_execution_application_applied(
                str(job["execution_job_id"]),
                pre_execution_version=pre_version,
                applied_agent_version=applied_version,
                applied_diff=applied_diff,
                change_set=change_set,
                candidate_commit_sha=candidate_commit,
                note=note or f"一键执行优化批次任务 {task['optimization_task_id']}。",
            )
            if not application:
                raise ExecutionApplicationError(404, "Execution job not found")
            applications.append(application)
        return applications

    def _compensate_batch_post_write_failure(
        self,
        contexts: list[tuple[JsonObject, JsonObject, JsonObject, str]],
        pre_version: JsonObject,
        error: Exception,
    ) -> str:
        original_error = str(error)
        for task, job, _plan, _baseline in contexts:
            try:
                self.feedback_store.record_execution_application_failed(
                    str(job["execution_job_id"]),
                    optimization_task_id=str(task["optimization_task_id"]),
                    message=original_error,
                    pre_execution_version=pre_version,
                    status="failed",
                )
            except Exception:
                logger.exception("Failed to record batch execution application failure for job %s", job.get("execution_job_id"))
        return f"Batch execution apply state sync failed: {original_error}"

    def _ready_execution_context(
        self,
        task_id: str,
        execution_job_id: str,
    ) -> tuple[JsonObject, JsonObject, JsonObject, str]:
        task = self.feedback_store.find_task(task_id)
        if not task:
            raise ExecutionApplicationError(404, "Optimization task not found")
        if task.get("applied_agent_version_id"):
            raise ExecutionApplicationError(409, "Task is already applied")
        job = self.feedback_store.get_execution_job(execution_job_id)
        if not job or job.get("optimization_task_id") != task_id:
            raise ExecutionApplicationError(404, "Execution job not found")
        if not self._execution_plan_ready(job):
            raise ExecutionApplicationError(409, "Execution job is not ready")
        plan = job.get("validated_output_json")
        if not isinstance(plan, dict):
            raise ExecutionApplicationError(409, "Execution job has no validated plan")
        baseline_version_id = str(job.get("baseline_agent_version_id") or task.get("baseline_agent_version_id") or "")
        return task, job, plan, baseline_version_id

    def _prepare_candidate_change_set(
        self,
        *,
        task_id: str,
        execution_job_id: str,
        baseline_version_id: str,
        note: str | None,
    ) -> tuple[JsonObject, JsonObject, Path]:
        repository_status = self.agent_version_store.repository_status()
        if repository_status.get("dirty"):
            raise MainWorkspaceDirtyError(repository_status)
        current_version_id = self.agent_version_store.current_version_id()
        if baseline_version_id and current_version_id != baseline_version_id:
            raise ExecutionApplicationError(409, "Current Agent version differs from execution baseline")
        change_set = self.agent_governance.create_change_set(
            optimization_task_id=task_id,
            execution_job_id=execution_job_id,
            base_commit_sha=baseline_version_id or None,
            title=f"Execution application for {task_id}",
            note=note,
        )
        if baseline_version_id and str(change_set.get("base_commit_sha")) != baseline_version_id:
            raise ExecutionApplicationError(409, "Agent change set base differs from execution baseline")
        pre_version = self.agent_version_store.version_summary(
            str(change_set["base_commit_sha"]),
            reason="change_set_base",
            note=note or f"执行优化任务 {task_id} 的候选基线。",
        )
        worktree_path = self.agent_governance.change_set_worktree_path(change_set)
        return change_set, pre_version, worktree_path

    def _apply_operations_to_candidate(
        self,
        *,
        execution_job_id: str,
        task_id: str,
        plan: JsonObject,
        worktree_path: Path,
        pre_version: JsonObject,
    ) -> None:
        try:
            self.apply_execution_operations(plan.get("operations") or [], workspace_dir=worktree_path)
        except ExecutionApplicationError as exc:
            self.feedback_store.record_execution_application_failed(
                execution_job_id,
                optimization_task_id=task_id,
                message=str(exc),
                pre_execution_version=pre_version,
            )
            raise

    def _commit_candidate_and_record_application(
        self,
        *,
        task_id: str,
        execution_job_id: str,
        change_set: JsonObject,
        worktree_path: Path,
        pre_version: JsonObject,
        note: str | None,
    ) -> tuple[JsonObject, JsonObject | None]:
        try:
            candidate_commit = self.agent_version_store.commit_worktree(
                worktree_path,
                message=note or f"Apply execution plan {execution_job_id} for {task_id}",
            )
            applied_version = self.agent_version_store.version_summary(
                candidate_commit,
                reason="candidate_change_set",
                note=note or f"execution-optimizer 生成任务 {task_id} 的候选提交。",
            )
            applied_diff = self.agent_version_store.diff_versions(
                str(pre_version["agent_version_id"]),
                str(applied_version["agent_version_id"]),
            )
            change_set = self.agent_governance.mark_candidate_committed(
                str(change_set["change_set_id"]),
                candidate_commit_sha=candidate_commit,
                execution_job_id=execution_job_id,
                note=note,
            )
            execution_application = self.feedback_store.record_execution_application_applied(
                execution_job_id,
                pre_execution_version=pre_version,
                applied_agent_version=applied_version,
                applied_diff=applied_diff,
                change_set=change_set,
                candidate_commit_sha=candidate_commit,
                note=note,
            )
            if not execution_application:
                raise ExecutionApplicationError(404, "Execution job not found")
        except Exception as exc:
            detail = self._compensate_post_write_failure(
                task_id=task_id,
                execution_job_id=execution_job_id,
                pre_version=pre_version,
                error=exc,
            )
            raise ExecutionApplicationError(409, detail) from exc
        return execution_application, applied_diff

    def _compensate_post_write_failure(
        self,
        *,
        task_id: str,
        execution_job_id: str,
        pre_version: JsonObject,
        error: Exception,
    ) -> str:
        pre_version_id = str(pre_version.get("agent_version_id") or "")
        restore_error: str | None = None
        restore_status = "restored"
        try:
            restored = self.agent_version_store.restore_version(
                pre_version_id,
                note=f"执行优化任务 {task_id} 后续状态同步失败，自动恢复应用前快照。",
            )
            if not restored:
                restore_status = "restore_failed"
                restore_error = f"pre-execution version not found: {pre_version_id}"
        except Exception as exc:  # pragma: no cover - defensive path exercised by runtime only
            restore_status = "restore_failed"
            restore_error = str(exc)

        original_error = str(error)
        try:
            self.feedback_store.record_execution_compensation(
                optimization_task_id=task_id,
                execution_job_id=execution_job_id,
                pre_execution_agent_version_id=pre_version_id or None,
                restore_status=restore_status,
                original_error=original_error,
                restore_error=restore_error,
            )
        except Exception:
            logger.exception("Failed to record execution compensation for job %s", execution_job_id)

        if restore_error:
            detail = (
                "Execution apply changed workspace files but state sync failed; "
                f"automatic restore also failed: {restore_error}; original error: {original_error}"
            )
        else:
            detail = (
                "Execution apply state sync failed after workspace changes; "
                f"workspace was restored to pre-execution version {pre_version_id}; original error: {original_error}"
            )
        try:
            self.feedback_store.record_execution_application_failed(
                execution_job_id,
                optimization_task_id=task_id,
                message=detail,
                pre_execution_version=pre_version,
                status="pending_manual_recovery" if restore_error else "compensated",
            )
        except Exception:
            logger.exception("Failed to record execution application failure for job %s", execution_job_id)
        return detail

    def _execution_plan_ready(self, job: JsonObject) -> bool:
        plan = job.get("validated_output_json") if isinstance(job.get("validated_output_json"), dict) else {}
        return job.get("status") == "completed" and plan.get("status") == "ready"

    def restore_execution_compensation(self, compensation_id: str) -> ExecutionCompensationResponse:
        compensation = self.feedback_store.find_execution_compensation(compensation_id)
        if not compensation:
            raise ExecutionApplicationError(404, "Execution compensation not found")
        record = ExecutionCompensationRecord.model_validate(compensation)
        if record.status == "resolved":
            return ExecutionCompensationResponse.model_validate(record.to_payload())
        if record.status != "pending_manual_recovery":
            raise ExecutionApplicationError(409, "Execution compensation is not pending manual recovery")
        version_id = record.pre_execution_agent_version_id or ""
        if not version_id:
            raise ExecutionApplicationError(409, "Execution compensation has no pre-execution version")
        try:
            restore_result = self.agent_version_store.restore_version(
                version_id,
                note=f"人工恢复执行补偿记录 {compensation_id} 到应用前版本。",
            )
        except Exception as exc:
            self.feedback_store.mark_execution_compensation_restore_failed(compensation_id, str(exc))
            raise ExecutionApplicationError(409, f"Execution compensation restore failed: {exc}") from exc
        if not restore_result:
            message = f"pre-execution version not found: {version_id}"
            self.feedback_store.mark_execution_compensation_restore_failed(compensation_id, message)
            raise ExecutionApplicationError(409, f"Execution compensation restore failed: {message}")
        updated = self.feedback_store.mark_execution_compensation_resolved(
            compensation_id,
            restore_result=restore_result,
        )
        if not updated:
            raise ExecutionApplicationError(404, "Execution compensation not found")
        return ExecutionCompensationResponse.model_validate(updated)

    def apply_execution_operations(self, operations: list[object], *, workspace_dir: Path | None = None) -> None:
        if not operations:
            raise ExecutionApplicationError(409, "Execution plan has no operations")
        originals: dict[Path, bytes | None] = {}
        writes: list[tuple[Path, bytes]] = []
        for item in operations:
            if not isinstance(item, dict):
                raise ExecutionApplicationError(409, "Execution operation must be an object")
            op = str(item.get("operation") or "")
            target_path = str(item.get("path") or "")
            dest = self.safe_workspace_target(target_path, workspace_dir=workspace_dir)
            if not self.feedback_store.target_allowed(target_path):
                raise ExecutionApplicationError(409, f"Target path is not allowed: {target_path}")
            if op == "noop":
                continue
            if dest not in originals:
                originals[dest] = dest.read_bytes() if dest.exists() else None
            expected_sha = str(item.get("expected_sha256") or "").strip()
            if op in {"append_text", "replace_file"} and not expected_sha:
                raise ExecutionApplicationError(409, f"{op} operation requires expected_sha256: {target_path}")
            if expected_sha and originals[dest] is not None:
                actual_sha = hashlib.sha256(originals[dest] or b"").hexdigest()
                if actual_sha != expected_sha:
                    raise ExecutionApplicationError(409, f"Target file changed before apply: {target_path}")
            if op == "append_text":
                append_text = item.get("append_text")
                if not isinstance(append_text, str):
                    raise ExecutionApplicationError(409, f"append_text operation requires append_text: {target_path}")
                before = originals[dest]
                if before is None:
                    raise ExecutionApplicationError(409, f"append_text target does not exist: {target_path}")
                try:
                    data = (before.decode("utf-8") + append_text).encode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ExecutionApplicationError(409, f"append_text target is not UTF-8 text: {target_path}") from exc
            elif op in {"replace_file", "create_file"}:
                content = item.get("content")
                if not isinstance(content, str):
                    raise ExecutionApplicationError(409, f"{op} operation requires content: {target_path}")
                if op == "create_file" and originals[dest] is not None:
                    raise ExecutionApplicationError(409, f"create_file target already exists: {target_path}")
                data = content.encode("utf-8")
            else:
                raise ExecutionApplicationError(409, f"Unsupported operation: {op}")
            if len(data) > self.max_write_bytes:
                raise ExecutionApplicationError(409, f"Execution write exceeds {self.max_write_bytes} bytes: {target_path}")
            writes.append((dest, data))
        if not writes:
            raise ExecutionApplicationError(409, "Execution plan has no writable operations")
        try:
            for dest, data in writes:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
        except Exception as exc:
            rollback_errors: list[str] = []
            for dest, data in originals.items():
                try:
                    if data is None:
                        dest.unlink(missing_ok=True)
                    else:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(data)
                except Exception as rollback_exc:
                    rollback_errors.append(f"{dest}: {rollback_exc}")
            suffix = f"; rollback errors: {'; '.join(rollback_errors)}" if rollback_errors else ""
            raise ExecutionApplicationError(409, f"Execution apply failed and was rolled back: {exc}{suffix}") from exc

    @staticmethod
    def _agent_job_payload(job: AgentJobResponse | JsonObject | None) -> JsonObject | None:
        if job is None:
            return None
        if isinstance(job, AgentJobResponse):
            return cast(JsonObject, job.model_dump(mode="json"))
        return job

    def safe_workspace_target(self, target_path: str, *, workspace_dir: Path | None = None) -> Path:
        if not target_path:
            raise ExecutionApplicationError(409, "Target path is required")
        rel = Path(target_path)
        if rel.is_absolute() or ".." in rel.parts:
            raise ExecutionApplicationError(409, f"Unsafe target path: {target_path}")
        try:
            base = (workspace_dir or self.settings.main_workspace_dir).resolve(strict=True)
            dest = (base / rel).resolve(strict=False)
            dest.relative_to(base)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ExecutionApplicationError(409, f"Target path escapes main workspace: {target_path}") from exc
        return dest
