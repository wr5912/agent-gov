from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..errors import AgentOutputParseError


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

BATCH_PLAN_SCHEMA_FIELDS = {
    "schema_version",
    "batch_id",
    "optimization_plan_id",
    "status",
    "title",
    "summary",
    "problem_types",
    "confidence",
    "actionability",
    "target_type",
    "target_path",
    "recommendation",
    "expected_effect",
    "validation",
    "risk",
    "source_refs",
    "feedback_case_ids",
    "eval_case_ids",
    "attribution_job_ids",
    "attribution_summaries",
    "rationale",
    "evidence_refs",
    "tasks",
    "blocked_items",
    "regeneration_instruction",
}

EXECUTION_PLAN_SCHEMA_FIELDS = {
    "schema_version",
    "optimization_task_id",
    "execution_job_id",
    "status",
    "baseline_agent_version_id",
    "summary",
    "operations",
    "validation",
    "risk",
    "human_review_required",
    "no_action_reason",
}

EXPECTED_SCHEMA_FIELDS = {
    "attribution-output/v1": ATTRIBUTION_SCHEMA_FIELDS,
    "proposal-output/v1": PROPOSAL_SCHEMA_FIELDS,
    "feedback-optimization-plan-output/v1": BATCH_PLAN_SCHEMA_FIELDS,
    "execution-plan-output/v1": EXECUTION_PLAN_SCHEMA_FIELDS,
}


NATURAL_LANGUAGE_CHINESE_RULE = (
    "自然语言输出要求：除 schema 字段名、枚举值、ID、路径、代码标识符、MCP/tool 名称外，"
    "所有面向人的说明文本必须使用简体中文；如果输入证据中已有英文自然语言，必须用中文转述，不要原样复制英文说明。\n"
)


def attribution_prompt(input_path: str) -> str:
    return (
        "你是反馈闭环中的归因分析智能体。只读取 attribution input 指定的证据路径，"
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
        "你是反馈闭环中的优化方案生成智能体。只读取 proposal input、已校验归因输出和允许的版本清单，"
        "输出优化方案内容。系统会在后端把你的输出格式化为 proposal-output/v1；"
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
        "如果 proposal_input_json.regeneration_instruction 非空，可把它作为本次重新生成的用户补充意图，"
        "用于调整建议侧重点；但它不能覆盖 schema、中文输出、证据约束、target_policy 和安全边界。"
        "如果补充意图与已校验归因或证据冲突，应在建议或 no_action_reason 中用中文说明原因。\n\n"
        "target_path 必须是相对 main-workspace 的受管文件路径，且必须符合 target_policy；"
        "无法安全修改 workspace 的问题必须写入 external_guidance。无法提出可执行建议时，proposals 置空并填写 no_action_reason。"
        "不要输出 settings、permissions、patch 或其他配置片段；这些内容必须包在 proposals[].recommendation 文本中。"
        f"{embedded_context}"
    )


