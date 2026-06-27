from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any, Optional

from ..agent_job_types import agent_job_spec
from ..errors import BusinessRuleViolation, ConflictError
from ..feedback_job_flags import no_actionable_attributions, with_reused_existing
from ..feedback_schemas import (
    FeedbackOptimizationPlanFormatterOutput,
    coerce_feedback_optimization_plan_output_model,
    output_model_payload,
)
from ..json_types import JsonObject
from ..records.batch_plan_records import (
    FeedbackOptimizationAttributionSummaryRecord,
    FeedbackOptimizationBlockedItemRecord,
    FeedbackOptimizationPlanRecord,
    FeedbackOptimizationPlanTaskRecord,
)
from ..runtime_db import utc_now

BATCH_PLAN_ACTIVE_JOB_STATUSES = {"created", "queued", "running", "schema_validating", "evidence_packaging"}


class FeedbackBatchPlanStoreMixin:
    """Store operations for batch optimization plan generation and task execution."""

    def generate_batch_optimization_plan(self, batch_id: str, *, regeneration_instruction: Optional[str] = None) -> Optional[JsonObject]:
        batch = self.find_optimization_batch(batch_id)
        if not batch:
            return None
        self._assert_batch_plan_can_regenerate(batch)
        self._discard_batch_draft_artifacts(batch)
        instruction = regeneration_instruction.strip() if isinstance(regeneration_instruction, str) else None
        instruction = instruction or None
        attributions = self._batch_attribution_outputs(batch)
        if not attributions:
            return self._update_batch(
                batch_id,
                status="needs_human_review",
                fields={
                    **self._batch_draft_artifact_reset_fields(),
                    "optimization_plan": self._non_actionable_plan(batch, "暂无可用归因结果，不能生成可执行优化方案。", instruction),
                    "optimization_plan_job_id": None,
                    "optimization_plan_job": None,
                    "optimization_plan_error": None,
                },
            )
        plan = self._build_batch_optimization_plan(batch, attributions, regeneration_instruction=instruction)
        return self._update_batch(
            batch_id,
            status=plan["status"],
            fields={
                **self._batch_draft_artifact_reset_fields(),
                "optimization_plan": plan,
                "optimization_plan_job_id": None,
                "optimization_plan_job": None,
                "optimization_plan_error": None,
                "updated_at": utc_now(),
            },
        )

    def create_batch_plan_job(
        self,
        batch_id: str,
        *,
        profile_version: Optional[JsonObject] = None,
        force: bool = True,
        regeneration_instruction: Optional[str] = None,
    ) -> Optional[JsonObject]:
        with self._job_create_lock:
            return self._create_batch_plan_job_locked(
                batch_id,
                profile_version=profile_version,
                force=force,
                regeneration_instruction=regeneration_instruction,
            )

    def _create_batch_plan_job_locked(
        self,
        batch_id: str,
        *,
        profile_version: Optional[JsonObject] = None,
        force: bool = True,
        regeneration_instruction: Optional[str] = None,
    ) -> Optional[JsonObject]:
        batch = self.find_optimization_batch(batch_id)
        if not batch:
            return None
        self._assert_batch_plan_can_regenerate(batch)
        instruction = regeneration_instruction.strip() if isinstance(regeneration_instruction, str) else None
        instruction = instruction or None
        if not force and not instruction and isinstance(batch.get("optimization_plan"), dict):
            return with_reused_existing(batch)
        active_job = self._latest_active_batch_plan_job(batch_id)
        if active_job:
            self._discard_inactive_batch_plan_jobs(batch_id, keep_job_id=str(active_job["job_id"]))
            return active_job
        attributions = self._batch_attribution_outputs(batch)
        if not attributions:
            self._discard_inactive_batch_plan_jobs(batch_id)
            self._record_no_actionable_batch_plan(batch_id, batch, instruction)
            return no_actionable_attributions(batch_id)

        job_id = f"fbp-{uuid.uuid4()}"
        try:
            self._discard_batch_draft_artifacts(batch)
            input_payload = self._batch_plan_job_input_payload(batch, attributions, job_id, instruction)
            self._create_batch_plan_agent_job(batch_id, job_id, input_payload, profile_version=profile_version)
        except Exception:
            self._discard_job(job_id)
            raise
        self._discard_inactive_batch_plan_jobs(batch_id, keep_job_id=job_id)
        return self.get_job(job_id)

    def _record_no_actionable_batch_plan(self, batch_id: str, batch: JsonObject, instruction: str | None) -> None:
        self._update_batch(
            batch_id,
            status="needs_human_review",
            fields={
                **self._batch_draft_artifact_reset_fields(),
                "optimization_plan": self._non_actionable_plan(batch, "暂无可用归因结果，不能生成可执行优化方案。", instruction),
                "optimization_plan_job_id": None,
                "optimization_plan_job": None,
                "optimization_plan_error": None,
            },
        )

    def _batch_plan_job_input_payload(
        self,
        batch: JsonObject,
        attributions: list[JsonObject],
        job_id: str,
        instruction: str | None,
    ) -> JsonObject:
        # #24-B/D：优化方案 grounding 的版本/仓库路径/target policy 同源于批次归属业务 Agent 的库。
        agent_id = self._string(batch.get("agent_id")) or "main-agent"
        payload = {
            "schema_version": "feedback-optimization-plan-input/v1",
            "job_id": job_id,
            "batch_id": batch["batch_id"],
            "feedback_case_ids": batch.get("feedback_case_ids") or [],
            "eval_case_ids": batch.get("eval_case_ids") or [],
            "source_refs": batch.get("source_refs") or [],
            "attribution_job_ids": self._unique_strings([self._string(item.get("_job_id") or item.get("attribution_job_id")) or "" for item in attributions]),
            "attribution_outputs": attributions,
            "eval_cases": [case for case in (self.find_eval_case(str(eval_case_id)) for eval_case_id in batch.get("eval_case_ids") or []) if case],
            "main_agent_version_id": self._current_agent_version_id(agent_id),
            **self._agent_git_paths_context(agent_id),
            "allowed_target_paths": ["<any-managed-main-workspace-relative-file>"],
            "target_policy": self._execution_target_policy(agent_id),
            "task": "generate_feedback_optimization_plan",
        }
        if instruction:
            payload["regeneration_instruction"] = instruction
        return payload

    def _create_batch_plan_agent_job(
        self,
        batch_id: str,
        job_id: str,
        input_payload: JsonObject,
        *,
        profile_version: Optional[JsonObject],
    ) -> JsonObject:
        spec = agent_job_spec("batch_plan")
        job = self.create_agent_job(
            job_id=job_id,
            job_type=spec.job_type,
            scope_kind="optimization_batch",
            scope_id=batch_id,
            profile_name=spec.profile_name,
            input_payload=input_payload,
            profile_version=profile_version,
        )
        self._update_batch(
            batch_id,
            status="optimization_plan_queued",
            fields={
                **self._batch_draft_artifact_reset_fields(),
                "optimization_plan": None,
                "optimization_plan_job_id": job_id,
                "optimization_plan_job": job,
                "optimization_plan_error": None,
            },
        )
        return job

    def _latest_active_batch_plan_job(self, batch_id: str) -> Optional[JsonObject]:
        for job in self.list_agent_jobs(job_type="batch_plan", scope_kind="optimization_batch", scope_id=batch_id, limit=10):
            if job.get("status") in BATCH_PLAN_ACTIVE_JOB_STATUSES:
                return job
        return None

    def _discard_inactive_batch_plan_jobs(self, batch_id: str, *, keep_job_id: str | None = None) -> None:
        for job in self.list_agent_jobs(job_type="batch_plan", scope_kind="optimization_batch", scope_id=batch_id, limit=1000):
            job_id = str(job.get("job_id") or "")
            if not job_id or job_id == keep_job_id:
                continue
            if job.get("status") in BATCH_PLAN_ACTIVE_JOB_STATUSES:
                continue
            self._discard_job(job_id)

    def complete_batch_plan_job(
        self,
        job_id: str,
        formatter_output: FeedbackOptimizationPlanFormatterOutput | JsonObject,
    ) -> Optional[JsonObject]:
        job = self.get_job(job_id)
        if not job:
            return None
        batch_id = self._job_batch_id(job)
        formatter_output_json = (
            output_model_payload(formatter_output) if isinstance(formatter_output, FeedbackOptimizationPlanFormatterOutput) else formatter_output
        )
        output = self._batch_plan_output_with_job_context(formatter_output_json, job)
        output_model, error = coerce_feedback_optimization_plan_output_model(output)
        raw_payload = output_model_payload(output_model) if output_model else formatter_output_json
        if not output_model:
            error_payload = self._job_error_payload(job, "SCHEMA_VALIDATION_FAILED", error or "invalid feedback optimization plan output")
            with self.Session.begin() as db:
                if not self._set_job_json_row(db, job_id, raw_output_json=raw_payload, error_json=error_payload):
                    return None
                self._append_job_update_row(db, job_id, status="schema_validating")
                completed_row = self._append_job_update_row(db, job_id, status="needs_human_review", completed_at=utc_now())
                completed = self._job_to_dict(completed_row) if completed_row else None
                if batch_id:
                    self._update_batch_row(
                        db,
                        batch_id,
                        status="needs_human_review",
                        fields={
                            "optimization_plan_job_id": job_id,
                            "optimization_plan_job": completed,
                            "optimization_plan_error": (completed or {}).get("error_json"),
                        },
                    )
            self._cleanup_job_tmp(job_id)
            return self.get_job(job_id)

        validated = output_model_payload(output_model)
        plan = self._normalize_batch_plan_output(validated, job)
        with self.Session.begin() as db:
            if not self._set_job_json_row(db, job_id, raw_output_json=raw_payload, validated_output_json=plan, error_json=None):
                return None
            self._append_job_update_row(db, job_id, status="schema_validating")
            completed_row = self._append_job_update_row(db, job_id, status="completed", completed_at=utc_now())
            completed = self._job_to_dict(completed_row) if completed_row else None
            if batch_id:
                self._update_batch_row(
                    db,
                    batch_id,
                    status=plan["status"],
                    fields={
                        "optimization_plan": plan,
                        "optimization_plan_job_id": job_id,
                        "optimization_plan_job": completed,
                        "optimization_plan_error": None,
                    },
                )
        self._cleanup_job_tmp(job_id)
        return self.get_job(job_id)

    def prepare_batch_plan_task_execution(
        self,
        batch_id: str,
        plan_task_id: str,
        *,
        comment: Optional[str] = None,
    ) -> Optional[JsonObject]:
        batch, plan, plan_task = self._batch_plan_task(batch_id, plan_task_id)
        if not batch or not plan or not plan_task:
            return None
        if plan_task.get("execution_kind") != "workspace_execution":
            raise ConflictError("Optimization plan task is not executable by execution-optimizer")
        target_path = self._string(plan_task.get("target_path"))
        if not target_path or not self._target_allowed(target_path):
            raise ConflictError("Optimization plan task target is not actionable")
        existing_task_id = self._string(plan_task.get("optimization_task_id"))
        existing_task = self.find_task(existing_task_id) if existing_task_id else None
        if existing_task:
            blocker = self.execution_job_queue_blocker(existing_task["optimization_task_id"])
            if blocker:
                raise ConflictError(blocker)
            updated = self._update_batch_plan_task(
                batch_id,
                plan_task_id,
                {
                    "status": existing_task.get("status") or "execution_planning",
                    "optimization_task_id": existing_task["optimization_task_id"],
                    "execution_job_id": existing_task.get("latest_execution_job_id"),
                    "latest_execution_job": existing_task.get("latest_execution_job"),
                    "applied_agent_version_id": existing_task.get("applied_agent_version_id"),
                },
                batch_status=str(existing_task.get("status") or "execution_planning"),
                top_level_fields={"optimization_task_id": existing_task["optimization_task_id"], "optimization_task": existing_task},
            )
            return {"batch": updated, "optimization_task": existing_task, "plan_task": self._plan_task_from_batch(updated, plan_task_id)}

        task = self.create_task_from_optimization_plan(
            batch=batch,
            plan=plan,
            plan_task=plan_task,
            execution_mode="manual_or_patch",
            comment=comment,
        )
        if not task:
            return None
        optimization_task_ids = self._unique_strings([*(batch.get("optimization_task_ids") or []), task["optimization_task_id"]])
        updated = self._update_batch_plan_task(
            batch_id,
            plan_task_id,
            {
                "status": "execution_planning",
                "optimization_task_id": task["optimization_task_id"],
            },
            batch_status="execution_planning",
            top_level_fields={
                "optimization_task_id": batch.get("optimization_task_id") or task["optimization_task_id"],
                "optimization_task": task,
                "optimization_task_ids": optimization_task_ids,
            },
        )
        return {"batch": updated, "optimization_task": task, "plan_task": self._plan_task_from_batch(updated, plan_task_id)}

    def notify_batch_plan_task_external(
        self,
        batch_id: str,
        plan_task_id: str,
        *,
        webhook_alias: str,
        sender: Optional[Callable[[JsonObject, JsonObject], JsonObject]] = None,
    ) -> Optional[JsonObject]:
        batch, plan, plan_task = self._batch_plan_task(batch_id, plan_task_id)
        if not batch or not plan or not plan_task:
            return None
        if plan_task.get("execution_kind") != "external_webhook":
            raise BusinessRuleViolation("Optimization plan task is not an external webhook task")
        item = self._upsert_external_governance_item_for_plan_task(batch, plan, plan_task)
        notified = self.notify_external_governance_item(item["external_item_id"], webhook_alias=webhook_alias, sender=sender)
        if not notified:
            return None
        updated = self._update_batch_plan_task(
            batch_id,
            plan_task_id,
            {
                "status": notified.get("status") or "notification_failed",
                "external_item_id": notified.get("external_item_id"),
                "latest_webhook_alias": notified.get("latest_webhook_alias"),
                "latest_notification": notified.get("latest_notification"),
            },
            batch_status=str(batch.get("status") or "pending_execution"),
        )
        return {"batch": updated, "external_item": notified, "plan_task": self._plan_task_from_batch(updated, plan_task_id)}

    def _non_actionable_plan(self, batch: JsonObject, reason: str, regeneration_instruction: Optional[str] = None) -> JsonObject:
        blocked_items = [
            {
                "schema_version": "feedback-optimization-blocked-item/v1",
                "blocked_item_id": f"fobi-{uuid.uuid4()}",
                "source_index": 0,
                "status": "blocked",
                "title": "未形成可执行优化任务",
                "target_type": "not_actionable",
                "target_path": None,
                "owner": "developer",
                "actionability": "needs_human_analysis",
                "recommendation": reason,
                "reason": reason,
                "feedback_case_ids": batch.get("feedback_case_ids") or [],
                "eval_case_ids": batch.get("eval_case_ids") or [],
                "attribution_job_ids": [],
                "created_at": utc_now(),
            }
        ]
        plan = {
            "schema_version": "feedback-optimization-plan/v1",
            "optimization_plan_id": f"fop-{uuid.uuid4()}",
            "batch_id": batch.get("batch_id"),
            "created_at": utc_now(),
            "status": "needs_human_review",
            "title": "不能生成可执行优化方案",
            "actionability": "needs_human_analysis",
            "target_type": "not_actionable",
            "target_path": None,
            "recommendation": reason,
            "expected_effect": "-",
            "validation": "-",
            "risk": "-",
            "source_refs": batch.get("source_refs") or [],
            "attribution_summaries": [],
            "tasks": [],
            "blocked_items": blocked_items,
            "task_summary": self._plan_task_summary([]),
            "blocked_summary": {"total": len(blocked_items)},
        }
        instruction = self._string(regeneration_instruction)
        if instruction:
            plan["regeneration_instruction"] = instruction
        return FeedbackOptimizationPlanRecord.model_validate(plan).to_payload()

    def _normalize_batch_plan_output(self, validated: JsonObject, job: JsonObject) -> JsonObject:
        input_json = job.get("input_json") if isinstance(job.get("input_json"), dict) else {}
        batch_id = self._string(input_json.get("batch_id")) or self._string(job.get("batch_id")) or self._string(job.get("scope_id")) or ""
        batch = self.find_optimization_batch(batch_id) or {}
        feedback_case_ids = self._string_list(batch.get("feedback_case_ids"))
        eval_case_ids = self._string_list(batch.get("eval_case_ids"))
        attribution_job_ids = self._string_list(input_json.get("attribution_job_ids")) or self._string_list(batch.get("attribution_job_ids"))
        plan = {
            **validated,
            "schema_version": "feedback-optimization-plan/v1",
            "optimization_plan_id": f"fop-{uuid.uuid4()}",
            "batch_id": batch_id,
            "created_at": utc_now(),
            "status": self._string(validated.get("status")) or "needs_human_review",
            "title": self._string(validated.get("title")) or "反馈批次优化方案",
            "recommendation": self._string(validated.get("recommendation")) or self._string(validated.get("summary")) or "根据归因结果生成优化任务。",
            "expected_effect": self._string(validated.get("expected_effect")) or "降低同类反馈再次出现的概率。",
            "validation": self._string(validated.get("validation")) or "使用本批次关联回归测试用例验证优化效果。",
            "risk": self._string(validated.get("risk")) or "需要关注优化后是否引入新的行为退化。",
            "source_refs": batch.get("source_refs") or input_json.get("source_refs") or [],
            "feedback_case_ids": feedback_case_ids,
            "eval_case_ids": eval_case_ids,
            "attribution_job_ids": attribution_job_ids,
            "attribution_summaries": self._normalize_batch_plan_attribution_summaries(
                self._batch_plan_attribution_summaries(input_json.get("attribution_outputs"))
            ),
            "optimization_plan_job_id": job["job_id"],
            "generated_by": agent_job_spec("batch_plan").profile_name,
            "tasks": self._sanitize_batch_plan_task_source_ids(
                validated.get("tasks"),
                feedback_case_ids=feedback_case_ids,
                eval_case_ids=eval_case_ids,
                attribution_job_ids=attribution_job_ids,
                id_fields=("plan_task_id",),
            ),
            "blocked_items": self._sanitize_batch_plan_task_source_ids(
                validated.get("blocked_items"),
                feedback_case_ids=feedback_case_ids,
                eval_case_ids=eval_case_ids,
                attribution_job_ids=attribution_job_ids,
                id_fields=("blocked_item_id", "plan_task_id"),
            ),
        }
        if input_json.get("regeneration_instruction"):
            plan["regeneration_instruction"] = input_json["regeneration_instruction"]
        return self._normalize_plan_task_collections(batch or plan, plan)

    def _batch_plan_output_with_job_context(self, formatter_output_json: JsonObject, job: JsonObject) -> JsonObject:
        input_json = job.get("input_json") if isinstance(job.get("input_json"), dict) else {}
        batch_id = self._string(input_json.get("batch_id")) or self._string(job.get("batch_id")) or self._string(job.get("scope_id")) or ""
        batch = self.find_optimization_batch(batch_id) or {}
        feedback_case_ids = self._string_list(batch.get("feedback_case_ids"))
        eval_case_ids = self._string_list(batch.get("eval_case_ids"))
        attribution_job_ids = self._string_list(input_json.get("attribution_job_ids")) or self._string_list(batch.get("attribution_job_ids"))
        output = dict(formatter_output_json)
        output.update(
            {
                "batch_id": batch_id,
                "source_refs": batch.get("source_refs") or input_json.get("source_refs") or [],
                "feedback_case_ids": feedback_case_ids,
                "eval_case_ids": eval_case_ids,
                "attribution_job_ids": attribution_job_ids,
                "attribution_summaries": self._batch_plan_attribution_summaries(input_json.get("attribution_outputs")),
            }
        )
        return output

    def _sanitize_batch_plan_task_source_ids(
        self,
        items: Any,
        *,
        feedback_case_ids: list[str],
        eval_case_ids: list[str],
        attribution_job_ids: list[str],
        id_fields: tuple[str, ...],
    ) -> list[JsonObject]:
        sanitized: list[JsonObject] = []
        allowed = {
            "feedback_case_ids": set(feedback_case_ids),
            "eval_case_ids": set(eval_case_ids),
            "attribution_job_ids": set(attribution_job_ids),
        }
        for item in items or []:
            if not isinstance(item, dict):
                continue
            normalized = dict(item)
            for field_name in id_fields:
                normalized.pop(field_name, None)
            normalized.pop("status", None)
            for field_name, allowed_values in allowed.items():
                selected = [value for value in self._string_list(normalized.get(field_name)) if value in allowed_values]
                if selected:
                    normalized[field_name] = selected
                else:
                    normalized.pop(field_name, None)
            sanitized.append(normalized)
        return sanitized

    def _batch_plan_attribution_summaries(self, attributions: Any) -> list[JsonObject]:
        summaries: list[JsonObject] = []
        for item in attributions or []:
            if not isinstance(item, dict):
                continue
            summaries.append(
                {
                    "attribution_job_id": item.get("_job_id") or item.get("attribution_job_id"),
                    "feedback_case_id": item.get("feedback_case_id"),
                    "problem_type": item.get("problem_type"),
                    "optimization_object_type": item.get("optimization_object_type"),
                    "actionability": item.get("actionability"),
                    "confidence": item.get("confidence"),
                    "rationale": item.get("rationale"),
                }
            )
        return summaries

    def _normalize_batch_plan_attribution_summaries(self, value: Any) -> list[JsonObject]:
        summaries: list[JsonObject] = []
        for item in value or []:
            if isinstance(item, str):
                summary = self._string(item)
                if summary:
                    summaries.append(FeedbackOptimizationAttributionSummaryRecord(summary=summary).model_dump(mode="json", exclude_none=True))
                continue
            if not isinstance(item, dict):
                continue
            attribution_job_id = self._string(item.get("attribution_job_id")) or self._string(item.get("job_id")) or self._string(item.get("_job_id"))
            summary = FeedbackOptimizationAttributionSummaryRecord(
                attribution_job_id=attribution_job_id,
                feedback_case_id=self._string(item.get("feedback_case_id")),
                problem_type=self._string(item.get("problem_type")),
                optimization_object_type=self._string(item.get("optimization_object_type")),
                actionability=self._string(item.get("actionability")),
                confidence=self._string(item.get("confidence")),
                rationale=self._string(item.get("rationale")),
                summary=self._string(item.get("summary")),
            )
            summaries.append(summary.model_dump(mode="json", exclude_none=True))
        return summaries

    def _build_batch_optimization_plan(
        self,
        batch: JsonObject,
        attributions: list[JsonObject],
        *,
        regeneration_instruction: Optional[str] = None,
    ) -> JsonObject:
        task_candidates = [self._build_batch_plan_task_or_blocked_item(batch, item, index) for index, item in enumerate(attributions)]
        tasks = [item for item in task_candidates if item.get("execution_kind") in {"workspace_execution", "external_webhook"}]
        blocked_items = [item for item in task_candidates if item.get("execution_kind") not in {"workspace_execution", "external_webhook"}]
        executable_tasks = tasks
        workspace_tasks = [item for item in tasks if item.get("execution_kind") == "workspace_execution"]
        primary = workspace_tasks[0] if workspace_tasks else executable_tasks[0] if executable_tasks else blocked_items[0]
        target_type = self._string(primary.get("target_type") or primary.get("optimization_object_type")) or "main_agent_claude_md"
        target_path = self._string(primary.get("target_path")) or self._plan_target_path(target_type)
        problem_types = self._unique_strings([self._string(item.get("problem_type")) or "" for item in attributions])
        confidence_values = self._unique_strings([self._string(item.get("confidence")) or "" for item in attributions])
        status = "pending_execution" if executable_tasks else "needs_human_review"
        actionability = self._string(primary.get("actionability")) or "needs_human_analysis"
        rationale_lines = [self._string(item.get("rationale")) for item in attributions if self._string(item.get("rationale"))]
        evidence_refs = [ref for item in attributions for ref in self._normalize_plan_evidence_refs(item.get("evidence_refs"))][:20]
        eval_case_ids = batch.get("eval_case_ids") or []
        recommendation = (
            "根据本批次归因结果，调整目标 Agent 配置，要求其在回答反馈暴露的场景时使用当前工作区的权威配置、"
            "工具调用和运行证据进行核查，避免依赖过期上下文或记忆回答。"
        )
        rationale = "\n\n".join(rationale_lines) or "归因结果未提供详细 rationale。"
        instruction = self._string(regeneration_instruction)
        if instruction:
            recommendation = f"{recommendation}\n\n开发人员补充要求：{instruction}"
            rationale = f"{rationale}\n\n生成补充要求：{instruction}"
        plan = {
            "schema_version": "feedback-optimization-plan/v1",
            "optimization_plan_id": f"fop-{uuid.uuid4()}",
            "batch_id": batch.get("batch_id"),
            "created_at": utc_now(),
            "status": status,
            "title": f"统筹 {len(batch.get('feedback_case_ids') or [])} 条反馈优化 {target_type}",
            "problem_types": problem_types,
            "confidence": "high" if "high" in confidence_values else "medium" if "medium" in confidence_values else "low",
            "actionability": actionability,
            "optimization_object_type": primary.get("target_type") or target_type,
            "target_type": primary.get("target_type") or target_type,
            "target_path": target_path,
            "recommendation": recommendation,
            "expected_effect": "提高反馈场景回答的完整性、可核查性和稳定性，降低同类反馈再次出现的概率。",
            "validation": (f"使用本批次关联的 {len(eval_case_ids)} 条回归测试用例逐条验证；所有用例均展示完整运行过程，失败项进入继续优化。"),
            "risk": "可能增加回答前的工具调用成本；如指令过强，简单问题可能产生不必要的检查步骤。",
            "source_refs": batch.get("source_refs") or [],
            "feedback_case_ids": batch.get("feedback_case_ids") or [],
            "eval_case_ids": eval_case_ids,
            "attribution_job_ids": self._unique_strings([self._string(item.get("_job_id")) or "" for item in attributions]),
            "attribution_summaries": self._batch_plan_attribution_summaries(attributions),
            "rationale": rationale,
            "evidence_refs": evidence_refs,
            "tasks": tasks,
            "task_summary": self._plan_task_summary(tasks),
            "blocked_items": blocked_items,
            "blocked_summary": {"total": len(blocked_items)},
        }
        if instruction:
            plan["regeneration_instruction"] = instruction
        return FeedbackOptimizationPlanRecord.model_validate(plan).to_payload()

    def _build_batch_plan_task_or_blocked_item(self, batch: JsonObject, attribution: JsonObject, index: int) -> JsonObject:
        target_type = self._string(attribution.get("optimization_object_type")) or "not_actionable"
        actionability = self._string(attribution.get("actionability")) or "needs_human_analysis"
        target_path = self._plan_target_path(target_type)
        boundary = attribution.get("responsibility_boundary") if isinstance(attribution.get("responsibility_boundary"), dict) else {}
        owner = self._string(boundary.get("owner")) or self._external_owner_for_target(target_type) or target_type
        rationale = self._string(attribution.get("rationale"))
        attribution_job_id = self._string(attribution.get("_job_id") or attribution.get("attribution_job_id"))
        feedback_case_id = self._string(attribution.get("feedback_case_id"))
        evidence_refs = self._normalize_plan_evidence_refs(attribution.get("evidence_refs"))
        task_context = self._task_context_from_attribution(batch, attribution, evidence_refs, owner)
        execution_kind = "blocked"
        status = "blocked"
        next_step = self._string(attribution.get("recommended_next_step"))
        reason = None if next_step in {"generate_proposal", "needs_human_review", "stop"} else next_step
        if actionability in {"direct_workspace_change", "workspace_config_change", "eval_only"} and target_path and self._target_allowed(target_path):
            execution_kind = "workspace_execution"
            status = "pending_execution"
            reason = None
        elif actionability == "external_guidance" or target_type in {"external_mcp_service", "soc_process", "mcp_description"}:
            if self._task_context_is_actionable_external(task_context):
                execution_kind = "external_webhook"
                status = "pending_notification"
                reason = None
            else:
                reason = "当前证据不足以定位具体外部对象、接口或问题 ID，不能生成可执行外部优化任务；请补充 trace 或重新归因。"
        elif not target_path:
            reason = reason or "归因结果未指向可由当前 workspace 受控修改的目标文件。"
        if execution_kind == "external_webhook":
            owner = self._external_owner_from_context(owner, task_context)
        title = self._plan_task_title(target_type, execution_kind, index, task_context)
        recommendation = self._plan_task_recommendation(target_type, execution_kind)
        analysis_summary = self._short_text(rationale, 420)
        evidence_summary = self._evidence_summary(evidence_refs)
        item = {
            "schema_version": "feedback-optimization-plan-task/v3",
            "plan_task_id": f"fopt-{uuid.uuid4()}",
            "source_index": index,
            "execution_kind": execution_kind,
            "status": status,
            "title": title,
            "description": self._plan_task_description(target_type, execution_kind, owner, target_path, task_context),
            "objective": self._plan_task_objective(target_type, execution_kind, task_context),
            "target_summary": self._plan_task_target_summary(target_type, execution_kind, owner, target_path),
            "task_context": task_context,
            "target_type": target_type,
            "target_path": target_path,
            "owner": owner,
            "actionability": actionability,
            "confidence": attribution.get("confidence"),
            "problem_type": attribution.get("problem_type"),
            "recommendation": recommendation,
            "recommended_actions": self._plan_task_actions(target_type, execution_kind, target_path, owner),
            "acceptance_criteria": self._plan_task_acceptance_criteria(execution_kind, target_path, task_context),
            "expected_effect": "降低同类反馈再次出现的概率。",
            "validation": "使用本批次关联回归用例验证优化效果。",
            "risk": "需要关注优化后是否引入额外工具调用成本或外部系统变更风险。",
            "analysis_summary": analysis_summary,
            "evidence_summary": evidence_summary,
            "evidence_refs": evidence_refs,
            "rationale": rationale,
            "reason": reason,
            "feedback_case_ids": [feedback_case_id] if feedback_case_id else batch.get("feedback_case_ids") or [],
            "eval_case_ids": batch.get("eval_case_ids") or [],
            "attribution_job_ids": [attribution_job_id] if attribution_job_id else [],
            "created_at": utc_now(),
        }
        if execution_kind == "blocked":
            return FeedbackOptimizationBlockedItemRecord.model_validate(
                {
                    **item,
                    "schema_version": "feedback-optimization-blocked-item/v1",
                    "blocked_item_id": f"fobi-{uuid.uuid4()}",
                    "reason": reason or "归因结果未形成可执行 workspace 任务或外部 webhook 任务。",
                }
            ).to_payload()
        return FeedbackOptimizationPlanTaskRecord.model_validate(item).to_payload()
