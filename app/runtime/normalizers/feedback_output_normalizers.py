from __future__ import annotations

import json

from ..records.json_types import JsonObject
from ..schema_versions import (
    FEEDBACK_EVAL_CASE_GENERATION_OUTPUT_SCHEMA_VERSION,
    FEEDBACK_EVAL_CASE_SCHEMA_VERSION,
    FEEDBACK_OPTIMIZATION_PLAN_OUTPUT_SCHEMA_VERSION,
    REGRESSION_IMPACT_ANALYSIS_OUTPUT_SCHEMA_VERSION,
)
from .feedback_output_records import (
    NormalizedAttributionOutput,
    NormalizedAttributionSummary,
    NormalizedBlockedOptimizationItem,
    NormalizedEvidenceRef,
    NormalizedExecutionPlanOutput,
    NormalizedExecutionOperation,
    NormalizedExternalGuidanceItem,
    NormalizedFeedbackEvalCaseGenerationOutput,
    NormalizedFeedbackOptimizationPlanOutput,
    NormalizedGeneratedEvalCase,
    NormalizedOptimizationPlanTask,
    NormalizedProposalItem,
    NormalizedProposalOutput,
    NormalizedRegressionImpactAnalysisOutput,
    NormalizedSummaryItem,
)
from .feedback_output_task_context import (
    external_context_target as _external_context_target,
    external_task_acceptance_criteria as _external_task_acceptance_criteria,
    external_task_actions as _external_task_actions,
    external_task_objective as _external_task_objective,
    external_task_validation as _external_task_validation,
    infer_external_task_context as _infer_external_task_context,
    normalize_task_context_payload as _normalize_task_context_payload,
    task_context_has_external_specificity,
)


def normalize_attribution_output(payload: JsonObject) -> JsonObject:
    normalized: JsonObject = dict(payload)
    problem_type_aliases = {
        "tool_usage_deficiency": "tool_data_quality",
        "tool_usage_gap": "tool_data_quality",
        "tool_call_gap": "tool_data_quality",
        "agent_behavior": "instruction_gap",
    }
    optimization_object_aliases = {
        "agent_behavior": "main_agent_claude_md",
        "agent": "main_agent_claude_md",
        "prompt": "main_agent_claude_md",
        "tool_usage_policy": "main_agent_claude_md",
    }
    actionability_aliases = {
        "low": "needs_human_analysis",
        "medium": "needs_human_analysis",
        "high": "needs_human_analysis",
        "human_review": "needs_human_analysis",
        "manual_review": "needs_human_analysis",
    }
    next_step_aliases = {
        "human_review": "needs_human_review",
        "manual_review": "needs_human_review",
        "review": "needs_human_review",
        "proposal": "generate_proposal",
    }
    for key, aliases in (
        ("problem_type", problem_type_aliases),
        ("optimization_object_type", optimization_object_aliases),
        ("actionability", actionability_aliases),
        ("recommended_next_step", next_step_aliases),
    ):
        value = normalized.get(key)
        if isinstance(value, str):
            normalized[key] = aliases.get(value.strip(), value)

    recommended_next_step = normalized.get("recommended_next_step")
    if isinstance(recommended_next_step, str) and recommended_next_step not in {"generate_proposal", "needs_human_review", "stop"}:
        rationale = str(normalized.get("rationale") or "").strip()
        normalized["rationale"] = f"{rationale}\n\n原始 recommended_next_step：{recommended_next_step}".strip()
        normalized["recommended_next_step"] = "needs_human_review"

    evidence_refs = normalized.get("evidence_refs")
    if isinstance(evidence_refs, list):
        normalized["evidence_refs"] = [
            item
            if isinstance(item, dict)
            else {"type": "evidence_file", "id": str(item), "reason": "归因分析智能体引用了该证据文件。"}
            for item in evidence_refs
        ]

    responsibility_boundary = normalized.get("responsibility_boundary")
    if isinstance(responsibility_boundary, str):
        normalized["responsibility_boundary"] = {
            "owner": responsibility_boundary,
            "reason": "归因分析智能体输出了责任边界标签，系统归一化为结构化对象。",
        }

    return NormalizedAttributionOutput.model_validate(normalized).to_payload()


