from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from app.runtime.agent_version_store import AgentVersionStore
from app.runtime.errors import FeedbackStoreError
from app.runtime.feedback_compensation_models import ExecutionCompensationRecord
from app.runtime.feedback_store import FeedbackStore
from app.runtime.settings import AppSettings


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

    def apply_ready_execution_job(
        self,
        task_id: str,
        execution_job_id: str,
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        task = self.feedback_store.find_task(task_id)
        if not task:
            raise ExecutionApplicationError(404, "Optimization task not found")
        if task.get("applied_agent_version_id"):
            raise ExecutionApplicationError(409, "Task is already applied")
        job = self.feedback_store.get_execution_job(execution_job_id)
        if not job or job.get("optimization_task_id") != task_id:
            raise ExecutionApplicationError(404, "Execution job not found")
        if job.get("status") != "ready":
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
            self.feedback_store.fail_execution_job(execution_job_id, "EXECUTION_APPLY_FAILED", str(exc))
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
            updated = self.feedback_store.mark_execution_job_applied(
                execution_job_id,
                pre_execution_version=pre_version,
                applied_agent_version=applied_version,
                applied_diff=applied_diff,
            )
            if not updated:
                raise ExecutionApplicationError(404, "Execution job not found")
        except Exception as exc:
            detail = self._compensate_post_write_failure(
                task_id=task_id,
                execution_job_id=execution_job_id,
                pre_version=pre_version,
                error=exc,
            )
            raise ExecutionApplicationError(409, detail) from exc
        return {
            "execution_job": updated,
            "optimization_task": self.feedback_store.find_task(task_id),
            "applied_diff": applied_diff,
        }

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
            pass

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
            self.feedback_store.fail_execution_job(execution_job_id, "EXECUTION_APPLY_STATE_SYNC_FAILED", detail)
        except Exception:
            pass
        return detail

    def restore_execution_compensation(self, compensation_id: str) -> dict[str, Any]:
        compensation = self.feedback_store.find_execution_compensation(compensation_id)
        if not compensation:
            raise ExecutionApplicationError(404, "Execution compensation not found")
        record = ExecutionCompensationRecord.model_validate(compensation)
        if record.status == "resolved":
            return record.to_payload()
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
        return updated

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
