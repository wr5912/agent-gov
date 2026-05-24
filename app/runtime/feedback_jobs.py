from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def attribution_prompt(input_path: str) -> str:
    return (
        "你是反馈闭环中的归因分析 Agent。只读取 attribution input 指定的证据路径，"
        "输出且只输出一个 JSON 对象，必须符合 attribution-output/v1。\n\n"
        f"输入文件：{input_path}\n\n"
        "必须包含字段：schema_version、feedback_case_id、attribution_job_id、status、problem_type、"
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
        "responsibility_boundary 必须是对象，形如 {\"owner\":\"main_agent_workspace\",\"reason\":\"...\"}。"
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
        "输出且只输出一个 JSON 对象，必须符合 proposal-output/v1。\n\n"
        f"输入文件：{input_path}\n\n"
        "执行方式：如果提示词提供了 proposal_input_json 和 attribution_output_json，则直接使用这些内容，"
        "不要调用工具。否则先读取输入文件，再读取其中的 attribution_output_path；如需确认当前版本，最多再读取 "
        "main_agent_manifest_path。不要继续探索 workspace，不要读取未在输入文件列出的路径。\n\n"
        "必须包含字段：schema_version、feedback_case_id、proposal_job_id、status、proposals、"
        "external_guidance、no_action_reason。\n"
        "status: completed | needs_human_review\n"
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
    candidates = _json_object_candidates(stripped)
    if expected_schema_version:
        for candidate in reversed(candidates):
            if candidate.get("schema_version") == expected_schema_version:
                return candidate
    if candidates:
        return candidates[0]
    raise ValueError("agent output did not contain a JSON object")


def _json_object_candidates(text: str) -> list[dict[str, Any]]:
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


def read_json(path: str | Path) -> dict[str, Any]:
    loaded = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return loaded