def normalize_proposal_output(payload: JsonObject) -> JsonObject:
    normalized: JsonObject = dict(payload)
    proposals: list[NormalizedProposalItem] = []
    for item in normalized.get("proposals") or []:
        if not isinstance(item, dict):
            continue
        proposal = dict(item)
        if not proposal.get("proposal_id") and proposal.get("id"):
            proposal["proposal_id"] = str(proposal["id"])
        if not proposal.get("title"):
            proposal["title"] = _proposal_title(proposal)
        if not proposal.get("target_type"):
            proposal["target_type"] = _proposal_target_type(str(proposal.get("target_path") or ""))
        if not proposal.get("expected_effect"):
            proposal["expected_effect"] = "提高反馈所指场景的回答完整性和可核查性。"
        if not proposal.get("validation"):
            proposal["validation"] = "复测原反馈场景，并检查反馈闭环中是否产生有效归因和建议结果。"
        if not proposal.get("risk"):
            proposal["risk"] = "可能增加回答前的工具调用成本或响应耗时。"
        proposals.append(NormalizedProposalItem.model_validate(proposal))
    normalized["proposals"] = [proposal.to_payload() for proposal in proposals]
    external_guidance: list[NormalizedExternalGuidanceItem] = []
    for item in normalized.get("external_guidance") or []:
        if not isinstance(item, dict):
            continue
        guidance = dict(item)
        if not guidance.get("owner") and guidance.get("target"):
            guidance["owner"] = str(guidance["target"])
        if not guidance.get("reason") and guidance.get("rationale"):
            guidance["reason"] = str(guidance["rationale"])
        external_guidance.append(NormalizedExternalGuidanceItem.model_validate(guidance))
    normalized["external_guidance"] = [guidance.to_payload() for guidance in external_guidance]
    return NormalizedProposalOutput.model_validate(normalized).to_payload()


def _proposal_title(proposal: JsonObject) -> str:
    recommendation = str(proposal.get("recommendation") or "").strip()
    if recommendation:
        first_line = recommendation.splitlines()[0].strip()
        if first_line:
            return first_line[:80]
    target_path = str(proposal.get("target_path") or "").strip()
    return f"优化 {target_path}" if target_path else "优化方案"


def _proposal_target_type(target_path: str) -> str:
    if target_path == "CLAUDE.md":
        return "main_agent_claude_md"
    if target_path == ".mcp.json":
        return "mcp_config"
    if target_path.startswith(".claude/skills/"):
        return "skill"
    if target_path.startswith(".claude/agents/"):
        return "subagent"
    if target_path.startswith(".claude/output-styles/"):
        return "output_style"
    if target_path.startswith("evals/"):
        return "eval_case"
    return "workspace_file"


