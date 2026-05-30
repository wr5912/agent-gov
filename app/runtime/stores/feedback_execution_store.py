from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from sqlalchemy import select, update

from ..agent_profiles import EXECUTION_OPTIMIZER_PROFILE
from ..errors import ConflictError
from ..feedback_job_flags import with_reused_existing
from ..feedback_schemas import validate_execution_plan_output
from ..runtime_db import OptimizationExecutionModel, OptimizationTaskModel, utc_now
from ..state_machines import validate_transition


class FeedbackExecutionStoreMixin:
    """Store operations for execution-optimizer jobs and execution plans."""

    def create_execution_job(
        self,
        task_id: str,
        *,
        profile_version: Optional[dict[str, Any]] = None,
        force: bool = False,
    ) -> Optional[dict[str, Any]]:
        task = self.find_task(task_id)
        if not task or task.get("applied_agent_version_id"):
            return None
        proposal = task.get("proposal") if isinstance(task.get("proposal"), dict) else None
        if not proposal or proposal.get("status") != "approved":
            return None
        if proposal.get("actionability") not in {"direct_workspace_change", "workspace_config_change"}:
            return None
        target_paths = [str(path) for path in task.get("target_paths") or [] if isinstance(path, str)]
        if not target_paths or any(not self._target_allowed(path) for path in target_paths):
            return None
        if not force:
            existing = self._latest_execution_job(task_id)
            if existing and existing.get("status") in {"queued", "running", "ready", "needs_human_review"}:
                return with_reused_existing(existing)
        job_id = f"fbe-{uuid.uuid4()}"
        baseline_version_id = self._string(task.get("baseline_agent_version_id")) or self._current_agent_version_id()
        input_payload = self._execution_job_input_payload(
            job_id=job_id,
            task_id=task_id,
            task=task,
            proposal=proposal,
            target_paths=target_paths,
            baseline_version_id=baseline_version_id,
        )
        try:
            input_path = self._write_job_input(job_id, "execution", input_payload)
            now = utc_now()
            job = self._scrub_record(
                {
                    "execution_job_id": job_id,
                    "optimization_task_id": task_id,
                    "feedback_case_id": task.get("feedback_case_id"),
                    "proposal_id": task.get("proposal_id"),
                    "status": "queued",
                    "profile_name": EXECUTION_OPTIMIZER_PROFILE,
                    "created_at": now,
                    "started_at": None,
                    "completed_at": None,
                    "baseline_agent_version_id": baseline_version_id,
                    "input_path": input_path,
                    "input_json": input_payload,
                    "raw_output_json": None,
                    "validated_output_json": None,
                    "error_json": None,
                    "profile_version": profile_version,
                }
            )
            with self.Session.begin() as db:
                db.add(
                    OptimizationExecutionModel(
                        execution_job_id=job_id,
                        optimization_task_id=task_id,
                        feedback_case_id=self._string(task.get("feedback_case_id")),
                        proposal_id=self._string(task.get("proposal_id")),
                        status="queued",
                        profile_name=EXECUTION_OPTIMIZER_PROFILE,
                        created_at=now,
                        baseline_agent_version_id=baseline_version_id,
                        payload_json=job,
                    )
                )
            self._attach_execution_job_to_task(task_id, job, status="execution_planning")
        except Exception:
            self._discard_execution_job(job_id)
            raise
        return self.get_execution_job(job_id)

    def _execution_job_input_payload(
        self,
        *,
        job_id: str,
        task_id: str,
        task: dict[str, Any],
        proposal: dict[str, Any],
        target_paths: list[str],
        baseline_version_id: Optional[str],
    ) -> dict[str, Any]:
        return {
            "schema_version": "execution-input/v1",
            "execution_job_id": job_id,
            "optimization_task_id": task_id,
            "feedback_case_id": task.get("feedback_case_id"),
            "proposal_id": task.get("proposal_id"),
            "proposal": proposal,
            "target_paths": target_paths,
            "allowed_target_paths": target_paths,
            "target_policy": self._execution_target_policy(),
            "target_file_contexts": self._execution_target_file_contexts(target_paths),
            "baseline_agent_version_id": baseline_version_id,
            "current_agent_version_id": self._current_agent_version_id(),
            "main_agent_manifest_path": str(self.data_dir / "agent-versions" / "main" / "current.json"),
            "task": "generate_controlled_execution_plan",
        }

    def start_execution_job(self, execution_job_id: str) -> Optional[dict[str, Any]]:
        return self._update_execution_job_payload(execution_job_id, status="running", fields={"started_at": utc_now()})

    def complete_execution_job(self, execution_job_id: str, raw_output: dict[str, Any]) -> Optional[dict[str, Any]]:
        job = self.get_execution_job(execution_job_id)
        if not job:
            return None
        output = self._execution_output_with_job_context(raw_output, job)
        validated, error = validate_execution_plan_output(output)
        if not validated:
            failed = self.fail_execution_job(execution_job_id, error_code="SCHEMA_VALIDATION_FAILED", message=error or "invalid execution output")
            return failed
        sanitized, sanitize_error = self._sanitize_execution_plan(validated, job)
        if sanitize_error:
            failed = self.fail_execution_job(execution_job_id, error_code="EXECUTION_PLAN_UNSAFE", message=sanitize_error)
            return failed
        next_status = "ready" if sanitized.get("status") == "ready" else "needs_human_review"
        task = self.find_task(str(job["optimization_task_id"]))
        with self.Session.begin() as db:
            row = self._update_execution_job_payload_row(
                db,
                execution_job_id,
                status=next_status,
                fields={
                    "completed_at": utc_now(),
                    "raw_output_json": raw_output,
                    "validated_output_json": sanitized,
                    "error_json": None,
                },
            )
            if not row:
                return None
            updated = self._execution_job_to_dict(row)
            if task:
                self._sync_execution_job_to_task_and_batch_row(
                    db,
                    task,
                    updated,
                    status="execution_ready" if next_status == "ready" else "needs_human_review",
                )
        return self.get_execution_job(execution_job_id)

    def _execution_output_with_job_context(self, raw_output: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
        output = dict(raw_output)
        output["execution_job_id"] = self._string(output.get("execution_job_id")) or self._string(job.get("execution_job_id"))
        output["optimization_task_id"] = self._string(output.get("optimization_task_id")) or self._string(job.get("optimization_task_id"))
        output["baseline_agent_version_id"] = self._string(output.get("baseline_agent_version_id")) or self._string(job.get("baseline_agent_version_id"))
        return output

    def fail_execution_job(self, execution_job_id: str, *, error_code: str, message: str) -> Optional[dict[str, Any]]:
        error_payload = {"error_code": error_code, "message": message, "created_at": utc_now(), "execution_job_id": execution_job_id}
        job = self.get_execution_job(execution_job_id)
        if not job:
            return None
        task = self.find_task(str(job["optimization_task_id"]))
        with self.Session.begin() as db:
            row = self._update_execution_job_payload_row(
                db,
                execution_job_id,
                status="failed",
                fields={"completed_at": utc_now(), "error_json": error_payload},
            )
            if not row:
                return None
            failed = self._execution_job_to_dict(row)
            if task:
                self._sync_execution_job_to_task_and_batch_row(db, task, failed, status="execution_failed")
        return self.get_execution_job(execution_job_id)

    def mark_execution_job_applied(
        self,
        execution_job_id: str,
        *,
        pre_execution_version: dict[str, Any],
        applied_agent_version: dict[str, Any],
        applied_diff: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        now = utc_now()
        fields = {
            "completed_at": now,
            "pre_execution_agent_version_id": self._string(pre_execution_version.get("agent_version_id")),
            "pre_execution_agent_version": pre_execution_version,
            "applied_agent_version_id": self._string(applied_agent_version.get("agent_version_id")),
            "applied_agent_version": applied_agent_version,
            "applied_diff": applied_diff or {},
        }
        with self.Session.begin() as db:
            row = db.get(OptimizationExecutionModel, execution_job_id, with_for_update=True)
            if not row:
                return None
            if row.status != "ready":
                raise ConflictError("Execution job has already been applied or is not ready")
            task_row = db.get(OptimizationTaskModel, row.optimization_task_id, with_for_update=True)
            if not task_row:
                return None
            task = dict(task_row.payload_json or {})
            if task_row.status != "execution_ready" or task.get("applied_agent_version_id"):
                raise ConflictError("Optimization task is not ready for execution application")
            validate_transition("execution_job", row.status, "completed")
            payload = dict(row.payload_json or {})
            payload.update(fields)
            payload["status"] = "completed"
            result = db.execute(
                update(OptimizationExecutionModel)
                .where(
                    OptimizationExecutionModel.execution_job_id == execution_job_id,
                    OptimizationExecutionModel.status == "ready",
                )
                .values(status="completed", completed_at=now, payload_json=payload)
            )
            if result.rowcount != 1:
                raise ConflictError("Execution job has already been applied or is not ready")
            db.flush()
            db.refresh(row)
            updated_job = self._execution_job_to_dict(row)
            task_row = self._mark_task_applied_row(
                db,
                task,
                agent_version=applied_agent_version,
                note=f"execution-optimizer 应用执行方案 {execution_job_id}。",
                pre_execution_version=pre_execution_version,
                execution_job=updated_job,
            )
            if task_row:
                updated_task = dict(task_row.payload_json or {})
                self._sync_task_execution_to_source_batch_row(db, updated_task, updated_job)
        return self.get_execution_job(execution_job_id)

    def get_execution_job(self, execution_job_id: str) -> Optional[dict[str, Any]]:
        if not execution_job_id:
            return None
        with self.Session() as db:
            row = db.get(OptimizationExecutionModel, execution_job_id)
            return self._execution_job_to_dict(row) if row else None

    def list_execution_jobs(self, task_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        with self.Session() as db:
            rows = db.scalars(
                select(OptimizationExecutionModel)
                .where(OptimizationExecutionModel.optimization_task_id == task_id)
                .order_by(OptimizationExecutionModel.created_at.desc())
            ).all()
        return [self._execution_job_to_dict(row) for row in rows[:limit]]


    def offline_execution_plan_output(self, job: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": "execution-plan-output/v1",
            "optimization_task_id": job["optimization_task_id"],
            "execution_job_id": job["execution_job_id"],
            "status": "needs_human_review",
            "baseline_agent_version_id": job.get("baseline_agent_version_id"),
            "summary": "当前未配置模型提供商，系统不能自动生成受控 patch。",
            "operations": [],
            "validation": "人工按优化方案修改后，可继续使用人工标记已应用兜底流程。",
            "risk": "离线占位不会修改主智能体 workspace。",
            "human_review_required": True,
            "no_action_reason": "MODEL_PROVIDER_NOT_CONFIGURED",
        }

    def deterministic_execution_plan_output(self, job: dict[str, Any]) -> Optional[dict[str, Any]]:
        input_json = job.get("input_json") if isinstance(job.get("input_json"), dict) else {}
        proposal = input_json.get("proposal") if isinstance(input_json.get("proposal"), dict) else {}
        target_paths = [str(path) for path in input_json.get("target_paths") or [] if isinstance(path, str)]
        if len(target_paths) != 1:
            return None
        target_path = target_paths[0]
        if not target_path.startswith("evals/") or proposal.get("target_type") != "eval_case":
            return None
        recommendation = self._string(proposal.get("recommendation")) or self._string(proposal.get("title")) or "反馈回归评估用例"
        content = {
            "schema_version": "feedback-eval-case/v1",
            "source": "execution_optimizer",
            "source_feedback_case_id": input_json.get("feedback_case_id"),
            "source_proposal_id": input_json.get("proposal_id"),
            "source_optimization_task_id": input_json.get("optimization_task_id"),
            "title": self._string(proposal.get("title")) or "反馈回归评估用例",
            "prompt": recommendation,
            "labels": self._unique_strings(["feedback_optimization", "eval_case", "execution_optimizer"]),
            "expected_behavior": self._string(proposal.get("expected_effect"))
            or self._string(proposal.get("validation"))
            or recommendation,
            "checks_json": {
                "requires_non_empty_answer": True,
                "requires_no_runtime_errors": True,
                "requires_human_review": True,
                "notes": "由 execution-optimizer 根据已批准优化方案生成的评估用例草案，首次应用后建议人工补充精确断言。",
            },
            "source_summary": {
                "recommendation": recommendation,
                "validation": proposal.get("validation"),
                "risk": proposal.get("risk"),
                "target_path": target_path,
            },
        }
        return {
            "schema_version": "execution-plan-output/v1",
            "optimization_task_id": job["optimization_task_id"],
            "execution_job_id": job["execution_job_id"],
            "status": "ready",
            "baseline_agent_version_id": job.get("baseline_agent_version_id"),
            "summary": "根据已批准的评估用例建议生成受控 create_file 执行方案。",
            "operations": [
                {
                    "operation": "create_file",
                    "path": target_path,
                    "content": json.dumps(content, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    "rationale": "目标为 evals/ 下的评估用例文件，可由后端根据方案确定性生成草案，避免执行优化智能体生成超长 JSON 超时。",
                }
            ],
            "validation": "应用前检查将创建的评估用例 JSON、目标路径和必需字段；应用后可在评估用例详情中补充精确断言，再手动运行回归验证。",
            "risk": "该方案只新增评估用例草案，不修改主智能体指令；语义断言仍需人工复核。",
            "human_review_required": True,
        }


    def _latest_execution_job(self, task_id: str) -> Optional[dict[str, Any]]:
        with self.Session() as db:
            row = db.scalars(
                select(OptimizationExecutionModel)
                .where(OptimizationExecutionModel.optimization_task_id == task_id)
                .order_by(OptimizationExecutionModel.created_at.desc())
            ).first()
            return self._execution_job_to_dict(row) if row else None

    def _execution_job_to_dict(self, row: OptimizationExecutionModel) -> dict[str, Any]:
        payload = dict(row.payload_json or {})
        payload["execution_job_id"] = row.execution_job_id
        payload["optimization_task_id"] = row.optimization_task_id
        payload["feedback_case_id"] = row.feedback_case_id
        payload["proposal_id"] = row.proposal_id
        payload["status"] = row.status
        payload["profile_name"] = row.profile_name
        payload["created_at"] = row.created_at
        payload["started_at"] = row.started_at
        payload["completed_at"] = row.completed_at
        payload["baseline_agent_version_id"] = row.baseline_agent_version_id
        payload["compensations"] = self._execution_compensations_for_job(row.execution_job_id)
        return payload

    def _update_execution_job_payload(
        self,
        execution_job_id: str,
        *,
        status: str,
        fields: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        with self.Session.begin() as db:
            if not self._update_execution_job_payload_row(db, execution_job_id, status=status, fields=fields):
                return None
        return self.get_execution_job(execution_job_id)

    def _update_execution_job_payload_row(
        self,
        db: Any,
        execution_job_id: str,
        *,
        status: str,
        fields: dict[str, Any],
    ) -> Optional[OptimizationExecutionModel]:
        row = db.get(OptimizationExecutionModel, execution_job_id)
        if not row:
            return None
        validate_transition("execution_job", row.status, status)
        payload = dict(row.payload_json or {})
        payload.update(fields)
        payload["status"] = status
        row.status = status
        if fields.get("started_at") is not None:
            row.started_at = self._string(fields.get("started_at"))
        if fields.get("completed_at") is not None:
            row.completed_at = self._string(fields.get("completed_at"))
        row.payload_json = payload
        return row

    def _sync_execution_job_to_task_and_batch_row(
        self,
        db: Any,
        task: dict[str, Any],
        job: dict[str, Any],
        *,
        status: str,
    ) -> None:
        task_row = self._attach_execution_job_to_task_row(db, task, job, status=status)
        if not task_row:
            return
        updated_task = dict(task_row.payload_json or {})
        self._sync_task_execution_to_source_batch_row(db, updated_task, job)

    def _sync_task_execution_to_source_batch_row(self, db: Any, task: dict[str, Any], job: Optional[dict[str, Any]] = None) -> None:
        batch_id = self._string(task.get("source_batch_id"))
        if not batch_id:
            return
        job = job if isinstance(job, dict) else None
        status = self._string(task.get("status")) or (
            "execution_ready" if (job or {}).get("status") == "ready" else self._string((job or {}).get("status")) or "execution_planning"
        )
        plan_task_id = self._string(task.get("source_plan_task_id"))
        if plan_task_id:
            task_updates = {
                "status": status,
                "optimization_task_id": task.get("optimization_task_id"),
                "applied_agent_version_id": task.get("applied_agent_version_id"),
            }
            top_level_fields = {
                "optimization_task_id": task.get("optimization_task_id"),
                "optimization_task": task,
            }
            if job:
                task_updates["execution_job_id"] = job.get("execution_job_id")
                task_updates["latest_execution_job"] = job
                top_level_fields["execution_job_id"] = job.get("execution_job_id")
                top_level_fields["execution_job"] = job
            self._update_batch_plan_task_row(
                db,
                batch_id,
                plan_task_id,
                task_updates,
                batch_status=status,
                top_level_fields=top_level_fields,
            )
            return
        fields = {
            "optimization_task_id": task.get("optimization_task_id"),
            "optimization_task": task,
        }
        if job:
            fields["execution_job_id"] = job.get("execution_job_id")
            fields["execution_job"] = job
        self._update_batch_row(
            db,
            batch_id,
            status=status,
            fields=fields,
        )

    def _discard_execution_job(self, execution_job_id: str) -> None:
        if not execution_job_id:
            return
        with self.Session.begin() as db:
            row = db.get(OptimizationExecutionModel, execution_job_id)
            if row:
                db.delete(row)
        self._cleanup_job_tmp(execution_job_id)


    def _sanitize_execution_plan(self, plan: dict[str, Any], job: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        sanitized = dict(plan)
        sanitized["execution_job_id"] = job["execution_job_id"]
        sanitized["optimization_task_id"] = job["optimization_task_id"]
        sanitized["baseline_agent_version_id"] = sanitized.get("baseline_agent_version_id") or job.get("baseline_agent_version_id")
        input_json = job.get("input_json") if isinstance(job.get("input_json"), dict) else {}
        target_paths = set(str(path) for path in input_json.get("target_paths") or [])
        target_contexts = {
            str(item.get("path")): item
            for item in input_json.get("target_file_contexts") or []
            if isinstance(item, dict) and item.get("path")
        }
        operations = []
        seen_write_paths: set[str] = set()
        for item in sanitized.get("operations") or []:
            if not isinstance(item, dict):
                return None, "operations must be objects"
            operation = dict(item)
            path = self._string(operation.get("path"))
            if not path or path not in target_paths:
                return None, f"operation path is not in task target_paths: {path or '-'}"
            if not self._target_allowed(path):
                return None, f"operation path is not allowed: {path}"
            op = self._string(operation.get("operation"))
            if op not in {"append_text", "replace_file", "create_file", "noop"}:
                return None, f"operation is not supported: {op or '-'}"
            context = target_contexts.get(path) or self._execution_target_file_context(path)
            skipped_reason = self._string(context.get("skipped_reason")) if isinstance(context, dict) else None
            if op != "noop":
                if skipped_reason:
                    return None, f"operation target is not safely editable: {path} ({skipped_reason})"
                if path in seen_write_paths:
                    return None, f"multiple write operations for one path are not supported: {path}"
                seen_write_paths.add(path)
            if op == "append_text" and not isinstance(operation.get("append_text"), str):
                return None, f"append_text operation must include append_text: {path}"
            if op in {"replace_file", "create_file"} and not isinstance(operation.get("content"), str):
                return None, f"{op} operation must include content: {path}"
            if op in {"append_text", "replace_file"}:
                if not context.get("exists") or context.get("type") != "file":
                    return None, f"{op} target must be an existing managed text file: {path}"
                expected_sha = self._string(context.get("sha256"))
                if expected_sha and not operation.get("expected_sha256"):
                    operation["expected_sha256"] = expected_sha
            if op == "create_file" and context.get("exists"):
                return None, f"create_file target already exists: {path}"
            operations.append(operation)
        sanitized["operations"] = operations
        if sanitized.get("status") == "ready" and not operations:
            return None, "ready execution plan has no operations"
        return sanitized, None
