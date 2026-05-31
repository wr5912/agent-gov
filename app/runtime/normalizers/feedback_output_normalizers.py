from __future__ import annotations

import json
import re
from typing import Any


def normalize_attribution_output(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
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

    return normalized


def normalize_proposal_output(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    proposals: list[Any] = []
    for item in normalized.get("proposals") or []:
        if not isinstance(item, dict):
            proposals.append(item)
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
        proposals.append(proposal)
    normalized["proposals"] = proposals
    external_guidance: list[Any] = []
    for item in normalized.get("external_guidance") or []:
        if not isinstance(item, dict):
            external_guidance.append(item)
            continue
        guidance = dict(item)
        if not guidance.get("owner") and guidance.get("target"):
            guidance["owner"] = str(guidance["target"])
        if not guidance.get("reason") and guidance.get("rationale"):
            guidance["reason"] = str(guidance["rationale"])
        external_guidance.append(guidance)
    normalized["external_guidance"] = external_guidance
    return normalized


def _proposal_title(proposal: dict[str, Any]) -> str:
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


def normalize_feedback_optimization_plan_output(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    if not normalized.get("optimization_plan_id") and normalized.get("plan_id"):
        normalized["optimization_plan_id"] = str(normalized["plan_id"])
    normalized.setdefault("schema_version", "feedback-optimization-plan-output/v1")
    normalized["status"] = _normalize_plan_status(normalized.get("status"))
    normalized["confidence"] = _normalize_confidence(normalized.get("confidence"))
    normalized["actionability"] = _normalize_actionability(normalized.get("actionability"))

    tasks: list[dict[str, Any]] = []
    blocked_items: list[dict[str, Any]] = []
    for index, item in enumerate(normalized.get("tasks") or []):
        if not isinstance(item, dict):
            continue
        task = _normalize_plan_task_output_item(item, index)
        if _plan_task_output_is_executable(task):
            tasks.append(task)
        else:
            blocked_items.append(_blocked_item_from_plan_task(task, index))
    for index, item in enumerate(normalized.get("blocked_items") or []):
        if not isinstance(item, dict):
            continue
        blocked = _normalize_blocked_output_item(item, index)
        promoted_task = _external_plan_task_from_blocked_item(blocked, index, normalized)
        if promoted_task is not None:
            tasks.append(promoted_task)
        else:
            blocked_items.append(blocked)
    normalized["tasks"] = tasks
    normalized["blocked_items"] = blocked_items

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
    normalized["attribution_summaries"] = _normalize_dict_list(normalized.get("attribution_summaries"), default_key="summary")
    normalized["problem_types"] = _string_list(normalized.get("problem_types"))
    normalized["evidence_refs"] = _normalize_evidence_refs(normalized.get("evidence_refs"))
    return normalized


def _normalize_plan_task_output_item(item: dict[str, Any], index: int) -> dict[str, Any]:
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
    return task


def _plan_task_output_is_executable(task: dict[str, Any]) -> bool:
    execution_kind = str(task.get("execution_kind") or "")
    if execution_kind == "workspace_execution":
        return bool(task.get("target_path"))
    if execution_kind == "external_webhook":
        return task_context_has_external_specificity(_normalize_task_context_payload(task.get("task_context")))
    return False


def _blocked_item_from_plan_task(task: dict[str, Any], index: int) -> dict[str, Any]:
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


def _normalize_blocked_output_item(item: dict[str, Any], index: int) -> dict[str, Any]:
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
    return blocked


def _external_plan_task_from_blocked_item(
    blocked: dict[str, Any],
    index: int,
    plan: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
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


def _infer_external_task_context(item: dict[str, Any], plan: dict[str, Any] | None = None) -> dict[str, Any]:
    context = _normalize_task_context_payload(item.get("task_context"))
    text = _external_context_source_text(item, plan)
    owner = str(item.get("owner") or "").strip()

    server = str(context.get("mcp_server") or "").strip() or _infer_mcp_server(text, owner)
    if server:
        context["mcp_server"] = server
        context.setdefault("external_system", server)

    tool_names = _unique_strings(
        [
            *_string_list(context.get("tool_names")),
            str(context.get("tool_name") or "").strip(),
            *_infer_tool_names(text),
        ]
    )
    if tool_names:
        context["tool_name"] = str(context.get("tool_name") or "").strip() or tool_names[0]
        context["tool_names"] = tool_names

    api_info = _api_info_from_tool_name(str(context.get("tool_name") or ""))
    for key, value in api_info.items():
        context.setdefault(key, value)

    context["query_ids"] = _unique_strings([*_string_list(context.get("query_ids")), *_infer_query_ids(text)])
    context["alert_ids"] = _unique_strings([*_string_list(context.get("alert_ids")), *re.findall(r"\balert[-_][A-Za-z0-9]+\b", text)])
    context["case_ids"] = _unique_strings([*_string_list(context.get("case_ids")), *re.findall(r"\bcase[-_][A-Za-z0-9]+\b", text)])
    context["asset_ids"] = _unique_strings([*_string_list(context.get("asset_ids")), *re.findall(r"\basset[-_][A-Za-z0-9]+\b", text)])
    context["dates"] = _unique_strings([*_string_list(context.get("dates")), *_infer_dates(text)])
    context["affected_fields"] = _unique_strings([*_string_list(context.get("affected_fields")), *_infer_affected_fields(text)])
    context["observed_issue"] = str(context.get("observed_issue") or "").strip() or _infer_observed_issue(text)
    if not context.get("expected_fix"):
        context["expected_fix"] = _external_expected_fix(context)

    return {key: value for key, value in context.items() if value not in ("", [], None)}


def _external_context_source_text(item: dict[str, Any], plan: dict[str, Any] | None = None) -> str:
    fields = (
        "title",
        "summary",
        "description",
        "objective",
        "target_summary",
        "recommendation",
        "reason",
        "analysis_summary",
        "evidence_summary",
        "rationale",
    )
    parts = [str(item.get(key) or "") for key in fields]
    if plan:
        parts.extend(str(plan.get(key) or "") for key in fields)
        for summary in plan.get("attribution_summaries") or []:
            if isinstance(summary, dict):
                parts.extend(str(value or "") for value in summary.values())
            else:
                parts.append(str(summary or ""))
    for ref in item.get("evidence_refs") or []:
        if isinstance(ref, dict):
            parts.append(str(ref.get("id") or ""))
            parts.append(str(ref.get("reason") or ""))
    for ref in (plan or {}).get("evidence_refs") or []:
        if isinstance(ref, dict):
            parts.append(str(ref.get("id") or ""))
            parts.append(str(ref.get("reason") or ""))
    return "\n".join(part.strip() for part in parts if part and part.strip())


def _infer_mcp_server(text: str, owner: str) -> str:
    generic_owners = {"", "external_mcp_service", "mcp_description", "soc_process", "external_system", "not_actionable"}
    if owner and owner not in generic_owners:
        return owner
    full_tool = re.search(r"\bmcp__([A-Za-z0-9_-]+)__", text)
    if full_tool:
        return full_tool.group(1)
    if "sec-ops-data" in text:
        return "sec-ops-data"
    server_match = re.search(r"\b([A-Za-z0-9][A-Za-z0-9_-]*-[A-Za-z0-9_-]+)\s*(?:MCP|mcp|服务|工具|数据源|接口)", text)
    return server_match.group(1) if server_match else ""


def _infer_tool_names(text: str) -> list[str]:
    full_tools = re.findall(r"\bmcp__[A-Za-z0-9_-]+__[A-Za-z0-9_]+__[A-Za-z0-9_]+\b", text)
    api_tools = re.findall(r"\b(?:list|get|search|query|fetch)_[A-Za-z0-9_]*api_v\d+[A-Za-z0-9_]*\b", text)
    simple_tools = re.findall(r"\b(?:list|get|search|query|fetch)_[A-Za-z0-9_]+\b", text)
    return _unique_strings([*full_tools, *api_tools, *simple_tools])


def _api_info_from_tool_name(tool_name: str) -> dict[str, str]:
    if not tool_name:
        return {}
    operation = tool_name.split("__")[-1] if "__" in tool_name else tool_name
    result = {"api_name": operation.split("_api_", 1)[0] if "_api_" in operation else operation}
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


def _infer_query_ids(text: str) -> list[str]:
    return _unique_strings(
        [
            *re.findall(r"\balert[-_][A-Za-z0-9]+\b", text, flags=re.IGNORECASE),
            *re.findall(r"\bcase[-_][A-Za-z0-9]+\b", text, flags=re.IGNORECASE),
            *re.findall(r"\basset[-_][A-Za-z0-9]+\b", text, flags=re.IGNORECASE),
            *re.findall(r"\bCVE-\d{4}-\d{4,}\b", text, flags=re.IGNORECASE),
        ]
    )


def _infer_dates(text: str) -> list[str]:
    return _unique_strings([*re.findall(r"\b20\d{2}-\d{2}-\d{2}\b", text), *re.findall(r"\b20\d{2}\b", text)])


def _infer_affected_fields(text: str) -> list[str]:
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
    fields = [field for field in candidates if field in text]
    if "年份" in text or "2026" in text:
        fields.append("year")
    if "漏洞" in text or "CVE" in text:
        fields.append("cve_coverage")
    return _unique_strings(fields)


def _infer_observed_issue(text: str) -> str:
    if "2026" in text and any(keyword in text for keyword in ("缺失", "缺少", "未收录", "没有", "不全")):
        return "2026 年漏洞数据缺失或未完整收录。"
    keywords = ("缺失", "缺少", "不足", "不完整", "固定", "不匹配", "不支持", "无法", "错误", "字段")
    for fragment in re.split(r"(?<=[。！？；;])|\n+", text):
        clean = fragment.strip()
        if clean and any(keyword in clean for keyword in keywords):
            return clean[:260]
    return text.strip()[:260]


def _external_context_target(context: dict[str, Any]) -> str:
    return str(
        context.get("endpoint")
        or context.get("api_name")
        or context.get("tool_name")
        or context.get("mcp_server")
        or context.get("external_system")
        or "外部系统"
    )


def _external_task_objective(owner: str, target: str, observed_issue: str) -> str:
    issue = f"，修复{observed_issue}" if observed_issue else ""
    return f"推动 {owner} 修复 {target} 的数据或接口能力{issue}，让 Agent 后续可获得完整可靠输入。"


def _external_task_actions(owner: str, target: str, observed_issue: str) -> list[str]:
    issue = f"：{observed_issue}" if observed_issue else ""
    return [
        f"请 {owner} 核查 {target} 的数据覆盖、筛选条件和返回逻辑{issue}",
        "修复后使用关联反馈场景验证 Agent 能获得完整数据并完成回答。",
    ]


def _external_task_acceptance_criteria(context: dict[str, Any]) -> list[str]:
    target = _external_context_target(context)
    query_ids = _string_list(context.get("query_ids"))
    affected_fields = _string_list(context.get("affected_fields"))
    observed_issue = str(context.get("observed_issue") or "").strip()
    query = f" 查询 {', '.join(query_ids)} 时" if query_ids else ""
    fields = f"，并覆盖 {', '.join(affected_fields)}" if affected_fields else ""
    criteria = [f"调用 {target}{query} 返回的数据与反馈场景一致{fields}。"]
    if observed_issue:
        criteria.append(f"返回结果不再出现该问题：{observed_issue}")
    criteria.append("关联回归测试中，Agent 能基于外部系统返回结果完整回答反馈指出的问题。")
    return criteria


def _external_task_validation(context: dict[str, Any]) -> str:
    target = _external_context_target(context)
    return f"修复后重新运行本批次回归测试，并核查 {target} 返回结果是否满足验收标准。"


def _external_expected_fix(context: dict[str, Any]) -> str:
    server = str(context.get("mcp_server") or context.get("external_system") or "").strip()
    target = _external_context_target(context)
    fields = _string_list(context.get("affected_fields"))
    field_text = f"，覆盖 {', '.join(fields)}" if fields else ""
    return f"修复 {server or target} 的 {target} 数据返回逻辑{field_text}，确保返回结果与查询上下文一致。"


def _unique_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _normalize_plan_status(value: Any) -> str:
    status_value = str(value or "").strip().lower()
    if status_value in {"completed", "ready", "approved", "pending_review", "pending_approval"}:
        return "pending_approval"
    if status_value in {"needs_review", "manual_review", "blocked", "failed", "needs_human_review"}:
        return "needs_human_review"
    return "pending_approval"


def _normalize_confidence(value: Any) -> str:
    confidence = str(value or "").strip().lower()
    if confidence in {"high", "medium", "low"}:
        return confidence
    return "medium"


def _normalize_actionability(value: Any) -> str:
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


def _normalize_problem_type(value: Any) -> str:
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


def _normalize_task_context_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    list_keys = {"tool_names", "query_ids", "alert_ids", "case_ids", "asset_ids", "dates", "affected_fields"}
    context: dict[str, Any] = {}
    for key, item in value.items():
        if item is None:
            continue
        if isinstance(item, list):
            context[key] = [str(entry).strip() for entry in item if str(entry).strip()]
        elif isinstance(item, dict):
            context[key] = item
        else:
            text = str(item).strip()
            if text:
                context[key] = [text] if key in list_keys else text
    return context


def task_context_has_external_specificity(context: dict[str, Any]) -> bool:
    has_interface = bool(context.get("tool_name") or context.get("tool_names") or context.get("api_name") or context.get("api_path") or context.get("endpoint"))
    has_object = bool(
        context.get("query_ids")
        or context.get("alert_ids")
        or context.get("case_ids")
        or context.get("asset_ids")
        or context.get("affected_fields")
        or context.get("observed_issue")
    )
    has_owner = bool(context.get("mcp_server") or context.get("external_system"))
    return has_interface and has_object and has_owner


def _normalize_evidence_refs(value: Any) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    for item in value or []:
        if isinstance(item, dict):
            ref_type = str(item.get("type") or "evidence_file").strip()
            ref_id = str(item.get("id") or item.get("path") or item.get("file") or "").strip()
            reason = str(item.get("reason") or item.get("description") or "优化方案生成智能体引用了该证据。").strip()
            if ref_id:
                refs.append({"type": ref_type, "id": ref_id, "reason": reason})
        else:
            refs.append({"type": "evidence_file", "id": str(item), "reason": "优化方案生成智能体引用了该证据。"})
    return refs


def _normalize_dict_list(value: Any, *, default_key: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in value or []:
        if isinstance(item, dict):
            items.append(item)
            continue
        text = str(item).strip()
        if text:
            items.append({default_key: text})
    return items


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    text = str(value).strip()
    return [text] if text else []


def normalize_execution_plan_output(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    status = normalized.get("status")
    if isinstance(status, str):
        status_value = status.strip().lower()
        if status_value in {"safe_to_apply", "ready_to_apply", "applicable", "success", "completed", "complete"}:
            normalized["status"] = "ready"
        elif status_value in {"requires_human_review", "requires_review", "needs_review", "manual_review", "unsafe", "blocked"}:
            normalized["status"] = "needs_human_review"
    operations: list[Any] = []
    for item in normalized.get("operations") or normalized.get("patches") or []:
        if not isinstance(item, dict):
            operations.append(item)
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
        operations.append(operation)
    normalized["operations"] = operations
    if not normalized.get("status"):
        normalized["status"] = "ready" if operations else "needs_human_review"
    if not normalized.get("summary"):
        normalized["summary"] = normalized.get("recommendation") or normalized.get("no_action_reason") or "执行优化方案"
    for key in ("summary", "validation", "risk", "no_action_reason"):
        if normalized.get(key) is not None and not isinstance(normalized.get(key), str):
            normalized[key] = _human_text(normalized.get(key))
    return normalized


def _human_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    return str(value)