def normalize_feedback_optimization_plan_output(payload: JsonObject) -> JsonObject:
    normalized: JsonObject = dict(payload)
    if not normalized.get("optimization_plan_id") and normalized.get("plan_id"):
        normalized["optimization_plan_id"] = str(normalized["plan_id"])
    normalized.setdefault("schema_version", FEEDBACK_OPTIMIZATION_PLAN_OUTPUT_SCHEMA_VERSION)
    normalized["status"] = _normalize_plan_status(normalized.get("status"))
    normalized["confidence"] = _normalize_confidence(normalized.get("confidence"))
    normalized["actionability"] = _normalize_actionability(normalized.get("actionability"))

    tasks: list[NormalizedOptimizationPlanTask] = []
    blocked_items: list[NormalizedBlockedOptimizationItem] = []
    for index, item in enumerate(normalized.get("tasks") or []):
        if not isinstance(item, dict):
            continue
        task = _normalize_plan_task_output_item(item, index)
        if _plan_task_output_is_executable(task):
            tasks.append(NormalizedOptimizationPlanTask.model_validate(task))
        else:
            blocked_items.append(NormalizedBlockedOptimizationItem.model_validate(_blocked_item_from_plan_task(task, index)))
    for index, item in enumerate(normalized.get("blocked_items") or []):
        if not isinstance(item, dict):
            continue
        blocked = _normalize_blocked_output_item(item, index)
        promoted_task = _external_plan_task_from_blocked_item(blocked, index, normalized)
        if promoted_task is not None:
            tasks.append(NormalizedOptimizationPlanTask.model_validate(promoted_task))
        else:
            blocked_items.append(NormalizedBlockedOptimizationItem.model_validate(blocked))
    normalized["tasks"] = [task.to_payload() for task in tasks]
    normalized["blocked_items"] = [blocked.to_payload() for blocked in blocked_items]

    for key in ("title", "summary", "recommendation", "expected_effect", "validation", "risk", "rationale"):
        if normalized.get(key) is not None and not isinstance(normalized.get(key), str):
            normalized[key] = _human_text(normalized.get(key))
    if not normalized.get("title"):
        normalized["title"] = "反馈批次优化方案"
    if not normalized.get("recommendation"):
        normalized["recommendation"] = normalized.get("summary") or "根据归因结果生成优化任务。"
    if not normalized.get("expected_effect"):
        normalized["expected_effect"] = "降低同类反馈再次出现的概率。"
    if not normalized.get("validation"):
        normalized["validation"] = "使用本批次关联回归测试用例验证优化效果。"
    if not normalized.get("risk"):
        normalized["risk"] = "需要关注优化后是否引入新的行为退化。"
    normalized["feedback_case_ids"] = _string_list(normalized.get("feedback_case_ids"))
    normalized["eval_case_ids"] = _string_list(normalized.get("eval_case_ids"))
    normalized["attribution_job_ids"] = _string_list(normalized.get("attribution_job_ids"))
    normalized["attribution_summaries"] = _normalize_attribution_summaries(normalized.get("attribution_summaries"))
    normalized["problem_types"] = _string_list(normalized.get("problem_types"))
    normalized["evidence_refs"] = _normalize_evidence_refs(normalized.get("evidence_refs"))
    return NormalizedFeedbackOptimizationPlanOutput.model_validate(normalized).to_payload()


def _normalize_plan_task_output_item(item: JsonObject, index: int) -> JsonObject:
    task = dict(item)
    actionability = _normalize_actionability(task.get("actionability"))
    target_type = str(task.get("target_type") or task.get("optimization_object_type") or "").strip()
    execution_kind = str(task.get("execution_kind") or "").strip()
    if not execution_kind:
        if actionability in {"direct_workspace_change", "workspace_config_change", "eval_only"}:
            execution_kind = "workspace_execution"
        elif actionability == "external_guidance" or target_type in {"external_mcp_service", "soc_process", "mcp_description"}:
            execution_kind = "external_webhook"
        else:
            execution_kind = "blocked"
    task["source_index"] = int(task.get("source_index") or index)
    task["execution_kind"] = execution_kind
    task["status"] = task.get("status") or ("pending_notification" if execution_kind == "external_webhook" else "pending_execution")
    task["target_type"] = target_type or ("external_mcp_service" if execution_kind == "external_webhook" else "main_agent_claude_md")
    task["actionability"] = actionability
    task["confidence"] = _normalize_confidence(task.get("confidence")) if task.get("confidence") is not None else None
    task["problem_type"] = _normalize_problem_type(task.get("problem_type")) if task.get("problem_type") is not None else None
    task["task_context"] = _normalize_task_context_payload(task.get("task_context"))
    task["evidence_refs"] = _normalize_evidence_refs(task.get("evidence_refs"))
    for key in (
        "title",
        "summary",
        "description",
        "objective",
        "target_summary",
        "recommendation",
        "expected_effect",
        "validation",
        "risk",
        "analysis_summary",
        "evidence_summary",
        "rationale",
        "reason",
    ):
        if task.get(key) is not None and not isinstance(task.get(key), str):
            task[key] = _human_text(task.get(key))
    task["title"] = task.get("title") or "优化任务"
    task["description"] = task.get("description") or task.get("recommendation") or "根据归因结果执行优化。"
    task["objective"] = task.get("objective") or task.get("expected_effect") or "降低同类反馈再次出现的概率。"
    task["recommendation"] = task.get("recommendation") or task["description"]
    task["expected_effect"] = task.get("expected_effect") or "降低同类反馈再次出现的概率。"
    task["validation"] = task.get("validation") or "使用关联回归测试用例验证优化效果。"
    task["risk"] = task.get("risk") or "需要关注优化后是否引入新的行为退化。"
    task["recommended_actions"] = _string_list(task.get("recommended_actions"))
    task["acceptance_criteria"] = _string_list(task.get("acceptance_criteria")) or [task["validation"]]
    task["feedback_case_ids"] = _string_list(task.get("feedback_case_ids"))
    task["eval_case_ids"] = _string_list(task.get("eval_case_ids"))
    task["attribution_job_ids"] = _string_list(task.get("attribution_job_ids"))
    return NormalizedOptimizationPlanTask.model_validate(task).to_payload()


