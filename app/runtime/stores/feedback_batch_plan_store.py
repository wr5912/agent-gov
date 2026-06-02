from __future__ import annotations

import uuid
from typing import Any, Callable, Optional

from ..agent_job_types import agent_job_spec
from ..agent_profiles import PROPOSAL_GENERATOR_PROFILE
from ..errors import BusinessRuleViolation, ConflictError
from ..feedback_job_flags import no_actionable_attributions, with_reused_existing
from ..feedback_schemas import validate_feedback_optimization_plan_output
from ..records.batch_plan_records import (
    FeedbackOptimizationBlockedItemRecord,
    FeedbackOptimizationPlanRecord,
    FeedbackOptimizationPlanTaskRecord,
)
from ..records.json_types import JsonObject
from ..runtime_db import utc_now


class FeedbackBatchPlanStoreMixin:
    """Store operations for batch optimization plan generation and approval."""

    def generate_batch_optimization_plan(self, batch_id: str, *, regeneration_instruction: Optional[str] = None) -> Optional[JsonObject]:
        batch = self.find_optimization_batch(batch_id)
        if not batch:
            return None
        self._assert_batch_plan_can_regenerate(batch)
        instruction = regeneration_instruction.strip() if isinstance(regeneration_instruction, str) else None
        instruction = instruction or None
        attributions = self._batch_attribution_outputs(batch)
        if not attributions:
            return self._update_batch(
                batch_id,
                status="needs_human_review",
                fields={"optimization_plan": self._non_actionable_plan(batch, "暂无可用归因结果，不能生成可执行优化方案。", instruction)},
            )
        plan = self._build_batch_optimization_plan(batch, attributions, regeneration_instruction=instruction)
        return self._update_batch(
            batch_id,
            status=plan["status"],
            fields={"optimization_plan": plan, "updated_at": utc_now()},
        )

    def create_batch_plan_job(
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
        attributions = self._batch_attribution_outputs(batch)
        if not attributions:
            self._update_batch(
                batch_id,
                status="needs_human_review",
                fields={
                    "optimization_plan": self._non_actionable_plan(batch, "暂无可用归因结果，不能生成可执行优化方案。", instruction),
                    "optimization_plan_job_id": None,
                    "optimization_plan_job": None,
                    "optimization_plan_error": None,
                },
            )
            return no_actionable_attributions(batch_id)

        feedback_case_id = self._latest(batch.get("feedback_case_ids")) or self._string(attributions[0].get("feedback_case_id")) or ""
        feedback_case = self.find_case(feedback_case_id) if feedback_case_id else None
        evidence_package_id = self._latest((feedback_case or {}).get("evidence_package_ids")) or f"batch-evidence-{batch_id}"
        job_id = f"fbp-{uuid.uuid4()}"
        input_payload = {
            "schema_version": "feedback-optimization-plan-input/v1",
            "job_id": job_id,
            "batch_id": batch_id,
            "feedback_case_ids": batch.get("feedback_case_ids") or [],
            "eval_case_ids": batch.get("eval_case_ids") or [],
            "source_refs": batch.get("source_refs") or [],
            "attribution_job_ids": self._unique_strings([self._string(item.get("_job_id") or item.get("attribution_job_id")) or "" for item in attributions]),
            "attribution_outputs": attributions,
            "eval_cases": [case for case in (self.find_eval_case(str(eval_case_id)) for eval_case_id in batch.get("eval_case_ids") or []) if case],
            "main_agent_version_id": self._current_agent_version_id(),
            "main_agent_manifest_path": str(self.data_dir / "agent-versions" / "main" / "current.json"),
            "allowed_target_paths": ["<any-managed-main-workspace-relative-file>"],
            "target_policy": self._execution_target_policy(),
            "task": "generate_feedback_optimization_plan",
        }
        if instruction:
            input_payload["regeneration_instruction"] = instruction
        try:
            spec = agent_job_spec("batch_plan")
            job = self.create_agent_job(
                job_id=job_id,
                job_type=spec.job_type,
                scope_kind="optimization_batch",
                scope_id=batch_id,
                profile_name=PROPOSAL_GENERATOR_PROFILE,
                input_payload=input_payload,
                output_schema_version=spec.output_schema_version,
                profile_version=profile_version,
            )
            self._update_batch(
                batch_id,
                status="optimization_plan_queued",
                fields={
                    "optimization_plan_job_id": job_id,
                    "optimization_plan_job": job,
                    "optimization_plan_error": None,
                },
            )
        except Exception:
            self._discard_job(job_id)
            raise
        return self.get_job(job_id)

    def complete_batch_plan_job(self, job_id: str, raw_output: JsonObject) -> Optional[JsonObject]:
        job = self.get_job(job_id)
        if not job:
            return None
        batch_id = self._job_batch_id(job)
        validated, error = validate_feedback_optimization_plan_output(raw_output)
        if not validated:
            error_payload = self._job_error_payload(job, "SCHEMA_VALIDATION_FAILED", error or "invalid feedback optimization plan output")
            with self.Session.begin() as db:
                if not self._set_job_json_row(db, job_id, raw_output_json=raw_output, error_json=error_payload):
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

        plan = self._normalize_batch_plan_output(validated, job)
        with self.Session.begin() as db:
            if not self._set_job_json_row(db, job_id, raw_output_json=raw_output, validated_output_json=plan, error_json=None):
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

    def approve_batch_optimization_plan(self, batch_id: str, *, comment: Optional[str] = None) -> Optional[JsonObject]:
        batch = self.find_optimization_batch(batch_id)
        plan = batch.get("optimization_plan") if isinstance((batch or {}).get("optimization_plan"), dict) else None
        if not batch or not plan or plan.get("status") != "pending_approval":
            return None
        target_path = self._string(plan.get("target_path"))
        if not target_path or not self._target_allowed(target_path):
            raise ConflictError("Optimization plan target is not actionable")
        proposal_id = self._string(plan.get("internal_proposal_id")) or f"prop-{uuid.uuid4()}"
        feedback_case_id = self._latest(batch.get("feedback_case_ids")) or ""
        now = utc_now()
        proposal_actionability = self._string(plan.get("actionability"))
        if proposal_actionability not in {"direct_workspace_change", "workspace_config_change"}:
            proposal_actionability = "direct_workspace_change"
        proposal = {
            "proposal_id": proposal_id,
            "created_at": now,
            "feedback_case_id": feedback_case_id,
            "proposal_job_id": f"batch-plan-{batch_id}",
            "status": "approved",
            "actionability": plan.get("actionability") or "direct_workspace_change",
            "target_type": plan.get("target_type") or plan.get("optimization_object_type") or "main_agent_claude_md",
            "target_path": target_path,
            "title": plan.get("title") or "反馈批次优化方案",
            "recommendation": plan.get("recommendation") or "",
            "expected_effect": plan.get("expected_effect") or "",
            "validation": plan.get("validation") or "",
            "risk": plan.get("risk") or "",
            "regeneration_instruction": plan.get("regeneration_instruction"),
            "requires_approval": True,
            "base_agent_version_id": self._current_agent_version_id(),
            "source_batch_id": batch_id,
            "source_feedback_case_ids": batch.get("feedback_case_ids") or [],
            "source_refs": batch.get("source_refs") or [],
            "latest_review": {
                "review_id": f"opr-{uuid.uuid4()}",
                "proposal_id": proposal_id,
                "created_at": now,
                "action": "approve",
                "status": "approved",
                "comment": comment,
                "source": "feedback_optimization_batch",
            },
        }
        with self.Session.begin() as db:
            db.merge(self._proposal_model_from_dict(proposal))
        task = self.create_task(proposal_id=proposal_id, execution_mode="manual_or_patch", comment=comment or f"由优化批次 {batch_id} 执行创建。")
        if not task:
            return None
        task = self._update_task_payload(
            task["optimization_task_id"],
            status=task["status"],
            fields={
                "source": "feedback_optimization_batch",
                "source_batch_id": batch_id,
                "feedback_case_ids": batch.get("feedback_case_ids") or [],
                "eval_case_ids": batch.get("eval_case_ids") or [],
            },
        ) or task
        approved_plan = {**plan, "status": "approved", "approved_at": utc_now(), "approval_comment": comment, "internal_proposal_id": proposal_id}
        updated_batch = self._update_batch(
            batch_id,
            status="execution_planning",
            fields={
                "optimization_plan": approved_plan,
                "internal_proposal_id": proposal_id,
                "optimization_task_id": task["optimization_task_id"],
                "optimization_task": task,
            },
        )
        return {"batch": updated_batch, "optimization_task": task}

    def reject_batch_optimization_plan(self, batch_id: str, *, comment: Optional[str] = None) -> Optional[JsonObject]:
        batch = self.find_optimization_batch(batch_id)
        plan = batch.get("optimization_plan") if isinstance((batch or {}).get("optimization_plan"), dict) else None
        if not batch or not plan:
            return None
        rejected_plan = {**plan, "status": "rejected", "rejected_at": utc_now(), "rejection_comment": comment}
        return self._update_batch(batch_id, status="rejected", fields={"optimization_plan": rejected_plan})

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

        proposal_id = self._string(plan_task.get("internal_proposal_id")) or f"prop-{uuid.uuid4()}"
        proposal = self._proposal_from_plan_task(
            batch=batch,
            plan=plan,
            plan_task=plan_task,
            proposal_id=proposal_id,
            target_path=target_path,
            comment=comment,
        )
        with self.Session.begin() as db:
            db.merge(self._proposal_model_from_dict(proposal))
        task = self.create_task(proposal_id=proposal_id, execution_mode="manual_or_patch", comment=comment or f"由优化批次 {batch_id} 的任务 {plan_task_id} 执行创建。")
        if not task:
            return None
        task = self._update_task_payload(
            task["optimization_task_id"],
            status=task["status"],
            fields={
                "source": "feedback_optimization_batch",
                "source_batch_id": batch_id,
                "source_plan_task_id": plan_task_id,
                "feedback_case_ids": plan_task.get("feedback_case_ids") or batch.get("feedback_case_ids") or [],
                "eval_case_ids": batch.get("eval_case_ids") or [],
            },
        ) or task
        optimization_task_ids = self._unique_strings([*(batch.get("optimization_task_ids") or []), task["optimization_task_id"]])
        updated = self._update_batch_plan_task(
            batch_id,
            plan_task_id,
            {
                "status": "execution_planning",
                "internal_proposal_id": proposal_id,
                "optimization_task_id": task["optimization_task_id"],
            },
            batch_status="execution_planning",
            top_level_fields={
                "internal_proposal_id": batch.get("internal_proposal_id") or proposal_id,
                "optimization_task_id": batch.get("optimization_task_id") or task["optimization_task_id"],
                "optimization_task": task,
                "optimization_task_ids": optimization_task_ids,
            },
        )
        return {"batch": updated, "optimization_task": task, "plan_task": self._plan_task_from_batch(updated, plan_task_id)}

    def _proposal_from_plan_task(
        self,
        *,
        batch: dict[str, Any],
        plan: dict[str, Any],
        plan_task: dict[str, Any],
        proposal_id: str,
        target_path: str,
        comment: Optional[str],
    ) -> JsonObject:
        batch_id = str(batch["batch_id"])
        plan_task_id = str(plan_task["plan_task_id"])
        now = utc_now()
        actionability = self._string(plan_task.get("actionability"))
        if actionability not in {"direct_workspace_change", "workspace_config_change"}:
            actionability = "direct_workspace_change"
        return {
            "proposal_id": proposal_id,
            "created_at": now,
            "feedback_case_id": self._latest(plan_task.get("feedback_case_ids") or batch.get("feedback_case_ids")) or "",
            "proposal_job_id": f"batch-plan-task-{batch_id}-{plan_task_id}",
            "status": "approved",
            "actionability": actionability,
            "target_type": plan_task.get("target_type") or "main_agent_claude_md",
            "target_path": target_path,
            "title": plan_task.get("title") or plan.get("title") or "反馈批次优化任务",
            "description": plan_task.get("description") or "",
            "objective": plan_task.get("objective") or "",
            "target_summary": plan_task.get("target_summary") or "",
            "task_context": plan_task.get("task_context") if isinstance(plan_task.get("task_context"), dict) else {},
            "recommendation": plan_task.get("recommendation") or plan.get("recommendation") or "",
            "recommended_actions": plan_task.get("recommended_actions") or [],
            "acceptance_criteria": plan_task.get("acceptance_criteria") or [],
            "expected_effect": plan_task.get("expected_effect") or plan.get("expected_effect") or "",
            "validation": plan_task.get("validation") or plan.get("validation") or "",
            "risk": plan_task.get("risk") or plan.get("risk") or "",
            "analysis_summary": plan_task.get("analysis_summary") or "",
            "evidence_summary": plan_task.get("evidence_summary") or "",
            "evidence_refs": plan_task.get("evidence_refs") or [],
            "regeneration_instruction": plan.get("regeneration_instruction"),
            "requires_approval": True,
            "base_agent_version_id": self._current_agent_version_id(),
            "source_batch_id": batch_id,
            "source_plan_task_id": plan_task_id,
            "source_feedback_case_ids": plan_task.get("feedback_case_ids") or batch.get("feedback_case_ids") or [],
            "source_refs": batch.get("source_refs") or [],
            "latest_review": {
                "review_id": f"opr-{uuid.uuid4()}",
                "proposal_id": proposal_id,
                "created_at": now,
                "action": "approve",
                "status": "approved",
                "comment": comment,
                "source": "feedback_optimization_plan_task",
            },
        }

    def notify_batch_plan_task_external(
        self,
        batch_id: str,
        plan_task_id: str,
        *,
        webhook_alias: str,
        sender: Optional[Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]] = None,
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
            batch_status=str(batch.get("status") or "pending_approval"),
        )
        return {"batch": updated, "external_item": notified, "plan_task": self._plan_task_from_batch(updated, plan_task_id)}


    def _non_actionable_plan(self, batch: dict[str, Any], reason: str, regeneration_instruction: Optional[str] = None) -> JsonObject:
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

    def _normalize_batch_plan_output(self, validated: dict[str, Any], job: dict[str, Any]) -> JsonObject:
        input_json = job.get("input_json") if isinstance(job.get("input_json"), dict) else {}
        batch_id = self._string(validated.get("batch_id")) or self._string(input_json.get("batch_id")) or ""
        batch = self.find_optimization_batch(batch_id) or {}
        plan = {
            **validated,
            "schema_version": "feedback-optimization-plan/v1",
            "source_output_schema_version": validated.get("schema_version"),
            "optimization_plan_id": self._string(validated.get("optimization_plan_id")) or f"fop-{uuid.uuid4()}",
            "batch_id": batch_id,
            "created_at": self._string(validated.get("created_at")) or utc_now(),
            "status": self._string(validated.get("status")) or "needs_human_review",
            "title": self._string(validated.get("title")) or "反馈批次优化方案",
            "recommendation": self._string(validated.get("recommendation")) or self._string(validated.get("summary")) or "根据归因结果生成优化任务。",
            "expected_effect": self._string(validated.get("expected_effect")) or "降低同类反馈再次出现的概率。",
            "validation": self._string(validated.get("validation")) or "使用本批次关联回归测试用例验证优化效果。",
            "risk": self._string(validated.get("risk")) or "需要关注优化后是否引入新的行为退化。",
            "source_refs": validated.get("source_refs") or batch.get("source_refs") or [],
            "feedback_case_ids": self._string_list(validated.get("feedback_case_ids")) or self._string_list(batch.get("feedback_case_ids")),
            "eval_case_ids": self._string_list(validated.get("eval_case_ids")) or self._string_list(batch.get("eval_case_ids")),
            "attribution_job_ids": self._string_list(validated.get("attribution_job_ids")) or self._string_list(input_json.get("attribution_job_ids")),
            "attribution_summaries": validated.get("attribution_summaries") or self._batch_plan_attribution_summaries(input_json.get("attribution_outputs")),
            "optimization_plan_job_id": job["job_id"],
            "generated_by": PROPOSAL_GENERATOR_PROFILE,
        }
        if input_json.get("regeneration_instruction") and not plan.get("regeneration_instruction"):
            plan["regeneration_instruction"] = input_json["regeneration_instruction"]
        return self._normalize_plan_task_collections(batch or plan, plan)

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

    def _build_batch_optimization_plan(
        self,
        batch: dict[str, Any],
        attributions: list[dict[str, Any]],
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
        status = "pending_approval" if executable_tasks else "needs_human_review"
        actionability = self._string(primary.get("actionability")) or "needs_human_analysis"
        rationale_lines = [self._string(item.get("rationale")) for item in attributions if self._string(item.get("rationale"))]
        evidence_refs = [
            ref
            for item in attributions
            for ref in (item.get("evidence_refs") or [])
            if isinstance(ref, dict)
        ][:20]
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
            "validation": (
                f"使用本批次关联的 {len(eval_case_ids)} 条回归测试用例逐条验证；所有用例均展示完整运行过程，"
                "失败项进入继续优化。"
            ),
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

    def _build_batch_plan_task_or_blocked_item(self, batch: dict[str, Any], attribution: dict[str, Any], index: int) -> JsonObject:
        target_type = self._string(attribution.get("optimization_object_type")) or "not_actionable"
        actionability = self._string(attribution.get("actionability")) or "needs_human_analysis"
        target_path = self._plan_target_path(target_type)
        boundary = attribution.get("responsibility_boundary") if isinstance(attribution.get("responsibility_boundary"), dict) else {}
        owner = self._string(boundary.get("owner")) or self._external_owner_for_target(target_type) or target_type
        rationale = self._string(attribution.get("rationale"))
        attribution_job_id = self._string(attribution.get("_job_id") or attribution.get("attribution_job_id"))
        feedback_case_id = self._string(attribution.get("feedback_case_id"))
        evidence_refs = [dict(ref) for ref in attribution.get("evidence_refs") or [] if isinstance(ref, dict)]
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
            "schema_version": "feedback-optimization-plan-task/v2",
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