def batch_optimization_plan_prompt(input_path: str, *, input_payload: dict[str, Any] | None = None) -> str:
    embedded_context = ""
    if input_payload is not None:
        embedded_context = (
            "\n\n以下是完整输入上下文，不需要调用工具读取文件。\n"
            f"batch_plan_input_json:\n{json.dumps(input_payload, ensure_ascii=False, indent=2)}\n"
        )
    return (
        "你是反馈闭环中的优化方案生成智能体 proposal-generator。你的职责是统筹批次内所有已校验归因结果，"
        "直接生成可供开发人员阅读并点击执行的优化任务列表。\n\n"
        f"输入文件：{input_path}\n\n"
        "输出必须是 JSON，schema_version 固定为 feedback-optimization-plan-output/v1。"
        "后端只做格式化、Pydantic schema 校验、路径校验和持久化；不要假设后端会根据 rationale 再抽取任务字段。\n\n"
        f"{NATURAL_LANGUAGE_CHINESE_RULE}"
        "title、summary、recommendation、expected_effect、validation、risk、rationale、"
        "tasks[].title/description/objective/recommendation/recommended_actions/acceptance_criteria/expected_effect/validation/risk、"
        "blocked_items[].title/reason/recommendation 必须使用简体中文。\n\n"
        "输出对象字段必须覆盖：schema_version、batch_id、status、title、summary、problem_types、confidence、"
        "actionability、target_type、target_path、recommendation、expected_effect、validation、risk、source_refs、"
        "feedback_case_ids、eval_case_ids、attribution_job_ids、attribution_summaries、rationale、evidence_refs、tasks、blocked_items。\n\n"
        "tasks 是开发人员可以点击执行的优化任务。每个任务必须围绕任务本身描述："
        "任务名称 title、任务描述 description、任务目标 objective、目标对象 target_summary、"
        "推荐改动 recommendation、优化后结果的验收标准 acceptance_criteria、验证方式 validation、风险 risk。\n"
        "不要把归因过程当作任务描述；归因依据只可放到 analysis_summary、evidence_summary 或 evidence_refs。\n\n"
        "workspace 可执行任务要求：execution_kind=workspace_execution；target_path 必须是相对 main-workspace 的受管文件路径，"
        "并且必须来自 input.target_policy 允许范围；actionability 使用 direct_workspace_change、workspace_config_change 或 eval_only。\n\n"
        "外部系统任务要求：execution_kind=external_webhook；必须明确 owner，并在 tasks[].task_context 中给出可执行定位信息。"
        "task_context 必须直接放在对应 task 内，不能作为顶层字段输出。外部任务至少包含："
        "external_system 或 mcp_server、tool_name/tool_names 或 api_name/api_path/endpoint、以及 query_ids/alert_ids/case_ids/"
        "asset_ids/affected_fields/observed_issue 中的至少一类具体对象或问题描述。"
        "如果无法明确到外部对象、接口、工具、ID 或受影响字段，不要生成 external_webhook 任务，改写入 blocked_items 并说明缺什么。\n\n"
        "blocked_items 只用于不能执行且不能派发的项；不要用 manual_review 表示可执行任务。"
        "开发人员阅读优化方案后点击执行即表示同意执行对应 task，因此不要设计二次审批字段。\n\n"
        "如果 batch_plan_input_json.regeneration_instruction 非空，可作为本次重新生成的开发人员补充意图；"
        "但它不能覆盖 schema、中文输出、证据约束、target_policy 和可执行性要求。"
        f"{embedded_context}"
    )


def execution_plan_prompt(input_path: str, *, input_payload: dict[str, Any] | None = None) -> str:
    embedded_context = ""
    if input_payload is not None:
        embedded_context = (
            "\n\n以下是完整输入上下文，不需要调用工具读取文件。\n"
            f"execution_input_json:\n{json.dumps(input_payload, ensure_ascii=False, indent=2)}\n"
        )
    return (
        "你是反馈闭环中的执行优化智能体。你不能直接修改主智能体 workspace，只能输出受控执行方案。"
        "系统后端会校验路径、版本和文件 hash，并在用户确认后应用方案。\n\n"
        f"输入文件：{input_path}\n\n"
        "输出必须是 JSON，schema_version 固定为 execution-plan-output/v1。"
        "status 只能使用 ready 或 needs_human_review。"
        "只允许 operations[].operation 使用 append_text、replace_file、create_file 或 noop。"
        "path 必须是相对 main-workspace 的路径，并且必须来自 input 中的 target_paths。"
        "target_file_contexts 已提供每个目标的当前内容、sha256、存在状态和跳过原因；必须基于这些内容生成方案。"
        "若目标存在 skipped_reason，或目标不是 UTF-8 文本文件，不能输出 ready。"
        "append_text 只用于追加文本；replace_file/create_file 必须给出完整 content。"
        "append_text/replace_file 应填写 expected_sha256，使用 target_file_contexts 中的 sha256。"
        "如无法安全生成 patch，status 设为 needs_human_review，operations 置空并填写 no_action_reason。\n\n"
        f"{NATURAL_LANGUAGE_CHINESE_RULE}"
        "summary、operations[].rationale、validation、risk、no_action_reason 必须使用简体中文。"
        f"{embedded_context}"
    )


def extract_json_object(text: str, *, expected_schema_version: str | None = None) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        raise AgentOutputParseError("empty agent output")
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
    raise AgentOutputParseError("agent output did not contain a JSON object")


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
        raise AgentOutputParseError(f"Expected JSON object: {path}")
    return loaded