def _plan_task_output_is_executable(task: JsonObject) -> bool:
    execution_kind = str(task.get("execution_kind") or "")
    if execution_kind == "workspace_execution":
        return bool(task.get("target_path"))
    if execution_kind == "external_webhook":
        return task_context_has_external_specificity(_normalize_task_context_payload(task.get("task_context")))
    return False


def _blocked_item_from_plan_task(task: JsonObject, index: int) -> JsonObject:
    reason = str(task.get("reason") or "").strip()
    if not reason:
        if task.get("execution_kind") == "workspace_execution":
            reason = "任务缺少 target_path，不能交给 execution-optimizer 执行。"
        elif task.get("execution_kind") == "external_webhook":
            reason = "任务缺少明确的外部对象、接口或问题 ID，不能派发到外部系统。"
        else:
            reason = "该项未形成可执行 workspace 任务或外部系统任务。"
    return _normalize_blocked_output_item(
        {
            **task,
            "blocked_item_id": task.get("blocked_item_id") or task.get("plan_task_id"),
            "status": "blocked",
            "reason": reason,
        },
        index,
    )


def _normalize_blocked_output_item(item: JsonObject, index: int) -> JsonObject:
    blocked = dict(item)
    blocked["source_index"] = int(blocked.get("source_index") or index)
    blocked["status"] = blocked.get("status") or "blocked"
    blocked["title"] = blocked.get("title") or "未形成可执行优化任务"
    blocked["target_type"] = blocked.get("target_type") or "not_actionable"
    blocked["actionability"] = _normalize_actionability(blocked.get("actionability"))
    blocked["confidence"] = _normalize_confidence(blocked.get("confidence")) if blocked.get("confidence") is not None else None
    blocked["problem_type"] = _normalize_problem_type(blocked.get("problem_type")) if blocked.get("problem_type") is not None else None
    blocked["task_context"] = _normalize_task_context_payload(blocked.get("task_context"))
    blocked["evidence_refs"] = _normalize_evidence_refs(blocked.get("evidence_refs"))
    if not blocked.get("reason"):
        blocked["reason"] = blocked.get("recommendation") or "该项不能自动执行，也没有可通知的外部目标。"
    for key in ("title", "recommendation", "reason", "analysis_summary", "evidence_summary"):
        if blocked.get(key) is not None and not isinstance(blocked.get(key), str):
            blocked[key] = _human_text(blocked.get(key))
    blocked["feedback_case_ids"] = _string_list(blocked.get("feedback_case_ids"))
    blocked["eval_case_ids"] = _string_list(blocked.get("eval_case_ids"))
    blocked["attribution_job_ids"] = _string_list(blocked.get("attribution_job_ids"))
    return NormalizedBlockedOptimizationItem.model_validate(blocked).to_payload()


