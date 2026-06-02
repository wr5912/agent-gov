from __future__ import annotations

import re
from typing import Any

from ..records.json_types import JsonObject
from .feedback_output_records import NormalizedTaskContext


def normalize_task_context_payload(value: Any) -> JsonObject:
    if not isinstance(value, dict):
        return {}
    list_keys = {"tool_names", "query_ids", "alert_ids", "case_ids", "asset_ids", "dates", "affected_fields"}
    context: JsonObject = {}
    for key, item in value.items():
        if item is None:
            continue
        if isinstance(item, list):
            context[key] = [str(entry).strip() for entry in item if entry is not None and str(entry).strip()]
        elif isinstance(item, dict):
            context[key] = item
        else:
            text = str(item).strip()
            if text:
                context[key] = [text] if key in list_keys else text
    normalized = NormalizedTaskContext.model_validate(context).to_payload()
    return {key: item for key, item in normalized.items() if item not in ("", [], None)}


def task_context_has_external_specificity(context: JsonObject) -> bool:
    has_interface = bool(
        context.get("tool_name")
        or context.get("tool_names")
        or context.get("api_name")
        or context.get("api_path")
        or context.get("endpoint")
    )
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


def infer_external_task_context(item: JsonObject, plan: JsonObject | None = None) -> JsonObject:
    context = normalize_task_context_payload(item.get("task_context"))
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

    return NormalizedTaskContext.model_validate(
        {key: value for key, value in context.items() if value not in ("", [], None)}
    ).to_payload()


def external_context_target(context: JsonObject) -> str:
    return str(
        context.get("endpoint")
        or context.get("api_name")
        or context.get("tool_name")
        or context.get("mcp_server")
        or context.get("external_system")
        or "外部系统"
    )


def external_task_objective(owner: str, target: str, observed_issue: str) -> str:
    issue = f"，修复{observed_issue}" if observed_issue else ""
    return f"推动 {owner} 修复 {target} 的数据或接口能力{issue}，让 Agent 后续可获得完整可靠输入。"


def external_task_actions(owner: str, target: str, observed_issue: str) -> list[str]:
    issue = f"：{observed_issue}" if observed_issue else ""
    return [
        f"请 {owner} 核查 {target} 的数据覆盖、筛选条件和返回逻辑{issue}",
        "修复后使用关联反馈场景验证 Agent 能获得完整数据并完成回答。",
    ]


def external_task_acceptance_criteria(context: JsonObject) -> list[str]:
    target = external_context_target(context)
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


def external_task_validation(context: JsonObject) -> str:
    target = external_context_target(context)
    return f"修复后重新运行本批次回归测试，并核查 {target} 返回结果是否满足验收标准。"


def _external_context_source_text(item: JsonObject, plan: JsonObject | None = None) -> str:
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


def _api_info_from_tool_name(tool_name: str) -> JsonObject:
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


def _external_expected_fix(context: JsonObject) -> str:
    server = str(context.get("mcp_server") or context.get("external_system") or "").strip()
    target = external_context_target(context)
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


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    text = str(value).strip()
    return [text] if text else []
