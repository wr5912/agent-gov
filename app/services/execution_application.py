from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from threading import Lock
from typing import Any, Awaitable, Callable

from pydantic import BaseModel

from app.runtime.agent_version_store import AgentVersionStore
from app.runtime.errors import FeedbackStoreError
from app.runtime.records.feedback_compensation_records import ExecutionCompensationRecord
from app.runtime.records.json_types import JsonObject
from app.runtime.response_schemas.agent_job_response_schemas import AgentJobResponse
from app.runtime.response_schemas.feedback_workflow_response_schemas import (
    ExecutionCompensationResponse,
    OptimizationExecutionApplyResponse,
    OptimizationTaskResponse,
)
from app.runtime.stores.feedback_store import FeedbackStore
from app.runtime.settings import AppSettings


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


class ExecutionApplicationService:
    """Coordinates execution-plan application across workspace files and store state."""

    def __init__(
        self,
        *,
        settings: AppSettings,
        feedback_store: FeedbackStore,
        agent_version_store: AgentVersionStore,
        max_write_bytes: int = 500_000,
    ) -> None:
        self.settings = settings
        self.feedback_store = feedback_store
        self.agent_version_store = agent_version_store
        self.max_write_bytes = max_write_bytes
        self._apply_lock = Lock()

    async def run_and_apply_execution_job(
        self,
        task_id: str,
        *,
        run_execution_job: Callable[..., Awaitable[AgentJobResponse | dict[str, Any] | None]],
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

    def mark_task_applied_manually(self, task_id: str, *, note: str | None = None) -> OptimizationTaskResponse:
        with self._apply_lock:
            task = self.feedback_store.ensure_task_can_mark_applied_manually_record(task_id)
            if not task:
                raise ExecutionApplicationError(404, "Optimization task not found")
            if task.applied_agent_version_id:
                return OptimizationTaskResponse.model_validate(task.to_payload())
            version = self.agent_version_store.create_snapshot(
                reason="proposal_applied",
                source_proposal_ids=[str(item) for item in task.proposal_ids if item],
                note=note or f"优化任务 {task_id} 已人工应用，创建主智能体版本快照。",
            )
            updated = self.feedback_store.mark_task_applied_record(task_id, agent_version=version, note=note)
            if not updated:
                raise ExecutionApplicationError(404, "Optimization task not found")
            return OptimizationTaskResponse.model_validate(updated.to_payload())

    def _apply_ready_execution_job_locked(
        self,
        task_id: str,
        execution_job_id: str,
        *,
        note: str | None = None,
    ) -> OptimizationExecutionApplyResponse:
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
        current_version_id = self.agent_version_store.current_version_id()
        if baseline_version_id and current_version_id != baseline_version_id:
            raise ExecutionApplicationError(409, "Current Agent version differs from execution baseline")

        pre_version = self.agent_version_store.create_snapshot(
            reason="pre_execution",
            source_proposal_ids=[str(item) for item in task.get("proposal_ids") or [] if item],
            note=note or f"执行优化任务 {task_id} 前快照。",
        )
        try:
            self.apply_execution_operations(plan.get("operations") or [])
        except ExecutionApplicationError as exc:
            self.feedback_store.record_execution_application_failed(
                execution_job_id,
                optimization_task_id=task_id,
                message=str(exc),
                pre_execution_version=pre_version,
            )
            raise

        try:
            applied_version = self.agent_version_store.create_snapshot(
                reason="execution_optimizer_applied",
                source_proposal_ids=[str(item) for item in task.get("proposal_ids") or [] if item],
                note=note or f"execution-optimizer 应用任务 {task_id}。",
                parent_version_id=str(pre_version.get("agent_version_id")),
            )
            applied_diff = self.agent_version_store.diff_versions(
                str(pre_version["agent_version_id"]),
                str(applied_version["agent_version_id"]),
            )
            execution_application = self.feedback_store.record_execution_application_applied(
                execution_job_id,
                pre_execution_version=pre_version,
                applied_agent_version=applied_version,
                applied_diff=applied_diff,
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
        execution_job = self.feedback_store.get_execution_job(execution_job_id)
        optimization_task = self.feedback_store.find_task(task_id)
        if not execution_job or not optimization_task:
            raise ExecutionApplicationError(404, "Execution application result not found")
        return OptimizationExecutionApplyResponse(
            execution_job=execution_job,
            execution_application=execution_application,
            optimization_task=optimization_task,
            applied_diff=applied_diff,
        )

    def _compensate_post_write_failure(
        self,
        *,
        task_id: str,
        execution_job_id: str,
        pre_version: dict[str, Any],
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

    def _execution_plan_ready(self, job: dict[str, Any]) -> bool:
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

    def apply_execution_operations(self, operations: list[Any]) -> None:
        if not operations:
            raise ExecutionApplicationError(409, "Execution plan has no operations")
        originals: dict[Path, bytes | None] = {}
        writes: list[tuple[Path, bytes]] = []
        for item in operations:
            if not isinstance(item, dict):
                raise ExecutionApplicationError(409, "Execution operation must be an object")
            op = str(item.get("operation") or "")
            target_path = str(item.get("path") or "")
            dest = self.safe_workspace_target(target_path)
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
            return job.model_dump(mode="json")
        return job

    def safe_workspace_target(self, target_path: str) -> Path:
        if not target_path:
            raise ExecutionApplicationError(409, "Target path is required")
        rel = Path(target_path)
        if rel.is_absolute() or ".." in rel.parts:
            raise ExecutionApplicationError(409, f"Unsafe target path: {target_path}")
        try:
            base = self.settings.main_workspace_dir.resolve(strict=True)
            dest = (base / rel).resolve(strict=False)
            dest.relative_to(base)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ExecutionApplicationError(409, f"Target path escapes main workspace: {target_path}") from exc
        return dest