def _external_plan_task_from_blocked_item(
    blocked: JsonObject,
    index: int,
    plan: JsonObject | None = None,
) -> JsonObject | None:
    context = _infer_external_task_context(blocked, plan)
    if not task_context_has_external_specificity(context):
        return None

    target_type = str(blocked.get("target_type") or "").strip()
    if target_type not in {"external_mcp_service", "soc_process", "mcp_description"}:
        target_type = "external_mcp_service"
    owner = (
        str(blocked.get("owner") or "").strip()
        or str(context.get("mcp_server") or "").strip()
        or str(context.get("external_system") or "").strip()
        or target_type
    )
    target = _external_context_target(context)
    observed_issue = str(context.get("observed_issue") or "").strip()
    reason = str(blocked.get("reason") or "").strip()
    recommendation = str(blocked.get("recommendation") or "").strip()
    description = recommendation or reason or f"通知 {owner} 处理外部系统数据或接口问题。"
    validation = _external_task_validation(context)

    return _normalize_plan_task_output_item(
        {
            **blocked,
            "source_index": blocked.get("source_index", index),
            "execution_kind": "external_webhook",
            "status": "pending_notification",
            "title": blocked.get("title") or f"通知 {owner} 处理外部系统问题",
            "description": description,
            "objective": _external_task_objective(owner, target, observed_issue),
            "target_summary": f"external:{owner}",
            "target_type": target_type,
            "target_path": None,
            "owner": owner,
            "actionability": "external_guidance",
            "recommendation": recommendation or description,
            "recommended_actions": _external_task_actions(owner, target, observed_issue),
            "acceptance_criteria": _external_task_acceptance_criteria(context),
            "expected_effect": "Agent 后续可从外部系统获得完整、可靠且与查询上下文匹配的数据。",
            "validation": validation,
            "risk": blocked.get("risk") or "外部系统修复周期可能影响本批次回归验证完成时间。",
            "analysis_summary": blocked.get("analysis_summary") or reason,
            "rationale": reason,
            "task_context": context,
        },
        index,
    )


def _normalize_plan_status(value: object) -> str:
    status_value = str(value or "").strip().lower()
    if status_value in {"completed", "ready", "approved", "pending_review", "pending_approval"}:
        return "pending_approval"
    if status_value in {"needs_review", "manual_review", "blocked", "failed", "needs_human_review"}:
        return "needs_human_review"
    return "pending_approval"

def _normalize_confidence(value: object) -> str:
    confidence = str(value or "").strip().lower()
    if confidence in {"high", "medium", "low"}:
        return confidence
    return "medium"

def _normalize_actionability(value: object) -> str:
    actionability = str(value or "").strip()
    aliases = {
        "manual_review": "needs_human_analysis",
        "human_review": "needs_human_analysis",
        "agent_behavior": "direct_workspace_change",
        "workspace_change": "direct_workspace_change",
        "external": "external_guidance",
        "external_task": "external_guidance",
        "not_applicable": "not_actionable",
    }
    actionability = aliases.get(actionability, actionability)
    allowed = {
        "direct_workspace_change",
        "workspace_config_change",
        "eval_only",
        "external_guidance",
        "runtime_fix",
        "needs_human_analysis",
        "not_actionable",
    }
    return actionability if actionability in allowed else "needs_human_analysis"

