from __future__ import annotations

import difflib
import hashlib
import json
import uuid
from typing import Any, Optional

from sqlalchemy import select

from ..agent_job_types import agent_job_spec
from ..errors import ConflictError
from ..feedback_job_flags import with_reused_existing
from ..feedback_schemas import (
    ExecutionPlanFormatterOutput,
    ExecutionPlanOutput,
    coerce_execution_plan_output_model,
    output_model_payload,
)
from ..json_types import JsonObject
from ..records.execution_records import ExecutionApplicationRecord
from ..records.optimization_task_records import OptimizationTaskRecord
from ..runtime_db import AgentJobModel, ExecutionApplicationModel, OptimizationTaskModel, utc_now

MAX_PLANNED_DIFF_INPUT_CHARS = 120_000
MAX_PLANNED_DIFF_OUTPUT_CHARS = 80_000
EXECUTION_JOB_ACTIONABILITIES = {"direct_workspace_change", "workspace_config_change", "eval_only"}


class FeedbackExecutionStoreMixin:
    """Store operations for execution-optimizer jobs and execution plans."""

    def create_execution_job(
        self,
        task_id: str,
        *,
        profile_version: Optional[JsonObject] = None,
        force: bool = False,
    ) -> Optional[JsonObject]:
        task = self.find_task(task_id)
        if not task or self._execution_job_queue_blockers(task):
            return None
        proposal = task.get("proposal") if isinstance(task.get("proposal"), dict) else None
        target_paths = [str(path) for path in task.get("target_paths") or [] if isinstance(path, str)]
        proposal = proposal or {}
        if not force:
            existing = self._latest_execution_job(task_id)
            if existing and existing.get("status") in {"queued", "running", "completed", "needs_human_review"}:
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
            spec = agent_job_spec("execution")
            job = self.create_agent_job(
                job_id=job_id,
                job_type=spec.job_type,
                scope_kind="optimization_task",
                scope_id=task_id,
                profile_name=spec.profile_name,
                input_payload=input_payload,
                profile_version=profile_version,
            )
            self._attach_execution_job_to_task(task_id, job, status="execution_planning")
        except Exception:
            self._discard_execution_job(job_id)
            raise
        return self.get_execution_job(job_id)

    def execution_job_queue_blocker(self, task_id: str) -> Optional[str]:
        task = self.find_task(task_id)
        if not task:
            return "找不到对应的优化任务，无法执行。"
        blockers = self._execution_job_queue_blockers(task)
        return "；".join(blockers) if blockers else None

    def _execution_job_queue_blockers(self, task: JsonObject) -> list[str]:
        blockers: list[str] = []
        if task.get("applied_agent_version_id"):
            blockers.append("这个优化任务已经应用过，不能重复执行。")
        proposal = task.get("proposal") if isinstance(task.get("proposal"), dict) else None
        if not proposal:
            blockers.append("这个优化任务缺少已确认的方案快照，无法生成执行方案。")
            return blockers
        if proposal.get("status") != "approved":
            blockers.append("这个优化任务的方案还不是 approved 状态，请先确认方案后再执行。")
        target_paths = [str(path).strip() for path in task.get("target_paths") or [] if isinstance(path, str) and str(path).strip()]
        target_file = self._string((proposal.get("task_context") or {}).get("target_file")) if isinstance(proposal.get("task_context"), dict) else ""
        if not target_paths:
            blockers.append("这个优化任务没有填写可执行的目标文件路径。")
        for target_path in target_paths:
            denied = self._target_denied_reason(target_path)
            if denied:
                blockers.append(f"目标文件“{target_path}”不在允许修改的 workspace 文件范围内，原因：{denied}。")
            if target_file and len(target_paths) == 1 and target_path != target_file:
                blockers.append(
                    f"目标文件现在填的是“{target_path}”，但任务上下文里识别出的具体文件是“{target_file}”。"
                    f"请编辑任务，把“目标文件”改成“{target_file}”这种相对 main-workspace 的具体文件路径。"
                )
        actionability = self._string(proposal.get("actionability"))
        if actionability not in EXECUTION_JOB_ACTIONABILITIES:
            blockers.append(
                f"可执行性现在是“{actionability or '空'}”，execution-optimizer 不能直接执行这种类型。"
                "如果这是修改 workspace 配置文件的任务，请编辑任务，把“可执行性”改成“workspace_config_change”。"
            )
        return blockers

    def _execution_job_input_payload(
        self,
        *,
        job_id: str,
        task_id: str,
        task: JsonObject,
        proposal: JsonObject,
        target_paths: list[str],
        baseline_version_id: Optional[str],
    ) -> JsonObject:
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
            **self._agent_git_paths_context(),
            "task": "generate_controlled_execution_plan",
        }

    def start_execution_job(self, execution_job_id: str) -> Optional[JsonObject]:
        with self.Session.begin() as db:
            if not self._append_agent_job_update_row(db, execution_job_id, status="running", started_at=utc_now()):
                return None
        return self.get_execution_job(execution_job_id)

    def complete_execution_job(
        self,
        execution_job_id: str,
        formatter_output: ExecutionPlanFormatterOutput | ExecutionPlanOutput | JsonObject,
    ) -> Optional[JsonObject]:
        job = self.get_execution_job(execution_job_id)
        if not job:
            return None
        formatter_output_json = (
            output_model_payload(formatter_output) if isinstance(formatter_output, (ExecutionPlanFormatterOutput, ExecutionPlanOutput)) else formatter_output
        )
        output = self._execution_output_with_job_context(formatter_output_json, job)
        output_model, error = coerce_execution_plan_output_model(output)
        raw_payload = output_model_payload(output_model) if output_model else formatter_output_json
        if not output_model:
            failed = self.fail_execution_job(
                execution_job_id,
                error_code="SCHEMA_VALIDATION_FAILED",
                message=error or "invalid execution output",
                raw_output_json=formatter_output_json,
            )
            return failed
        validated = output_model_payload(output_model)
        sanitized, sanitize_error = self._sanitize_execution_plan(validated, job)
        if sanitize_error:
            failed = self.fail_execution_job(
                execution_job_id,
                error_code="EXECUTION_PLAN_UNSAFE",
                message=sanitize_error,
                raw_output_json=raw_payload,
            )
            return failed
        next_status = "completed" if sanitized.get("status") == "ready" else "needs_human_review"
        task = self.find_task(str(job["optimization_task_id"]))
        with self.Session.begin() as db:
            row = self._set_agent_job_json_row(
                db,
                execution_job_id,
                raw_output_json=raw_payload,
                validated_output_json=sanitized,
                error_json=None,
            )
            if not row:
                return None
            self._append_agent_job_update_row(db, execution_job_id, status="schema_validating")
            completed_row = self._append_agent_job_update_row(db, execution_job_id, status=next_status, completed_at=utc_now())
            updated = self._execution_job_to_dict(completed_row) if completed_row else self._agent_job_to_dict(row)
            if task:
                self._sync_execution_job_to_task_and_batch_row(
                    db,
                    task,
                    updated,
                    status="execution_ready" if sanitized.get("status") == "ready" else "needs_human_review",
                )
        return self.get_execution_job(execution_job_id)

    def _execution_output_with_job_context(self, formatter_output_json: JsonObject, job: JsonObject) -> JsonObject:
        output = dict(formatter_output_json)
        output["execution_job_id"] = self._string(job.get("execution_job_id"))
        output["optimization_task_id"] = self._string(job.get("optimization_task_id"))
        output["baseline_agent_version_id"] = self._string(job.get("baseline_agent_version_id"))
        return output

    def fail_execution_job(
        self,
        execution_job_id: str,
        *,
        error_code: str,
        message: str,
        raw_output_json: Optional[JsonObject] = None,
    ) -> Optional[JsonObject]:
        error_payload = {"error_code": error_code, "message": message, "created_at": utc_now(), "execution_job_id": execution_job_id}
        job = self.get_execution_job(execution_job_id)
        if not job:
            return None
        task = self.find_task(str(job["optimization_task_id"]))
        with self.Session.begin() as db:
            json_fields: dict[str, JsonObject] = {"error_json": error_payload}
            if raw_output_json is not None:
                json_fields["raw_output_json"] = raw_output_json
            row = self._set_agent_job_json_row(
                db,
                execution_job_id,
                **json_fields,
            )
            if not row:
                return None
            failed_row = self._append_agent_job_update_row(db, execution_job_id, status="failed", completed_at=utc_now())
            failed = self._execution_job_to_dict(failed_row) if failed_row else self._agent_job_to_dict(row)
            if task:
                self._sync_execution_job_to_task_and_batch_row(db, task, failed, status="execution_failed")
        return self.get_execution_job(execution_job_id)

    def record_execution_application_applied(
        self,
        execution_job_id: str,
        *,
        pre_execution_version: JsonObject,
        applied_agent_version: JsonObject,
        applied_diff: Optional[JsonObject] = None,
        change_set: Optional[JsonObject] = None,
        candidate_commit_sha: Optional[str] = None,
        note: Optional[str] = None,
    ) -> Optional[JsonObject]:
        now = utc_now()
        with self.Session.begin() as db:
            job_row = db.get(AgentJobModel, execution_job_id, with_for_update=True)
            if not job_row or job_row.job_type != "execution":
                return None
            job = self._execution_job_to_dict(job_row)
            plan = job.get("validated_output_json") if isinstance(job.get("validated_output_json"), dict) else {}
            if job_row.status != "completed" or plan.get("status") != "ready":
                raise ConflictError("Execution job is not ready")
            task_row = db.get(OptimizationTaskModel, str(job["optimization_task_id"]), with_for_update=True)
            if not task_row:
                return None
            task = OptimizationTaskRecord.from_row(task_row)
            if task_row.status not in {"execution_ready", "applied_pending_regression"} or task.applied_agent_version_id:
                raise ConflictError("Optimization task is not ready for execution application")
            payload = {
                "schema_version": "execution-application/v1",
                "application_id": f"exa-{uuid.uuid4()}",
                "execution_job_id": execution_job_id,
                "optimization_task_id": str(job["optimization_task_id"]),
                "created_at": now,
                "completed_at": None,
                "status": "created",
                "pre_execution_agent_version_id": self._string(pre_execution_version.get("agent_version_id")),
                "pre_execution_agent_version": pre_execution_version,
                "applied_agent_version_id": self._string(applied_agent_version.get("agent_version_id")),
                "applied_agent_version": applied_agent_version,
                "applied_diff": applied_diff or {},
                "change_set_id": self._string((change_set or {}).get("change_set_id")),
                "change_set": change_set,
                "candidate_commit_sha": candidate_commit_sha,
                "error_json": None,
            }
            application_row = self._create_execution_application_row(db, payload)
            self._complete_execution_application_row(db, application_row, status="applied", fields={"completed_at": now})
            application = self._execution_application_to_dict(application_row)
            updated_job = self._execution_job_to_dict(job_row)
            updated_task_row = self._mark_task_applied_row(
                db,
                task,
                agent_version=applied_agent_version,
                note=note or f"execution-optimizer 应用执行方案 {execution_job_id}。",
                pre_execution_version=pre_execution_version,
                execution_job=updated_job,
            )
            if updated_task_row:
                task_fields = {
                    "latest_execution_application_id": application["application_id"],
                    "latest_execution_application": application,
                }
                if change_set:
                    task_fields["latest_change_set_id"] = change_set.get("change_set_id")
                    task_fields["latest_change_set"] = change_set
                    task_fields["candidate_commit_sha"] = candidate_commit_sha or change_set.get("candidate_commit_sha")
                updated_task_row = self._update_task_payload_row(
                    db,
                    str(updated_task_row.optimization_task_id),
                    status=updated_task_row.status,
                    fields=task_fields,
                )
            if updated_task_row:
                updated_task = self._task_to_dict(updated_task_row)
                self._sync_task_execution_to_source_batch_row(db, updated_task, updated_job)
            return application

    def record_execution_application_failed(
        self,
        execution_job_id: str,
        *,
        optimization_task_id: str,
        message: str,
        pre_execution_version: Optional[JsonObject] = None,
        status: str = "failed",
    ) -> JsonObject:
        now = utc_now()
        payload = {
            "schema_version": "execution-application/v1",
            "application_id": f"exa-{uuid.uuid4()}",
            "execution_job_id": execution_job_id,
            "optimization_task_id": optimization_task_id,
            "created_at": now,
            "completed_at": None,
            "status": "created",
            "pre_execution_agent_version_id": self._string((pre_execution_version or {}).get("agent_version_id")),
            "pre_execution_agent_version": pre_execution_version,
            "applied_agent_version_id": None,
            "applied_agent_version": None,
            "applied_diff": {},
            "error_json": {"error_code": "EXECUTION_APPLY_FAILED", "message": message, "created_at": now},
        }
        with self.Session.begin() as db:
            row = self._create_execution_application_row(db, payload)
            self._complete_execution_application_row(db, row, status=status, fields={"completed_at": now})
            return self._execution_application_to_dict(row)

    def get_execution_job(self, execution_job_id: str) -> Optional[JsonObject]:
        job = self.get_agent_job(execution_job_id)
        if not job or job.get("job_type") != "execution":
            return None
        return job

    def list_execution_jobs(self, task_id: str, *, limit: int = 100) -> list[JsonObject]:
        return self.list_agent_jobs(
            job_type="execution",
            scope_kind="optimization_task",
            scope_id=task_id,
            limit=limit,
        )

    def deterministic_execution_plan_output(self, job: JsonObject) -> Optional[JsonObject]:
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
            "expected_behavior": self._string(proposal.get("expected_effect")) or self._string(proposal.get("validation")) or recommendation,
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

    def _latest_execution_job(self, task_id: str) -> Optional[JsonObject]:
        with self.Session() as db:
            row = db.scalars(
                select(AgentJobModel)
                .where(
                    AgentJobModel.job_type == "execution",
                    AgentJobModel.scope_kind == "optimization_task",
                    AgentJobModel.scope_id == task_id,
                )
                .order_by(AgentJobModel.created_at.desc())
            ).first()
            return self._execution_job_to_dict(row) if row else None

    def _execution_job_to_dict(self, row: AgentJobModel) -> JsonObject:
        return self._agent_job_to_dict(row)

    def _sync_execution_job_to_task_and_batch_row(
        self,
        db: Any,
        task: JsonObject,
        job: JsonObject,
        *,
        status: str,
    ) -> None:
        task_row = self._attach_execution_job_to_task_row(db, task, job, status=status)
        if not task_row:
            return
        updated_task = self._task_to_dict(task_row)
        self._sync_task_execution_to_source_batch_row(db, updated_task, job)

    def _sync_task_execution_to_source_batch_row(self, db: Any, task: JsonObject, job: Optional[JsonObject] = None) -> None:
        batch_id = self._string(task.get("source_batch_id"))
        if not batch_id:
            return
        job = job if isinstance(job, dict) else None
        job_plan = (job or {}).get("validated_output_json") if isinstance((job or {}).get("validated_output_json"), dict) else {}
        status = self._string(task.get("status")) or (
            "execution_ready" if job_plan.get("status") == "ready" else self._string((job or {}).get("status")) or "execution_planning"
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
            row = db.get(AgentJobModel, execution_job_id)
            if row:
                db.delete(row)
        self._cleanup_job_tmp(execution_job_id)

    def find_execution_application(self, application_id: str) -> Optional[JsonObject]:
        if not application_id:
            return None
        with self.Session() as db:
            row = db.get(ExecutionApplicationModel, application_id)
            return self._execution_application_to_dict(row) if row else None

    def latest_execution_application(self, execution_job_id: str) -> Optional[JsonObject]:
        if not execution_job_id:
            return None
        with self.Session() as db:
            row = db.scalars(
                select(ExecutionApplicationModel)
                .where(ExecutionApplicationModel.execution_job_id == execution_job_id)
                .order_by(ExecutionApplicationModel.created_at.desc())
            ).first()
            return self._execution_application_to_dict(row) if row else None

    def _create_execution_application_row(self, db: Any, payload: JsonObject) -> ExecutionApplicationModel:
        record = ExecutionApplicationRecord.model_validate(payload)
        row = ExecutionApplicationModel(
            application_id=record.application_id,
            execution_job_id=record.execution_job_id,
            optimization_task_id=record.optimization_task_id,
            created_at=record.created_at,
            completed_at=None,
            status=record.status,
            payload_json=record.to_payload(),
        )
        db.add(row)
        db.flush()
        return row

    def _complete_execution_application_row(
        self,
        db: Any,
        row: ExecutionApplicationModel,
        *,
        status: str,
        fields: JsonObject,
    ) -> ExecutionApplicationModel:
        updated = ExecutionApplicationRecord.from_row(row).transition_to(
            status,
            fields=fields,
        )
        row.status = updated.status
        row.completed_at = updated.completed_at
        row.payload_json = updated.to_payload()
        return row

    def _execution_application_to_dict(self, row: ExecutionApplicationModel) -> JsonObject:
        return ExecutionApplicationRecord.from_row(row).to_payload()

    def _sanitize_execution_plan(self, plan: JsonObject, job: JsonObject) -> tuple[Optional[JsonObject], Optional[str]]:
        sanitized = dict(plan)
        sanitized["execution_job_id"] = job["execution_job_id"]
        sanitized["optimization_task_id"] = job["optimization_task_id"]
        sanitized["baseline_agent_version_id"] = job.get("baseline_agent_version_id")
        input_json = job.get("input_json") if isinstance(job.get("input_json"), dict) else {}
        target_paths = set(str(path) for path in input_json.get("target_paths") or [])
        target_contexts = {str(item.get("path")): item for item in input_json.get("target_file_contexts") or [] if isinstance(item, dict) and item.get("path")}
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
        sanitized["planned_diff"] = self._build_execution_planned_diff(operations, target_contexts)
        if sanitized.get("status") == "ready" and not operations:
            return None, "ready execution plan has no operations"
        return sanitized, None

    def _build_execution_planned_diff(
        self,
        operations: list[JsonObject],
        target_contexts: dict[str, JsonObject],
    ) -> JsonObject:
        files: list[JsonObject] = []
        counts = {
            "added": 0,
            "modified": 0,
            "deleted": 0,
            "unchanged": 0,
            "noop": 0,
        }
        for operation in operations:
            entry = self._build_execution_planned_diff_entry(operation, target_contexts)
            files.append(entry)
            status = self._string(entry.get("status"))
            if status in counts:
                counts[status] += 1
        return {
            "schema_version": "execution-planned-diff/v1",
            "files": files,
            **counts,
        }

    def _build_execution_planned_diff_entry(
        self,
        operation: JsonObject,
        target_contexts: dict[str, JsonObject],
    ) -> JsonObject:
        op = self._string(operation.get("operation")) or "operation"
        path = self._string(operation.get("path")) or "-"
        context = target_contexts.get(path) or self._execution_target_file_context(path)
        before_text = context.get("content_text") if isinstance(context.get("content_text"), str) else ""
        before_bytes = before_text.encode("utf-8")
        after_text = before_text
        status = "noop"
        reason = self._string(context.get("skipped_reason"))
        if op == "append_text":
            after_text = before_text + str(operation.get("append_text") or "")
            status = "modified" if after_text != before_text else "unchanged"
            reason = None
        elif op == "replace_file":
            after_text = str(operation.get("content") or "")
            status = "modified" if after_text != before_text else "unchanged"
            reason = None
        elif op == "create_file":
            before_text = ""
            before_bytes = b""
            after_text = str(operation.get("content") or "")
            status = "added" if after_text else "unchanged"
            reason = None
        elif op == "noop":
            status = "noop"
            reason = self._string(operation.get("rationale")) or reason or "noop operation"
        after_bytes = after_text.encode("utf-8")
        unified_diff = ""
        truncated = False
        if op != "noop":
            unified_diff, truncated, truncate_reason = self._planned_unified_diff(path, before_text, after_text)
            reason = truncate_reason or reason
        return {
            "path": path,
            "operation": op,
            "status": status,
            "expected_sha256": self._string(operation.get("expected_sha256")),
            "before_sha256": hashlib.sha256(before_bytes).hexdigest() if before_bytes or context.get("exists") else None,
            "after_sha256": hashlib.sha256(after_bytes).hexdigest(),
            "unified_diff": unified_diff,
            "is_text": True,
            "truncated": truncated,
            "reason": reason,
            "rationale": self._string(operation.get("rationale")),
        }

    def _planned_unified_diff(self, path: str, before_text: str, after_text: str) -> tuple[str, bool, Optional[str]]:
        if len(before_text) + len(after_text) > MAX_PLANNED_DIFF_INPUT_CHARS:
            return "", True, f"计划变更内容超过 {MAX_PLANNED_DIFF_INPUT_CHARS} 字符，未展开 diff。"
        diff = "".join(
            difflib.unified_diff(
                before_text.splitlines(keepends=True),
                after_text.splitlines(keepends=True),
                fromfile=f"before:{path}",
                tofile=f"after:{path}",
                lineterm="\n",
            )
        )
        if len(diff) <= MAX_PLANNED_DIFF_OUTPUT_CHARS:
            return diff, False, None
        truncated = diff[:MAX_PLANNED_DIFF_OUTPUT_CHARS].rstrip() + "\n... planned diff truncated ...\n"
        return truncated, True, f"计划 diff 超过 {MAX_PLANNED_DIFF_OUTPUT_CHARS} 字符，已截断展示。"
