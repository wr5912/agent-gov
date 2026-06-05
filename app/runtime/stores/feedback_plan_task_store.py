from __future__ import annotations

import re
import uuid
from typing import Any, Optional

from ..json_types import JsonObject
from ..records.batch_plan_records import (
    FeedbackOptimizationBlockedItemRecord,
    FeedbackOptimizationEvidenceRefRecord,
    FeedbackOptimizationPlanRecord,
    FeedbackOptimizationPlanTaskRecord,
    FeedbackOptimizationTaskContextRecord,
)
from ..runtime_db import FeedbackOptimizationBatchModel, utc_now


class FeedbackPlanTaskStoreMixin:
    """Normalization helpers for batch optimization plan tasks and external task context."""

    def _normalize_plan_task_collections(self, batch: JsonObject, plan: JsonObject) -> JsonObject:
        raw_tasks = [dict(item) for item in plan.get("tasks") or [] if isinstance(item, dict)]
        executable_tasks = [
            self._normalize_plan_task(batch, plan, item)
            for item in raw_tasks
            if item.get("execution_kind") in {"workspace_execution", "external_webhook", "internal_action"}
        ]
        blocked_items = [self._normalize_blocked_item(batch, plan, dict(item)) for item in plan.get("blocked_items") or [] if isinstance(item, dict)]
        blocked_items.extend(self._blocked_items_from_tasks(batch, plan, raw_tasks))
        if not raw_tasks and not blocked_items:
            legacy_item = self._legacy_plan_task_or_blocked_item(batch, plan)
            if legacy_item.get("execution_kind") in {"workspace_execution", "external_webhook", "internal_action"}:
                executable_tasks.append(self._normalize_plan_task(batch, plan, legacy_item))
            else:
                blocked_items.append(self._normalize_blocked_item(batch, plan, legacy_item))
        normalized_plan = {
            **plan,
            "schema_version": self._string(plan.get("schema_version")) or "feedback-optimization-plan/v1",
            "optimization_plan_id": self._string(plan.get("optimization_plan_id")) or f"fop-legacy-{self._string(batch.get('batch_id')) or 'unknown'}",
            "batch_id": plan.get("batch_id") or batch.get("batch_id"),
            "created_at": plan.get("created_at") or batch.get("created_at"),
            "status": self._string(plan.get("status")) or "needs_human_review",
            "title": self._string(plan.get("title")) or "反馈批次优化方案",
            "tasks": executable_tasks,
            "blocked_items": blocked_items,
            "task_summary": self._plan_task_summary(executable_tasks),
            "blocked_summary": {"total": len(blocked_items)},
        }
        return FeedbackOptimizationPlanRecord.model_validate(normalized_plan).to_payload()

    def _normalize_plan_task(self, batch: JsonObject, plan: JsonObject, item: JsonObject) -> JsonObject:
        target_type = self._string(item.get("target_type")) or self._string(plan.get("target_type")) or "not_actionable"
        execution_kind = self._string(item.get("execution_kind")) or "workspace_execution"
        status = self._string(item.get("status")) or ("pending_notification" if execution_kind == "external_webhook" else "pending_execution")
        internal_action = self._string(item.get("internal_action"))
        if execution_kind == "internal_action" and target_type == "not_actionable":
            target_type = "eval_case"
        if execution_kind == "internal_action" and not internal_action and target_type == "eval_case":
            internal_action = "promote_eval_cases"
        target_path = self._string(item.get("target_path")) or None
        owner = self._string(item.get("owner")) or self._external_owner_for_target(target_type) or target_type
        if execution_kind == "internal_action":
            owner = "feedback_optimizer"
        rationale = self._string(item.get("rationale")) or self._string(plan.get("rationale"))
        analysis_summary = self._string(item.get("analysis_summary")) or self._short_text(rationale, 420)
        evidence_refs = self._normalize_plan_evidence_refs(item.get("evidence_refs"))
        evidence_summary = self._string(item.get("evidence_summary")) or self._evidence_summary(evidence_refs)
        task_context = self._normalize_task_context(item.get("task_context"), rationale, owner)
        if execution_kind == "external_webhook":
            owner = self._external_owner_from_context(owner, task_context)
        target_summary = self._string(item.get("target_summary"))
        if execution_kind == "external_webhook" and (
            not target_summary or target_type in target_summary or "external-mcp-service" in target_summary or "对应外部系统" in target_summary
        ):
            target_summary = self._plan_task_target_summary(target_type, execution_kind, owner, target_path)
        if execution_kind == "internal_action" and not target_summary:
            target_summary = self._plan_task_target_summary(target_type, execution_kind, owner, target_path)
        normalized = {
            **item,
            "schema_version": "feedback-optimization-plan-task/v3",
            "plan_task_id": self._string(item.get("plan_task_id")) or f"fopt-{uuid.uuid4()}",
            "execution_kind": execution_kind,
            "status": status,
            "internal_action": internal_action,
            "owner": owner,
            "actionability": self._string(item.get("actionability"))
            or ("regression_asset_governance" if execution_kind == "internal_action" else "needs_human_analysis"),
            "title": self._clean_plan_task_title(item.get("title"), target_type, execution_kind, int(item.get("source_index") or 0), task_context),
            "description": self._clean_plan_task_description(item.get("description"), target_type, execution_kind, owner, target_path, task_context),
            "objective": self._clean_plan_task_objective(item.get("objective"), target_type, execution_kind, task_context),
            "target_summary": target_summary,
            "recommended_actions": self._string_list(item.get("recommended_actions"))
            or self._plan_task_actions(target_type, execution_kind, target_path, owner),
            "acceptance_criteria": self._clean_plan_task_acceptance_criteria(item.get("acceptance_criteria"), execution_kind, target_path, task_context),
            "task_context": task_context,
            "analysis_summary": analysis_summary,
            "evidence_summary": evidence_summary,
            "evidence_refs": evidence_refs,
            "recommendation": self._clean_plan_task_recommendation(item.get("recommendation"), target_type, execution_kind),
            "feedback_case_ids": self._string_list(item.get("feedback_case_ids")) or self._string_list(batch.get("feedback_case_ids")),
            "eval_case_ids": self._string_list(item.get("eval_case_ids")) or self._string_list(batch.get("eval_case_ids")),
            "attribution_job_ids": self._string_list(item.get("attribution_job_ids")) or self._string_list(plan.get("attribution_job_ids")),
        }
        return FeedbackOptimizationPlanTaskRecord.model_validate(normalized).to_payload()

    def _normalize_blocked_item(self, batch: JsonObject, plan: JsonObject, item: JsonObject) -> JsonObject:
        target_type = self._string(item.get("target_type")) or "not_actionable"
        evidence_refs = self._normalize_plan_evidence_refs(item.get("evidence_refs"))
        rationale = self._string(item.get("rationale")) or self._string(plan.get("rationale"))
        return FeedbackOptimizationBlockedItemRecord.model_validate(
            {
                **item,
                "schema_version": "feedback-optimization-blocked-item/v1",
                "blocked_item_id": self._string(item.get("blocked_item_id")) or self._string(item.get("plan_task_id")) or f"fobi-{uuid.uuid4()}",
                "status": "blocked",
                "title": self._string(item.get("title")) or "未形成可执行优化任务",
                "target_type": target_type,
                "reason": self._string(item.get("reason")) or rationale or "该项不能自动执行，也没有可通知的外部目标。",
                "analysis_summary": self._string(item.get("analysis_summary")) or self._short_text(rationale, 420),
                "evidence_summary": self._string(item.get("evidence_summary")) or self._evidence_summary(evidence_refs),
                "feedback_case_ids": self._string_list(item.get("feedback_case_ids")) or self._string_list(batch.get("feedback_case_ids")),
                "eval_case_ids": self._string_list(item.get("eval_case_ids")) or self._string_list(batch.get("eval_case_ids")),
                "attribution_job_ids": self._string_list(item.get("attribution_job_ids")) or self._string_list(plan.get("attribution_job_ids")),
            }
        ).to_payload()

    def _blocked_items_from_tasks(self, batch: JsonObject, plan: JsonObject, tasks: list[JsonObject]) -> list[JsonObject]:
        blocked: list[JsonObject] = []
        for item in tasks:
            if item.get("execution_kind") in {"workspace_execution", "external_webhook", "internal_action"}:
                continue
            blocked.append(self._normalize_blocked_item(batch, plan, item))
        return blocked

    def _normalize_plan_evidence_refs(self, value: Any) -> list[JsonObject]:
        refs: list[JsonObject] = []
        for item in value or []:
            if not isinstance(item, dict):
                continue
            ref = dict(item)
            ref_id = self._string(ref.get("id") or ref.get("path") or ref.get("file"))
            if not ref_id:
                continue
            ref["type"] = self._string(ref.get("type")) or "evidence_file"
            ref["id"] = ref_id
            ref["reason"] = self._string(ref.get("reason") or ref.get("description")) or ""
            refs.append(FeedbackOptimizationEvidenceRefRecord.model_validate(ref).to_payload())
        return refs

    def _legacy_plan_task_or_blocked_item(self, batch: JsonObject, plan: JsonObject) -> JsonObject:
        target_type = self._string(plan.get("target_type") or plan.get("optimization_object_type")) or "not_actionable"
        target_path = self._string(plan.get("target_path")) or None
        actionability = self._string(plan.get("actionability")) or "needs_human_analysis"
        execution_kind = "blocked"
        status = "blocked"
        reason = self._string(plan.get("no_action_reason")) or self._string(plan.get("recommendation"))
        if actionability in {"direct_workspace_change", "workspace_config_change", "eval_only"} and target_path and self._target_allowed(target_path):
            execution_kind = "workspace_execution"
            status = "pending_execution"
            reason = None
        elif actionability == "external_guidance" or target_type in {"external_mcp_service", "soc_process", "mcp_description"}:
            execution_kind = "external_webhook"
            status = "pending_notification"
            reason = None
        item_id = f"fopt-legacy-{self._string(plan.get('optimization_plan_id')) or self._string(batch.get('batch_id'))}"
        item = {
            "schema_version": "feedback-optimization-plan-task/v1",
            "plan_task_id": item_id,
            "source_index": 0,
            "execution_kind": execution_kind,
            "status": status,
            "title": plan.get("title") or "历史优化方案任务",
            "target_type": target_type,
            "target_path": target_path,
            "owner": self._external_owner_for_target(target_type) or target_type,
            "actionability": actionability,
            "confidence": plan.get("confidence"),
            "problem_type": self._latest(plan.get("problem_types") or []),
            "recommendation": plan.get("recommendation") or "",
            "expected_effect": plan.get("expected_effect") or "",
            "validation": plan.get("validation") or "",
            "risk": plan.get("risk") or "",
            "rationale": plan.get("rationale") or "",
            "reason": reason,
            "feedback_case_ids": plan.get("feedback_case_ids") or batch.get("feedback_case_ids") or [],
            "eval_case_ids": plan.get("eval_case_ids") or batch.get("eval_case_ids") or [],
            "attribution_job_ids": plan.get("attribution_job_ids") or [],
            "created_at": plan.get("created_at") or batch.get("created_at"),
        }
        if execution_kind == "blocked":
            return FeedbackOptimizationBlockedItemRecord.model_validate(
                {
                    **item,
                    "schema_version": "feedback-optimization-blocked-item/v1",
                    "blocked_item_id": item_id,
                    "reason": reason or "历史方案未形成可执行任务。",
                }
            ).to_payload()
        return FeedbackOptimizationPlanTaskRecord.model_validate(item).to_payload()

    def _batch_plan_task(self, batch_id: str, plan_task_id: str) -> tuple[Optional[JsonObject], Optional[JsonObject], Optional[JsonObject]]:
        batch = self.find_optimization_batch(batch_id)
        plan = batch.get("optimization_plan") if isinstance((batch or {}).get("optimization_plan"), dict) else None
        if not batch or not plan:
            return None, None, None
        task = self._plan_task_from_batch(batch, plan_task_id)
        return batch, plan, task

    def _plan_task_from_batch(self, batch: Optional[JsonObject], plan_task_id: str) -> Optional[JsonObject]:
        plan = batch.get("optimization_plan") if isinstance((batch or {}).get("optimization_plan"), dict) else None
        for task in (plan or {}).get("tasks") or []:
            if isinstance(task, dict) and self._string(task.get("plan_task_id")) == plan_task_id:
                return FeedbackOptimizationPlanTaskRecord.model_validate(dict(task)).to_payload()
        return None

    def _update_batch_plan_task(
        self,
        batch_id: str,
        plan_task_id: str,
        updates: JsonObject,
        *,
        batch_status: Optional[str] = None,
        top_level_fields: Optional[JsonObject] = None,
    ) -> Optional[JsonObject]:
        with self.Session.begin() as db:
            if not self._update_batch_plan_task_row(
                db,
                batch_id,
                plan_task_id,
                updates,
                batch_status=batch_status,
                top_level_fields=top_level_fields,
            ):
                return None
        return self.find_optimization_batch(batch_id)

    def _update_batch_plan_task_row(
        self,
        db: Any,
        batch_id: str,
        plan_task_id: str,
        updates: JsonObject,
        *,
        batch_status: Optional[str] = None,
        top_level_fields: Optional[JsonObject] = None,
    ) -> Optional[FeedbackOptimizationBatchModel]:
        row = db.get(FeedbackOptimizationBatchModel, batch_id)
        if not row:
            return None
        batch = self._batch_payload_snapshot(row)
        plan = batch.get("optimization_plan") if isinstance(batch.get("optimization_plan"), dict) else None
        if not plan:
            return None
        tasks = [dict(item) for item in plan.get("tasks") or [] if isinstance(item, dict)]
        changed = False
        now = utc_now()
        for index, task in enumerate(tasks):
            if self._string(task.get("plan_task_id")) == plan_task_id:
                tasks[index] = {**task, **updates, "updated_at": now}
                changed = True
                break
        if not changed:
            return None
        next_plan = {**plan, "tasks": tasks, "updated_at": now, "task_summary": self._plan_task_summary(tasks)}
        fields = {"optimization_plan": next_plan, **(top_level_fields or {})}
        return self._update_batch_row(db, batch_id, status=batch_status or str(batch.get("status") or "pending_approval"), fields=fields)

    def _plan_task_summary(self, tasks: list[JsonObject]) -> JsonObject:
        summary: JsonObject = {"total": len(tasks), "workspace_execution": 0, "external_webhook": 0, "internal_action": 0}
        for task in tasks:
            kind = self._string(task.get("execution_kind"))
            if kind not in {"workspace_execution", "external_webhook", "internal_action"}:
                continue
            summary[kind] = int(summary.get(kind) or 0) + 1
        return summary

    def _plan_task_title(self, target_type: str, execution_kind: str, index: int, task_context: Optional[JsonObject] = None) -> str:
        if execution_kind == "workspace_execution":
            label = {
                "main_agent_claude_md": "优化 Agent 工作区指令",
                "instruction_gap": "补充 Agent 行为约束",
                "skill_gap": "优化 Agent 技能说明",
                "mcp_config": "优化 MCP 配置",
                "eval_case": "补充回归测试用例",
            }.get(target_type, "优化 Agent 工作区配置")
            return f"任务 {index + 1}: {label}"
        if execution_kind == "external_webhook":
            context_title = self._external_task_title_from_context(task_context or {})
            if context_title:
                return f"任务 {index + 1}: {context_title}"
            label = {
                "external_mcp_service": "补齐 MCP 服务返回数据能力",
                "mcp_description": "完善 MCP 服务描述",
                "soc_process": "优化 SOC 处置流程",
            }.get(target_type, "处理外部系统优化事项")
            return f"任务 {index + 1}: {label}"
        if execution_kind == "internal_action":
            label = {
                "eval_case": "晋级回归资产",
            }.get(target_type, "执行内部治理动作")
            return f"任务 {index + 1}: {label}"
        return f"阻塞项 {index + 1}: 未形成可执行优化任务"

    def _plan_task_description(
        self,
        target_type: str,
        execution_kind: str,
        owner: str,
        target_path: Optional[str],
        task_context: Optional[JsonObject] = None,
    ) -> str:
        if execution_kind == "workspace_execution":
            return "根据反馈归因结果，调整受管 workspace 中的 Agent 配置、指令或用例，让 Agent 在同类场景中按当前证据和配置作答。"
        if execution_kind == "external_webhook":
            context_description = self._external_task_description_from_context(task_context or {})
            if context_description:
                return context_description
            owner_label = owner if owner and owner != target_type else "对应外部系统"
            return f"将反馈暴露的问题整理为外部系统优化任务，派发给 {owner_label} 处理。"
        if execution_kind == "internal_action":
            return "将本批次候选评估用例晋级为已批准的回归资产，纳入后续版本回归验证。"
        return "该项没有形成可执行 workspace 任务或明确的外部系统派发目标。"

    def _plan_task_objective(self, target_type: str, execution_kind: str, task_context: Optional[JsonObject] = None) -> str:
        if execution_kind == "workspace_execution":
            return "通过修改 workspace 受管配置或指令，降低同类反馈再次出现的概率。"
        if execution_kind == "external_webhook":
            context_objective = self._external_task_objective_from_context(task_context or {})
            if context_objective:
                return context_objective
            return "推动对应外部系统补齐能力、数据或流程，使 Agent 后续可获得可靠输入。"
        if execution_kind == "internal_action":
            return "把本批次验证同类问题所需的评估用例纳入长期回归资产，防止修复效果失守。"
        return "补充更多上下文后重新归因或重新生成优化方案。"

    def _plan_task_target_summary(self, target_type: str, execution_kind: str, owner: str, target_path: Optional[str]) -> str:
        if execution_kind == "workspace_execution":
            return f"workspace:{target_path or target_type}"
        if execution_kind == "external_webhook":
            return f"external:{owner or target_type}"
        if execution_kind == "internal_action":
            return "internal:promote_eval_cases"
        return f"blocked:{target_type}"

    def _plan_task_actions(self, target_type: str, execution_kind: str, target_path: Optional[str], owner: str) -> list[str]:
        if execution_kind == "workspace_execution":
            return [
                f"由 execution-optimizer 读取已审批任务并生成针对 {target_path or target_type} 的受控执行方案。",
                "后端校验执行方案的目标路径、操作类型和文件哈希后应用变更。",
                "应用成功后创建 Agent 新版本，并使用本批次回归用例验证效果。",
            ]
        if execution_kind == "external_webhook":
            return [
                f"通过已配置 Webhook 将任务派发给 {owner or target_type}。",
                "Webhook payload 携带反馈、归因、测试用例、验收标准和证据摘要。",
                "本阶段以通知成功作为派发完成状态，不等待外部系统回调完成。",
            ]
        if execution_kind == "internal_action":
            return [
                "后端校验任务列出的 eval_case_ids 全部属于当前优化批次。",
                "将候选用例晋级为 active/approved 回归资产，并记录 revision 与 governance event。",
                "刷新批次优化方案中的任务状态，后续回归计划可选择这些资产执行验证。",
            ]
        return ["重新补充反馈上下文后运行归因，或调整优化方案生成提示。"]

    def _plan_task_acceptance_criteria(
        self,
        execution_kind: str,
        target_path: Optional[str],
        task_context: Optional[JsonObject] = None,
    ) -> list[str]:
        if execution_kind == "workspace_execution":
            return [
                "关联回归测试用例通过，反馈指出的同类问题不再复现。",
                "Agent 在同类场景中能按优化目标使用当前配置、工具或数据完成回答。",
                "优化后回答满足反馈用例的期望行为，且不引入新的明显退化。",
            ]
        if execution_kind == "external_webhook":
            context_criteria = self._external_acceptance_criteria_from_context(task_context or {})
            if context_criteria:
                return context_criteria
            return [
                "外部系统在同类请求中提供 Agent 完成任务所需的完整、可靠输入。",
                "Agent 使用外部系统返回结果后，能完整回答反馈指出的缺失或错误内容。",
                "关联回归测试用例通过，反馈指出的问题不再复现。",
            ]
        if execution_kind == "internal_action":
            return [
                "任务列出的评估用例状态均为 active。",
                "任务列出的评估用例 promotion_status 均为 approved。",
                "每个被晋级用例都有 revision 和 governance event 审计记录。",
            ]
        return ["阻塞原因清晰可见，开发人员可据此重新归因或重新生成优化方案。"]

    def _clean_plan_task_title(
        self,
        value: Any,
        target_type: str,
        execution_kind: str,
        index: int,
        task_context: Optional[JsonObject] = None,
    ) -> str:
        title = self._string(value)
        generic_fragments = ("补齐 MCP 服务返回数据能力", "外部系统优化", "external_mcp_service", "对应外部系统")
        if not title or target_type in title or any(fragment in title for fragment in generic_fragments):
            return self._plan_task_title(target_type, execution_kind, index, task_context)
        return title

    def _clean_plan_task_description(
        self,
        value: Any,
        target_type: str,
        execution_kind: str,
        owner: str,
        target_path: Optional[str],
        task_context: Optional[JsonObject] = None,
    ) -> str:
        description = self._string(value)
        generic_fragments = ("对应外部系统", "外部系统优化任务", "external_mcp_service")
        text_quality_markers = ("时 返回", "时 时", "时 的")
        if (
            not description
            or target_type in description
            or any(fragment in description for fragment in generic_fragments)
            or any(marker in description for marker in text_quality_markers)
        ):
            return self._plan_task_description(target_type, execution_kind, owner, target_path, task_context)
        return description

    def _clean_plan_task_objective(
        self,
        value: Any,
        target_type: str,
        execution_kind: str,
        task_context: Optional[JsonObject] = None,
    ) -> str:
        objective = self._string(value)
        generic_fragments = ("对应外部系统", "external_mcp_service")
        if not objective or target_type in objective or any(fragment in objective for fragment in generic_fragments):
            return self._plan_task_objective(target_type, execution_kind, task_context)
        return objective

    def _clean_plan_task_acceptance_criteria(
        self,
        value: Any,
        execution_kind: str,
        target_path: Optional[str],
        task_context: Optional[JsonObject] = None,
    ) -> list[str]:
        criteria = self._string_list(value)
        process_markers = ("Webhook", "2xx", "payload", "notification_failed", "execution-optimizer", "版本快照", "目标文件", "派发")
        generic_markers = ("外部系统在同类请求中", "完整、可靠输入")
        text_quality_markers = ("时 时", "时 的", "时 返回")
        if not criteria or any(any(marker in item for marker in (*process_markers, *generic_markers, *text_quality_markers)) for item in criteria):
            return self._plan_task_acceptance_criteria(execution_kind, target_path, task_context)
        return criteria

    def _task_context_from_attribution(
        self,
        batch: JsonObject,
        attribution: JsonObject,
        evidence_refs: list[JsonObject],
        owner: str,
    ) -> JsonObject:
        feedback_case_id = self._string(attribution.get("feedback_case_id"))
        feedback_case = self.find_case(feedback_case_id) if feedback_case_id else None
        text_parts = [
            self._string(attribution.get("rationale")) or "",
            self._string(attribution.get("recommended_next_step")) or "",
            self._string((attribution.get("responsibility_boundary") or {}).get("reason"))
            if isinstance(attribution.get("responsibility_boundary"), dict)
            else "",
            " ".join(self._string(ref.get("reason")) or "" for ref in evidence_refs),
        ]
        text = "\n".join(part for part in text_parts if part)
        tool_matches = list(re.finditer(r"\bmcp__([A-Za-z0-9_-]+)__([A-Za-z0-9_]+)__([A-Za-z0-9_]+)\b", text))
        tool_names = self._unique_strings(match.group(0) for match in tool_matches)
        mcp_server = tool_matches[0].group(1) if tool_matches else self._server_from_owner(owner)
        tool_operation = tool_matches[0].group(3) if tool_matches else ""
        api_info = self._api_info_from_tool_operation(tool_operation)
        alert_ids = self._unique_strings(
            [
                *(re.findall(r"\balert[-_][A-Za-z0-9]+\b", text, flags=re.IGNORECASE)),
                *self._string_list((feedback_case or {}).get("alert_ids")),
            ]
        )
        case_ids = self._unique_strings(
            [
                *(re.findall(r"\bcase[-_][A-Za-z0-9]+\b", text, flags=re.IGNORECASE)),
                *self._string_list((feedback_case or {}).get("case_ids")),
            ]
        )
        asset_ids = self._unique_strings(re.findall(r"\basset[-_][A-Za-z0-9]+\b", text, flags=re.IGNORECASE))
        seed_values = self._unique_strings(f"seed={value}" for value in re.findall(r"\bseed\s*[=:]\s*([A-Za-z0-9_-]+)", text, flags=re.IGNORECASE))
        query_ids = self._unique_strings([*alert_ids, *case_ids, *asset_ids, *seed_values])
        dates = self._unique_strings(re.findall(r"\b20\d{2}-\d{2}-\d{2}\b", text))
        affected_fields = self._affected_fields_from_text(text)
        observed_issue = self._observed_issue_from_text(text)
        context = {
            "mcp_server": mcp_server,
            "tool_name": tool_names[0] if tool_names else "",
            "tool_names": tool_names,
            "api_name": api_info.get("api_name") or "",
            "api_path": api_info.get("api_path") or "",
            "api_method": api_info.get("api_method") or "",
            "endpoint": api_info.get("endpoint") or "",
            "query_ids": query_ids,
            "alert_ids": alert_ids,
            "case_ids": case_ids,
            "asset_ids": asset_ids,
            "dates": dates,
            "affected_fields": affected_fields,
            "observed_issue": observed_issue,
            "expected_fix": self._expected_fix_from_context(mcp_server, api_info, query_ids, affected_fields, observed_issue),
        }
        return FeedbackOptimizationTaskContextRecord.model_validate({key: value for key, value in context.items() if value not in ("", [], None)}).to_payload()

    def _normalize_task_context(self, value: Any, rationale: Optional[str], owner: str) -> JsonObject:
        if isinstance(value, dict) and value:
            cleaned = {key: item for key, item in value.items() if item not in ("", [], None)}
            if isinstance(cleaned.get("expected_fix"), str):
                cleaned["expected_fix"] = cleaned["expected_fix"].replace("时 的", "时的").replace("时 返回", "时返回").replace("时 时", "时")
            return FeedbackOptimizationTaskContextRecord.model_validate(cleaned).to_payload()
        attribution = {"rationale": rationale or "", "responsibility_boundary": {"owner": owner}}
        return self._task_context_from_attribution({}, attribution, [], owner)

    def _task_context_specificity(self, context: JsonObject) -> int:
        categories = [
            bool(context.get("mcp_server")),
            bool(context.get("tool_name") or context.get("api_name") or context.get("api_path")),
            bool(context.get("query_ids") or context.get("dates")),
            bool(context.get("observed_issue")),
            bool(context.get("affected_fields")),
        ]
        return sum(1 for item in categories if item)

    def _task_context_is_actionable_external(self, context: JsonObject) -> bool:
        has_interface = bool(context.get("tool_name") or context.get("api_name") or context.get("api_path"))
        return has_interface and self._task_context_specificity(context) >= 2

    def _external_owner_from_context(self, owner: str, context: JsonObject) -> str:
        server = self._string(context.get("mcp_server"))
        generic_owners = {
            "",
            "external_mcp_service",
            "external-mcp-service",
            "mcp_description",
            "mcp-owner",
            "soc_process",
            "soc-process",
            "external_system",
            "external-system",
        }
        if server and owner in generic_owners:
            return server
        return owner

    def _server_from_owner(self, owner: str) -> str:
        if owner and owner not in {"external_mcp_service", "mcp_description", "soc_process", "external_system"}:
            return owner
        return ""

    def _api_info_from_tool_operation(self, operation: str) -> JsonObject:
        if not operation:
            return {}
        api_name = operation.split("_api_", 1)[0] if "_api_" in operation else operation
        result = {"api_name": api_name}
        if "_api_" not in operation:
            return result
        rest = operation.split("_api_", 1)[1]
        parts = [part for part in rest.split("_") if part]
        method = parts[-1].upper() if parts and parts[-1].lower() in {"get", "post", "put", "patch", "delete"} else ""
        path_parts = parts[:-1] if method else parts
        if path_parts:
            api_path = f"/api/{'/'.join(path_parts)}"
            result["api_path"] = api_path
            if method:
                result["api_method"] = method
                result["endpoint"] = f"{method} {api_path}"
        return result

    def _affected_fields_from_text(self, text: str) -> list[str]:
        candidates = [
            "event_time",
            "timestamp",
            "severity",
            "source",
            "status",
            "title",
            "asset_id",
            "alert_id",
            "case_id",
            "hostname",
            "ip",
            "process",
            "technique",
            "tactic",
        ]
        return [field for field in candidates if field in text]

    def _observed_issue_from_text(self, text: str) -> str:
        if not text:
            return ""
        fragments = re.split(r"(?<=[。！？；;])|\n+", text)
        keywords = ("缺失", "不足", "不完整", "固定", "不匹配", "不支持", "无法", "相距", "event_time", "时间戳", "字段")
        for fragment in fragments:
            clean = self._short_text(fragment, 260)
            if clean and any(keyword in clean for keyword in keywords):
                return clean
        return self._short_text(text, 260)

    def _expected_fix_from_context(
        self,
        mcp_server: str,
        api_info: dict[str, str],
        query_ids: list[str],
        affected_fields: list[str],
        observed_issue: str,
    ) -> str:
        if not any([mcp_server, api_info, query_ids, affected_fields, observed_issue]):
            return ""
        target = " ".join(part for part in [mcp_server, api_info.get("endpoint") or api_info.get("api_name")] if part) or "外部系统接口"
        query = f" 在查询 {', '.join(query_ids)} 时" if query_ids else ""
        fields = f"字段 {', '.join(affected_fields)}" if affected_fields else "完整业务字段"
        return f"修复 {target}{query}的数据返回逻辑，确保返回 {fields} 且与查询上下文一致。"

    def _external_task_title_from_context(self, context: JsonObject) -> str:
        server = self._string(context.get("mcp_server"))
        api_name = self._string(context.get("api_name")) or self._string(context.get("endpoint")) or self._string(context.get("tool_name"))
        if server and api_name:
            return f"修复 {server} {api_name} 数据返回问题"
        if server:
            return f"修复 {server} 数据返回问题"
        return ""

    def _external_task_description_from_context(self, context: JsonObject) -> str:
        server = self._string(context.get("mcp_server"))
        target = self._string(context.get("endpoint")) or self._string(context.get("api_name")) or self._string(context.get("tool_name"))
        observed_issue = self._string(context.get("observed_issue"))
        query_ids = self._string_list(context.get("query_ids"))
        if not (server and (target or observed_issue)):
            return ""
        query = f" 在查询 {', '.join(query_ids)} 时，" if query_ids else ""
        target_text = f" 的 {target}" if target else ""
        issue = f"具体表现为：{observed_issue}" if observed_issue else "存在数据不完整或与查询上下文不一致的问题。"
        return f"{server}{target_text}{query}返回的数据无法支撑 Agent 完成反馈场景回答。{issue}需要修复该接口或底层数据源，确保返回可被 Agent 稳定读取和使用的完整可靠数据。"

    def _external_task_objective_from_context(self, context: JsonObject) -> str:
        server = self._string(context.get("mcp_server"))
        target = self._string(context.get("endpoint")) or self._string(context.get("api_name")) or self._string(context.get("tool_name"))
        if not server:
            return ""
        target_text = f" {target}" if target else ""
        return f"确保 {server}{target_text} 在同类查询中返回完整、可靠且与查询上下文匹配的数据，使 Agent 能基于返回结果完整回答反馈中指出的问题。"

    def _external_acceptance_criteria_from_context(self, context: JsonObject) -> list[str]:
        server = self._string(context.get("mcp_server"))
        target = self._string(context.get("tool_name")) or self._string(context.get("endpoint")) or self._string(context.get("api_name"))
        query_ids = self._string_list(context.get("query_ids"))
        affected_fields = self._string_list(context.get("affected_fields"))
        observed_issue = self._string(context.get("observed_issue"))
        if not (server and (target or query_ids or affected_fields)):
            return []
        query = f" 在查询 {', '.join(query_ids)} 时" if query_ids else ""
        fields = f"，并包含 {', '.join(affected_fields)} 等关键字段" if affected_fields else ""
        criteria = [
            f"调用 {target or server}{query}，返回结果与查询上下文一致{fields}。",
        ]
        if observed_issue:
            criteria.append(f"返回结果不再出现该问题：{observed_issue}")
        criteria.append("关联回归测试中，Agent 能基于该返回结果完整回答反馈指出的问题。")
        return criteria

    def _plan_task_recommendation(self, target_type: str, execution_kind: str) -> str:
        if execution_kind == "workspace_execution":
            base = "由 execution-optimizer 根据归因结果生成受控执行方案，并在安全校验通过后应用到 workspace。"
        elif execution_kind == "external_webhook":
            base = "将该优化任务发送给对应外部系统，由外部系统处理服务、知识库、MCP server 或流程侧变更。"
        elif execution_kind == "internal_action":
            base = "由后端执行内部回归资产治理动作，将当前批次候选评估用例晋级为 approved 回归资产。"
        else:
            base = "当前归因结果不能转为 workspace 执行任务，也没有明确的外部 webhook 执行目标。"
        return base

    def _clean_plan_task_recommendation(self, value: Any, target_type: str, execution_kind: str) -> str:
        text = self._string(value) or self._plan_task_recommendation(target_type, execution_kind)
        marker = "归因依据："
        if marker in text:
            text = text.split(marker, 1)[0].rstrip()
        return text or self._plan_task_recommendation(target_type, execution_kind)

    def _evidence_summary(self, evidence_refs: list[JsonObject]) -> str:
        summaries: list[str] = []
        for ref in evidence_refs[:5]:
            ref_id = self._string(ref.get("id")) or self._string(ref.get("path")) or self._string(ref.get("type")) or "evidence"
            reason = self._string(ref.get("reason"))
            summaries.append(f"{ref_id}: {reason}" if reason else ref_id)
        return "\n".join(summaries)

    def _external_owner_for_target(self, target_type: str) -> Optional[str]:
        if target_type == "external_mcp_service":
            return "external-mcp-service"
        if target_type == "soc_process":
            return "soc-process"
        if target_type == "mcp_description":
            return "mcp-owner"
        return None

    def _plan_target_path(self, target_type: str) -> Optional[str]:
        if target_type in {"main_agent_claude_md", "instruction_gap", "skill_gap", "tool_misuse", "evidence_gap"}:
            return "CLAUDE.md"
        if target_type == "mcp_config":
            return ".mcp.json"
        if target_type == "skill":
            return ".claude/skills/feedback-optimization.md"
        if target_type == "subagent":
            return ".claude/agents/feedback-optimization.md"
        if target_type == "output_style":
            return ".claude/output-styles/feedback-optimization.md"
        if target_type == "eval_case":
            return "evals/feedback-optimization.json"
        if target_type in {"mcp_description", "runtime_code", "external_mcp_service", "soc_process", "not_actionable"}:
            return None
        return "CLAUDE.md"