def _normalize_problem_type(value: object) -> str:
    problem_type = str(value or "").strip()
    aliases = {
        "tool_usage_deficiency": "tool_data_quality",
        "tool_usage_gap": "tool_data_quality",
        "tool_call_gap": "tool_data_quality",
        "agent_behavior": "instruction_gap",
    }
    problem_type = aliases.get(problem_type, problem_type)
    allowed = {
        "evidence_gap",
        "tool_misuse",
        "tool_unavailable",
        "tool_data_quality",
        "output_style_issue",
        "instruction_gap",
        "skill_gap",
        "mcp_description_gap",
        "runtime_error",
        "external_soc_process_issue",
        "user_misunderstanding",
        "insufficient_information",
    }
    return problem_type if problem_type in allowed else "insufficient_information"

def _normalize_evidence_refs(value: object) -> list[JsonObject]:
    refs: list[NormalizedEvidenceRef] = []
    for item in value or []:
        if isinstance(item, dict):
            ref_type = str(item.get("type") or "evidence_file").strip()
            ref_id = str(item.get("id") or item.get("path") or item.get("file") or "").strip()
            reason = str(item.get("reason") or item.get("description") or "优化方案生成智能体引用了该证据。").strip()
            if ref_id:
                refs.append(NormalizedEvidenceRef.model_validate({"type": ref_type, "id": ref_id, "reason": reason}))
        else:
            refs.append(
                NormalizedEvidenceRef.model_validate(
                    {"type": "evidence_file", "id": str(item), "reason": "优化方案生成智能体引用了该证据。"}
                )
            )
    return [ref.to_payload() for ref in refs]


def _normalize_attribution_summaries(value: object) -> list[JsonObject]:
    items: list[NormalizedAttributionSummary] = []
    for item in value or []:
        if isinstance(item, dict):
            items.append(NormalizedAttributionSummary.model_validate(item))
            continue
        text = str(item).strip()
        if text:
            items.append(NormalizedAttributionSummary.model_validate({"summary": text}))
    return [item.to_payload() for item in items]


def _normalize_summary_items(value: object) -> list[JsonObject]:
    items: list[NormalizedSummaryItem] = []
    for item in value or []:
        if isinstance(item, dict):
            items.append(NormalizedSummaryItem.model_validate(item))
            continue
        text = str(item).strip()
        if text:
            items.append(NormalizedSummaryItem.model_validate({"summary": text}))
    return [item.to_payload() for item in items]


def _string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    text = str(value).strip()
    return [text] if text else []


def normalize_execution_plan_output(payload: JsonObject) -> JsonObject:
    normalized: JsonObject = dict(payload)
    status = normalized.get("status")
    if isinstance(status, str):
        status_value = status.strip().lower()
        if status_value in {"safe_to_apply", "ready_to_apply", "applicable", "success", "completed", "complete"}:
            normalized["status"] = "ready"
        elif status_value in {"requires_human_review", "requires_review", "needs_review", "manual_review", "unsafe", "blocked"}:
            normalized["status"] = "needs_human_review"
    operations: list[NormalizedExecutionOperation] = []
    for item in normalized.get("operations") or normalized.get("patches") or []:
        if not isinstance(item, dict):
            continue
        operation = dict(item)
        if not operation.get("operation") and operation.get("op"):
            operation["operation"] = operation["op"]
        if operation.get("operation") == "append":
            operation["operation"] = "append_text"
        if operation.get("operation") == "replace":
            operation["operation"] = "replace_file"
        if not operation.get("append_text") and operation.get("operation") == "append_text" and operation.get("content"):
            operation["append_text"] = operation["content"]
        if operation.get("rationale") is not None and not isinstance(operation.get("rationale"), str):
            operation["rationale"] = _human_text(operation.get("rationale"))
        operations.append(NormalizedExecutionOperation.model_validate(operation))
    normalized["operations"] = [operation.to_payload() for operation in operations]
    if not normalized.get("status"):
        normalized["status"] = "ready" if operations else "needs_human_review"
    if not normalized.get("summary"):
        normalized["summary"] = normalized.get("recommendation") or normalized.get("no_action_reason") or "执行优化方案"
    for key in ("summary", "validation", "risk", "no_action_reason"):
        if normalized.get(key) is not None and not isinstance(normalized.get(key), str):
            normalized[key] = _human_text(normalized.get(key))
    return NormalizedExecutionPlanOutput.model_validate(normalized).to_payload()


