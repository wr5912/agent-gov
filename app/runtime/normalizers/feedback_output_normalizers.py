from __future__ import annotations

import json

from ..json_types import JsonObject
from .feedback_output_records import (
    NormalizedAttributionOutput,
    NormalizedExecutionOperation,
    NormalizedExecutionPlanOutput,
    NormalizedFeedbackEvalCaseGenerationOutput,
    NormalizedGeneratedEvalCase,
)


def normalize_attribution_output(payload: JsonObject) -> JsonObject:
    normalized: JsonObject = dict(payload)
    problem_type_aliases = {
        "tool_usage_deficiency": "tool_data_quality",
        "tool_usage_gap": "tool_data_quality",
        "tool_call_gap": "tool_data_quality",
        "agent_behavior": "instruction_gap",
        "reasoning_gap": "reasoning_error",
        "reasoning_flaw": "reasoning_error",
        "logic_error": "reasoning_error",
        "inference_error": "reasoning_error",
        "flawed_reasoning": "reasoning_error",
        "faulty_inference": "reasoning_error",
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
            item if isinstance(item, dict) else {"type": "evidence_file", "id": str(item), "reason": "归因分析智能体引用了该证据文件。"}
            for item in evidence_refs
        ]

    responsibility_boundary = normalized.get("responsibility_boundary")
    if isinstance(responsibility_boundary, str):
        normalized["responsibility_boundary"] = {
            "owner": responsibility_boundary,
            "reason": "归因分析智能体输出了责任边界标签，系统归一化为结构化对象。",
        }

    return NormalizedAttributionOutput.model_validate(normalized).to_payload()


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
    if "eval_cases" not in normalized and isinstance(normalized.get("eval_case"), dict):
        normalized["eval_cases"] = [normalized["eval_case"]]
    cases: list[NormalizedGeneratedEvalCase] = []
    for item in normalized.get("eval_cases") or []:
        if not isinstance(item, dict):
            continue
        case = dict(item)
        case.setdefault("schema_version", "feedback-eval-case/v1")
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
