from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ATTRIBUTION_SCHEMA_FIELDS = {
    "schema_version",
    "feedback_case_id",
    "attribution_job_id",
    "status",
    "problem_type",
    "optimization_object_type",
    "actionability",
    "confidence",
    "human_review_required",
    "evidence_refs",
    "responsibility_boundary",
    "rationale",
    "recommended_next_step",
}

PROPOSAL_SCHEMA_FIELDS = {
    "schema_version",
    "feedback_case_id",
    "proposal_job_id",
    "status",
    "proposals",
    "external_guidance",
    "no_action_reason",
}

EXPECTED_SCHEMA_FIELDS = {
    "attribution-output/v1": ATTRIBUTION_SCHEMA_FIELDS,
    "proposal-output/v1": PROPOSAL_SCHEMA_FIELDS,
}


NATURAL_LANGUAGE_CHINESE_RULE = (
    "自然语言输出要求：除 schema 字段名、枚举值、ID、路径、代码标识符、MCP/tool 名称外，"
    "所有面向人的说明文本必须使用简体中文；如果输入证据中已有英文自然语言，必须用中文转述，不要原样复制英文说明。\n"
)


def attribution_prompt(input_path: str) -> str:
    return (
        "你是反馈闭环中的归因分析 Agent。只读取 attribution input 指定的证据路径，"
        "输出归因分析内容。系统会在后端把你的输出格式化为 attribution-output/v1；"
        "你可以输出自然语言分析或 JSON，但必须包含足够信息供系统格式化。\n\n"
        f"输入文件：{input_path}\n\n"
        f"{NATURAL_LANGUAGE_CHINESE_RULE}"
        "其中 evidence_refs[].reason、responsibility_boundary.reason、rationale 必须使用简体中文。\n\n"
        "归因内容应覆盖字段：schema_version、feedback_case_id、attribution_job_id、status、problem_type、"
        "optimization_object_type、actionability、confidence、human_review_required、evidence_refs、"
        "responsibility_boundary、rationale、recommended_next_step。\n\n"
        "字段取值必须严格使用以下枚举：\n"
        "status: completed | needs_human_review\n"
        "problem_type: evidence_gap | tool_misuse | tool_unavailable | tool_data_quality | output_style_issue | "
        "instruction_gap | skill_gap | mcp_description_gap | runtime_error | external_soc_process_issue | "
        "user_misunderstanding | insufficient_information\n"
        "optimization_object_type: main_agent_claude_md | skill | subagent | mcp_config | mcp_description | "
        "output_style | eval_case | runtime_code | external_mcp_service | soc_process | not_actionable\n"
        "actionability: direct_workspace_change | workspace_config_change | eval_only | external_guidance | "
        "runtime_fix | needs_human_analysis | not_actionable\n"
        "confidence: low | medium | high\n"
        "recommended_next_step: generate_proposal | needs_human_review | stop\n\n"
        "evidence_refs 必须是对象数组，每项形如 {\"type\":\"evidence_file\",\"id\":\"tool_calls.json\",\"reason\":\"...\"}；"
        "responsibility_boundary 必须能表达 owner 和 reason。证据不足时明确说明需要人工复核，"
        "不要为了凑结论而补充证据中没有的信息。"
    )


def proposal_prompt(input_path: str, *, input_payload: dict[str, Any] | None = None, attribution_output: dict[str, Any] | None = None) -> str:
    embedded_context = ""
    if input_payload is not None and attribution_output is not None:
        embedded_context = (
            "\n\n以下是完整输入上下文，不需要调用工具读取文件。\n"
            f"proposal_input_json:\n{json.dumps(input_payload, ensure_ascii=False, indent=2)}\n\n"
            f"attribution_output_json:\n{json.dumps(attribution_output, ensure_ascii=False, indent=2)}\n\n"
        )
    return (
        "你是反馈闭环中的优化建议 Agent。只读取 proposal input、已校验归因输出和允许的版本清单，"
        "输出优化建议内容。系统会在后端把你的输出格式化为 proposal-output/v1；"
        "你可以输出自然语言建议或 JSON，但必须包含足够信息供系统格式化。\n\n"
        f"输入文件：{input_path}\n\n"
        "执行方式：如果提示词提供了 proposal_input_json 和 attribution_output_json，则直接使用这些内容，"
        "不要调用工具。否则先读取输入文件，再读取其中的 attribution_output_path；如需确认当前版本，最多再读取 "
        "main_agent_manifest_path。不要继续探索 workspace，不要读取未在输入文件列出的路径。\n\n"
        f"{NATURAL_LANGUAGE_CHINESE_RULE}"
        "其中 proposals[].title/recommendation/expected_effect/validation/risk、"
        "external_guidance[].recommendation/reason、no_action_reason 必须使用简体中文。\n\n"
        "建议内容应覆盖字段：schema_version、feedback_case_id、proposal_job_id、status、proposals、"
        "external_guidance、no_action_reason。\n"
        "status: completed | needs_human_review\n"
        "external_guidance[].owner 必填，用于标识外部责任方或系统；不要用 target 替代 owner。\n"
        "proposal.actionability 和 external_guidance.actionability 必须使用：direct_workspace_change | "
        "workspace_config_change | eval_only | external_guidance | runtime_fix | needs_human_analysis | not_actionable\n\n"
        "target_path 必须是相对 main-workspace 的路径，且必须命中 allowed_target_paths；"
        "无法安全修改 workspace 的问题必须写入 external_guidance。无法提出可执行建议时，proposals 置空并填写 no_action_reason。"
        "不要输出 settings、permissions、patch 或其他配置片段；这些内容必须包在 proposals[].recommendation 文本中。"
        f"{embedded_context}"
    )


def extract_json_object(text: str, *, expected_schema_version: str | None = None) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        raise ValueError("empty agent output")
    candidates = extract_json_candidates(stripped)
    if expected_schema_version:
        for candidate in reversed(candidates):
            if candidate.get("schema_version") == expected_schema_version:
                return candidate
        scored = sorted(
            candidates,
            key=lambda item: _schema_candidate_score(item, expected_schema_version),
            reverse=True,
        )
        if scored and _schema_candidate_score(scored[0], expected_schema_version) > 0:
            return scored[0]
    if candidates:
        return candidates[0]
    raise ValueError("agent output did not contain a JSON object")


def extract_json_candidates(text: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            loaded, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(loaded, dict):
            candidates.append(loaded)
    return candidates


def _schema_candidate_score(candidate: dict[str, Any], expected_schema_version: str) -> int:
    fields = EXPECTED_SCHEMA_FIELDS.get(expected_schema_version)
    if not fields:
        return 0
    score = len(set(candidate) & fields)
    if candidate.get("schema_version") == expected_schema_version:
        score += len(fields)
    return score


def read_json(path: str | Path) -> dict[str, Any]:
    loaded = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return loaded