def normalize_feedback_eval_case_generation_output(payload: JsonObject) -> JsonObject:
    normalized: JsonObject = dict(payload)
    normalized.setdefault("schema_version", FEEDBACK_EVAL_CASE_GENERATION_OUTPUT_SCHEMA_VERSION)
    if "eval_cases" not in normalized and isinstance(normalized.get("eval_case"), dict):
        normalized["eval_cases"] = [normalized["eval_case"]]
    cases: list[NormalizedGeneratedEvalCase] = []
    for item in normalized.get("eval_cases") or []:
        if not isinstance(item, dict):
            continue
        case = dict(item)
        case.setdefault("schema_version", FEEDBACK_EVAL_CASE_SCHEMA_VERSION)
        case["status"] = _normalize_eval_case_status(case.get("status"))
        case["asset_layer"] = str(case.get("asset_layer") or "candidate")
        case["promotion_status"] = str(case.get("promotion_status") or ("approved" if case["status"] == "active" else "candidate"))
        case["blocking_policy"] = str(case.get("blocking_policy") or ("blocking" if case["status"] == "active" else "non_blocking"))
        case["severity"] = str(case.get("severity") or "medium")
        case["flaky_status"] = str(case.get("flaky_status") or "stable")
        case["variant_role"] = str(case.get("variant_role") or "original_reproduction")
        case["labels"] = _string_list(case.get("labels"))
        case["checks_json"] = case.get("checks_json") if isinstance(case.get("checks_json"), dict) else {}
        for key in ("prompt", "expected_behavior"):
            if case.get(key) is not None and not isinstance(case.get(key), str):
                case[key] = _human_text(case.get(key))
        if not str(case.get("prompt") or "").strip():
            case["prompt"] = str(case.get("title") or case.get("source_summary") or "").strip()
        cases.append(NormalizedGeneratedEvalCase.model_validate(case))
    normalized["eval_cases"] = [case.to_payload() for case in cases]
    if not cases and not normalized.get("no_action_reason"):
        normalized["no_action_reason"] = "eval-case-governor 未生成可用评估用例。"
    normalized["status"] = "completed" if cases and normalized.get("status") != "needs_human_review" else "needs_human_review"
    return NormalizedFeedbackEvalCaseGenerationOutput.model_validate(normalized).to_payload()


def normalize_regression_impact_analysis_output(payload: JsonObject) -> JsonObject:
    normalized: JsonObject = dict(payload)
    normalized.setdefault("schema_version", REGRESSION_IMPACT_ANALYSIS_OUTPUT_SCHEMA_VERSION)
    normalized["gate_result"] = normalized.get("gate_result") if isinstance(normalized.get("gate_result"), dict) else {}
    normalized["impacted_assets"] = _normalize_summary_items(normalized.get("impacted_assets"))
    normalized["recommendations"] = _string_list(normalized.get("recommendations"))
    normalized["next_steps"] = _string_list(normalized.get("next_steps"))
    status = str(normalized.get("status") or "").strip().lower()
    normalized["status"] = "needs_human_review" if status in {"needs_human_review", "needs_review", "manual_review"} else "completed"
    for key in ("summary", "risk_assessment", "no_action_reason"):
        if normalized.get(key) is not None and not isinstance(normalized.get(key), str):
            normalized[key] = _human_text(normalized.get(key))
    if not normalized["recommendations"] and normalized.get("summary"):
        normalized["recommendations"] = [str(normalized["summary"])]
    return NormalizedRegressionImpactAnalysisOutput.model_validate(normalized).to_payload()


def _normalize_eval_case_status(value: object) -> str:
    status = str(value or "").strip().lower()
    if status in {"active", "draft", "archived"}:
        return status
    if status in {"approved", "blocking"}:
        return "active"
    return "draft"


def _human_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    return str(value)
