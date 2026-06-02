from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast

from ..errors import AgentOutputParseError
from ..records.json_types import JsonObject
from ..schema_versions import (
    ATTRIBUTION_OUTPUT_SCHEMA_VERSION,
    EXECUTION_PLAN_OUTPUT_SCHEMA_VERSION,
    FEEDBACK_EVAL_CASE_GENERATION_OUTPUT_SCHEMA_VERSION,
    FEEDBACK_OPTIMIZATION_PLAN_OUTPUT_SCHEMA_VERSION,
    PROPOSAL_OUTPUT_SCHEMA_VERSION,
    REGRESSION_IMPACT_ANALYSIS_OUTPUT_SCHEMA_VERSION,
)


NATURAL_LANGUAGE_CHINESE_RULE = (
    "自然语言输出要求：除 schema 字段名、枚举值、ID、路径、代码标识符、MCP/tool 名称外，"
    "所有面向人的说明文本必须使用简体中文；如果输入证据中已有英文自然语言，必须用中文转述，不要原样复制英文说明。\n"
)


def attribution_prompt(input_path: str) -> str:
    return (
        "你是反馈闭环中的归因分析智能体。只读取 attribution input 指定的证据路径，"
        "输出归因分析内容。系统会在后端通过 DSPy formatter 和 Pydantic schema "
        f"把你的输出格式化为 {ATTRIBUTION_OUTPUT_SCHEMA_VERSION}；"
        "你可以输出自然语言分析、结构化要点或 JSON 片段，但必须包含足够信息供系统格式化。\n\n"
        f"输入文件：{input_path}\n\n"
        f"{NATURAL_LANGUAGE_CHINESE_RULE}"
        "其中 evidence_refs[].reason、responsibility_boundary.reason、rationale 必须使用简体中文。\n\n"
        "归因内容应覆盖：schema_version、feedback_case_id、attribution_job_id、status、problem_type、"
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


def proposal_prompt(input_path: str, *, input_payload: JsonObject | None = None, attribution_output: JsonObject | None = None) -> str:
    embedded_context = ""
    if input_payload is not None and attribution_output is not None:
        embedded_context = (
            "\n\n以下是完整输入上下文，不需要调用工具读取文件。\n"
            f"proposal_input_json:\n{json.dumps(input_payload, ensure_ascii=False, indent=2)}\n\n"
            f"attribution_output_json:\n{json.dumps(attribution_output, ensure_ascii=False, indent=2)}\n\n"
        )
    return (
        "你是反馈闭环中的优化方案生成智能体。只读取 proposal input、已校验归因输出和允许的版本清单，"
        "输出优化方案内容。系统会在后端通过 DSPy formatter 和 Pydantic schema "
        f"把你的输出格式化为 {PROPOSAL_OUTPUT_SCHEMA_VERSION}；"
        "你可以输出自然语言建议、结构化要点或 JSON 片段，但必须包含足够信息供系统格式化。\n\n"
        f"输入文件：{input_path}\n\n"
        "执行方式：如果提示词提供了 proposal_input_json 和 attribution_output_json，则直接使用这些内容，"
        "不要调用工具。否则先读取输入文件，再读取其中的 attribution_output_path；如需确认当前版本，最多再读取 "
        "main_agent_manifest_path。不要继续探索 workspace，不要读取未在输入文件列出的路径。\n\n"
        f"{NATURAL_LANGUAGE_CHINESE_RULE}"
        "其中 proposals[].title/recommendation/expected_effect/validation/risk、"
        "external_guidance[].recommendation/reason、no_action_reason 必须使用简体中文。\n\n"
        "建议内容应覆盖：schema_version、feedback_case_id、proposal_job_id、status、proposals、"
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


def batch_optimization_plan_prompt(input_path: str, *, input_payload: JsonObject | None = None) -> str:
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
        "系统会在后端通过 DSPy formatter 和 Pydantic schema 把你的输出格式化为 "
        f"{FEEDBACK_OPTIMIZATION_PLAN_OUTPUT_SCHEMA_VERSION}。"
        "你可以输出自然语言方案、结构化任务要点或 JSON 片段，但必须包含足够信息供系统格式化、"
        "路径校验和持久化；不要假设后端会根据空泛 rationale 再补全任务字段。\n\n"
        f"{NATURAL_LANGUAGE_CHINESE_RULE}"
        "title、summary、recommendation、expected_effect、validation、risk、rationale、"
        "tasks[].title/description/objective/recommendation/recommended_actions/acceptance_criteria/expected_effect/validation/risk、"
        "blocked_items[].title/reason/recommendation 必须使用简体中文。\n\n"
        "方案内容必须覆盖：schema_version、batch_id、status、title、summary、problem_types、confidence、"
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
        "对外部 MCP 工具、数据源、知识库或 SOC 流程问题，只要能定位到系统、工具或 API、具体问题描述，"
        "就必须生成 external_webhook 任务。例如 sec-ops-data 的 list_vulnerabilities 存在 2026 年漏洞数据缺失时，"
        "应输出通知 sec-ops-data 工具提供方修复数据覆盖的 external_webhook 任务，而不是放入 blocked_items。"
        "如果无法明确到外部对象、接口、工具、ID 或受影响字段，不要生成 external_webhook 任务，改写入 blocked_items 并说明缺什么。\n\n"
        "blocked_items 只用于不能执行且不能派发的项；不要用 manual_review 表示可执行任务。"
        "开发人员阅读优化方案后点击执行即表示同意执行对应 task，因此不要设计二次审批字段。\n\n"
        "如果 batch_plan_input_json.regeneration_instruction 非空，可作为本次重新生成的开发人员补充意图；"
        "但它不能覆盖 schema、中文输出、证据约束、target_policy 和可执行性要求。"
        f"{embedded_context}"
    )


def execution_plan_prompt(input_path: str, *, input_payload: JsonObject | None = None) -> str:
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
        "系统会在后端通过 DSPy formatter 和 Pydantic schema "
        f"把你的输出格式化为 {EXECUTION_PLAN_OUTPUT_SCHEMA_VERSION}。"
        "你可以输出自然语言执行方案、结构化操作要点或 JSON 片段，但必须包含足够信息供系统格式化。"
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


def eval_case_generation_prompt(input_path: str, *, input_payload: JsonObject | None = None) -> str:
    embedded_context = ""
    if input_payload is not None:
        embedded_context = (
            "\n\n以下是完整输入上下文，不需要调用工具读取文件。\n"
            f"eval_case_generation_input_json:\n{json.dumps(input_payload, ensure_ascii=False, indent=2)}\n"
        )
    return (
        "你是反馈闭环中的评估用例治理智能体 eval-case-governor。你的职责是基于反馈来源、"
        "已校验归因和优化建议，生成可复测原问题的评估用例草案。\n\n"
        f"输入文件：{input_path}\n\n"
        "系统会在后端通过 DSPy formatter 和 Pydantic schema 把你的输出格式化为 "
        f"{FEEDBACK_EVAL_CASE_GENERATION_OUTPUT_SCHEMA_VERSION}。"
        "你可以输出自然语言、结构化要点或 JSON 片段，但必须包含足够信息供系统格式化。\n\n"
        f"{NATURAL_LANGUAGE_CHINESE_RULE}"
        "每个 eval case 必须覆盖 prompt、expected_behavior、checks_json 和 labels；"
        "prompt 应复现用户原始输入或最接近的反馈场景，expected_behavior 应描述修复后应满足的行为。"
        "不要凭空编造证据中不存在的业务事实；证据不足时输出 no_action_reason 并说明需要人工补充。"
        f"{embedded_context}"
    )


def regression_impact_analysis_prompt(input_path: str, *, input_payload: JsonObject | None = None) -> str:
    embedded_context = ""
    if input_payload is not None:
        embedded_context = (
            "\n\n以下是完整输入上下文，不需要调用工具读取文件。\n"
            f"regression_impact_input_json:\n{json.dumps(input_payload, ensure_ascii=False, indent=2)}\n"
        )
    return (
        "你是反馈闭环中的回归影响分析智能体 regression-impact-analyzer。你的职责是根据 eval_run、"
        "gate_result 和每个 eval item 的结果，判断本次变更对长期回归资产的影响。\n\n"
        f"输入文件：{input_path}\n\n"
        "系统会在后端通过 DSPy formatter 和 Pydantic schema 把你的输出格式化为 "
        f"{REGRESSION_IMPACT_ANALYSIS_OUTPUT_SCHEMA_VERSION}。"
        "输出应覆盖 result_status、gate_result、impacted_assets、recommendations、summary、risk_assessment 和 next_steps。"
        "不能判断是否阻断时，status 使用 needs_human_review 并填写 no_action_reason。\n\n"
        f"{NATURAL_LANGUAGE_CHINESE_RULE}"
        "summary、risk_assessment、recommendations、next_steps、no_action_reason 必须使用简体中文。"
        f"{embedded_context}"
    )


def extract_json_object(text: str, *, expected_schema_version: str | None = None) -> JsonObject:
    stripped = text.strip()
    if not stripped:
        raise AgentOutputParseError("empty agent output")
    candidates = extract_json_candidates(stripped)
    if expected_schema_version:
        for candidate in reversed(candidates):
            if candidate.get("schema_version") == expected_schema_version:
                return candidate
    if candidates:
        return candidates[0]
    raise AgentOutputParseError("agent output did not contain a JSON object")


def extract_json_candidates(text: str) -> list[JsonObject]:
    decoder = json.JSONDecoder()
    candidates: list[JsonObject] = []
    seen: set[str] = set()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            loaded, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(loaded, dict):
            _append_json_candidate(candidates, seen, cast(JsonObject, loaded))
    for loaded in _repair_json_candidates(text):
        _append_json_candidate(candidates, seen, loaded)
    return candidates


def _append_json_candidate(candidates: list[JsonObject], seen: set[str], loaded: JsonObject) -> None:
    key = json.dumps(loaded, ensure_ascii=False, sort_keys=True)
    if key in seen:
        return
    seen.add(key)
    candidates.append(loaded)


def _repair_json_candidates(text: str) -> list[JsonObject]:
    try:
        import json_repair  # type: ignore[import-untyped]
    except Exception:
        return []

    repaired: list[JsonObject] = []
    blocks = re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
    for block in blocks:
        try:
            loaded = json_repair.loads(block)
        except Exception:
            continue
        if isinstance(loaded, dict):
            repaired.append(cast(JsonObject, loaded))
    return repaired


def read_json(path: str | Path) -> JsonObject:
    loaded = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise AgentOutputParseError(f"Expected JSON object: {path}")
    return cast(JsonObject, loaded)
