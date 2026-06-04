from __future__ import annotations

import json

from ..json_types import JsonObject


NATURAL_LANGUAGE_CHINESE_RULE = (
    "自然语言输出要求：除 schema 字段名、枚举值、ID、路径、代码标识符、MCP/tool 名称外，"
    "所有面向人的说明文本必须使用简体中文；如果输入证据中已有英文自然语言，必须用中文转述，不要原样复制英文说明。\n"
)


def _structured_prompt(*sections: tuple[str, str]) -> str:
    return "\n\n".join(f"## {title}\n{body.strip()}" for title, body in sections if body.strip())


def attribution_prompt(input_path: str) -> str:
    return _structured_prompt(
        (
            "角色",
            "你是反馈闭环中的归因分析智能体。你的职责是基于 attribution input 指定的证据路径，判断反馈问题归因。",
        ),
        (
            "输入",
            f"输入文件：{input_path}\n只读取 attribution input 指定的证据路径，不读取未列出的路径。",
        ),
        (
            "工作方式",
            "先读取反馈、运行轨迹、工具调用、配置快照和证据文件，再判断问题类型、责任边界和下一步。"
            "证据不足时明确说明需要人工复核，不要为了凑结论而补充证据中没有的信息。",
        ),
        (
            "业务信息要点",
            "输出中必须能直接读出：\n"
            "- feedback_case_id 和 attribution_job_id 对应的反馈与归因任务。\n"
            "- problem_type：反馈问题类别，说明为什么属于该类。\n"
            "- optimization_object_type：应优化的对象或 not_actionable，说明判断依据。\n"
            "- actionability：可执行性判断，区分 workspace 修改、配置修改、评估用例、外部处理、运行时修复、人工分析或不可行动。\n"
            "- confidence 和 human_review_required：置信度与是否需要人工复核。\n"
            "- evidence_refs：引用哪些证据文件、证据引用原因、证据支持了什么结论。\n"
            "- responsibility_boundary：责任方 owner、责任边界和责任边界 reason。\n"
            "- rationale：完整归因理由。\n"
            "- recommended_next_step：生成优化方案、人工复核或停止，并说明原因。",
        ),
        (
            "MCP 和运行时归因规则",
            "MCP/运行时问题必须优先读取 runtime_config_summary.json、effective_mcp_config.json、"
            "mcp_connection_summary.json、runtime_env_snapshot.json、workspace_placeholder_summary.json。\n"
            "只有 effective_mcp_config.json 显示选中的 MCP config 或 MCP config path 仍有 unresolved placeholder 时，"
            "通常归因到 MCP 配置或运行时配置问题。\n"
            "workspace_placeholder_summary.json 中其他占位符按来源归因：\n"
            "- .claude/settings.json 若影响权限、sandbox 或网络域名，通常归因到运行时代码或配置。\n"
            "- mcp_servers/**/sample*.json 若作为 MCP 工具返回数据污染回答，通常归因到外部 MCP 服务数据质量。\n"
            "- README、docs、*.example 只作为说明或示例，通常归 not_actionable 或 insufficient_information，除非证据显示示例被当作运行配置使用。\n"
            "- *.sh 中的 ${VAR:-default} 通常是 shell 默认值语法，不应仅因出现占位符归因，必须结合执行失败证据判断。\n"
            "如果证据显示本应使用 .mcp.local.json 或显式 CLAUDE_MCP_CONFIG_PATH，但运行时仍选择 template .mcp.json，"
            "通常归因到运行时配置选择逻辑。\n"
            "只有在 MCP 配置已实例化且无占位符、MCP 仍连接失败或服务返回异常时，才优先判定 external_mcp_service。\n"
            "MAX_TURNS 达上限若伴随 MCP failed 或 MCP 配置未解析占位符，应视为放大器，不要把 turns 默认值当作唯一根因。",
        ),
        ("约束", NATURAL_LANGUAGE_CHINESE_RULE),
    )


def proposal_generator_prompt(input_path: str, *, input_payload: JsonObject | None = None) -> str:
    embedded_context = ""
    if input_payload is not None:
        embedded_context = (
            "\n\n以下是完整输入上下文，不需要调用工具读取文件。\n"
            f"optimization_plan_input_json:\n{json.dumps(input_payload, ensure_ascii=False, indent=2)}\n"
        )
    return _structured_prompt(
        (
            "角色",
            "你是反馈闭环中的优化方案生成智能体 proposal-generator。你的职责是统筹输入中的所有已校验归因结果，"
            "直接生成可供开发人员阅读并点击执行的优化任务列表。",
        ),
        ("输入", f"输入文件：{input_path}"),
        (
            "工作方式",
            "围绕输入中的反馈、归因输出、回归用例和 target_policy 生成任务。单条反馈也会以 size=1 优化批次输入。"
            "不要假设后端会根据空泛理由再补全业务含义；不要把归因过程当作任务描述。",
        ),
        (
            "业务信息要点",
            "顶层方案必须能直接读出：title、summary、problem_types、confidence、actionability、target_type、target_path、"
            "recommendation、expected_effect、validation、risk、source_refs、feedback_case_ids、eval_case_ids、"
            "attribution_job_ids、attribution_summaries、rationale 和 evidence_refs。\n"
            "tasks 是开发人员可以点击执行的优化任务。每个 task 必须围绕任务本身描述："
            "tasks[].title、description、objective、target_summary、recommendation、recommended_actions、"
            "acceptance_criteria、expected_effect、validation、risk、analysis_summary、evidence_summary、evidence_refs。"
            "归因依据只可放到 analysis_summary、evidence_summary 或 evidence_refs。\n"
            "blocked_items 只用于不能执行且不能派发的项；每个 blocked item 必须说明 title、reason、recommendation 和缺失条件。",
        ),
        (
            "workspace 可执行任务",
            "workspace 可执行任务要求：execution_kind=workspace_execution；target_path 必须是相对 main-workspace 的受管文件路径，"
            "并且必须来自 input.target_policy 允许范围；actionability 使用 direct_workspace_change、workspace_config_change 或 eval_only。",
        ),
        (
            "外部系统任务",
            "外部系统任务要求：execution_kind=external_webhook；必须明确 owner，并在 tasks[].task_context 中给出可执行定位信息。\n"
            "task_context 必须直接放在对应 task 内，不能作为顶层字段输出。外部任务至少包含："
            "external_system 或 mcp_server、tool_name/tool_names 或 api_name/api_path/endpoint、以及 query_ids/alert_ids/case_ids/"
            "asset_ids/affected_fields/observed_issue 中的至少一类具体对象或问题描述。\n"
            "对外部 MCP 工具、数据源、知识库或 SOC 流程问题，只要能定位到系统、工具或 API、具体问题描述，"
            "就必须生成 external_webhook 任务。例如 sec-ops-data 的 list_vulnerabilities 存在 2026 年漏洞数据缺失时，"
            "应输出通知 sec-ops-data 工具提供方修复数据覆盖的 external_webhook 任务，而不是放入 blocked_items。\n"
            "如果无法明确到外部对象、接口、工具、ID 或受影响字段，不要生成 external_webhook 任务，改写入 blocked_items 并说明缺什么。",
        ),
        (
            "约束",
            f"{NATURAL_LANGUAGE_CHINESE_RULE}"
            "title、summary、recommendation、expected_effect、validation、risk、rationale、"
            "tasks[].title/description/objective/recommendation/recommended_actions/acceptance_criteria/expected_effect/validation/risk、"
            "blocked_items[].title/reason/recommendation 必须使用简体中文。\n"
            "不要用 manual_review 表示可执行任务。开发人员阅读优化方案后点击执行即表示同意执行对应 task，因此不要设计二次审批字段。\n"
            "如果 optimization_plan_input_json.regeneration_instruction 非空，可作为本次重新生成的开发人员补充意图；"
            "但它不能覆盖中文输出、证据约束、target_policy 和可执行性要求。",
        ),
        ("输入上下文", embedded_context),
    )


def execution_plan_prompt(input_path: str, *, input_payload: JsonObject | None = None) -> str:
    embedded_context = ""
    if input_payload is not None:
        embedded_context = (
            "\n\n以下是完整输入上下文，不需要调用工具读取文件。\n"
            f"execution_input_json:\n{json.dumps(input_payload, ensure_ascii=False, indent=2)}\n"
        )
    return _structured_prompt(
        (
            "角色",
            "你是反馈闭环中的执行优化智能体。你不能直接修改主智能体 workspace，只能输出受控执行方案。",
        ),
        ("输入", f"输入文件：{input_path}"),
        (
            "工作方式",
            "基于 input 中的 proposal、target_paths 和 target_file_contexts 生成操作方案。"
            "target_file_contexts 已提供每个目标的当前内容、sha256、存在状态和跳过原因；必须基于这些内容生成方案。",
        ),
        (
            "业务信息要点",
            "输出中必须能直接读出：optimization_task_id、execution_job_id、status、summary、operations、validation、risk、"
            "human_review_required 和 no_action_reason。\n"
            "每个 operation 必须说明 operations[].operation、path、expected_sha256、content 或 append_text、rationale。"
            "summary 要说明本次准备改什么；validation 要说明如何验证；risk 要说明可能的退化或人工注意点。",
        ),
        (
            "操作约束",
            "只允许 operations[].operation 使用 append_text、replace_file、create_file 或 noop。\n"
            "path 必须是相对 main-workspace 的路径，并且必须来自 input 中的 target_paths。\n"
            "若目标存在 skipped_reason，或目标不是 UTF-8 文本文件，不能输出 ready。\n"
            "append_text 只用于追加文本；replace_file/create_file 必须给出完整 content。\n"
            "append_text/replace_file 应填写 expected_sha256，使用 target_file_contexts 中的 sha256。\n"
            "如无法安全生成 patch，status 设为 needs_human_review，operations 置空并填写 no_action_reason。",
        ),
        (
            "约束",
            f"{NATURAL_LANGUAGE_CHINESE_RULE}"
            "summary、operations[].rationale、validation、risk、no_action_reason 必须使用简体中文。",
        ),
        ("输入上下文", embedded_context),
    )


def eval_case_generation_prompt(input_path: str, *, input_payload: JsonObject | None = None) -> str:
    embedded_context = ""
    if input_payload is not None:
        embedded_context = (
            "\n\n以下是完整输入上下文，不需要调用工具读取文件。\n"
            f"eval_case_generation_input_json:\n{json.dumps(input_payload, ensure_ascii=False, indent=2)}\n"
        )
    return _structured_prompt(
        (
            "角色",
            "你是反馈闭环中的评估用例治理智能体 eval-case-governor。你的职责是基于反馈来源、已校验归因和优化建议，"
            "生成可复测原问题的评估用例草案。",
        ),
        ("输入", f"输入文件：{input_path}"),
        (
            "工作方式",
            "先定位反馈原始场景、应复测的问题、修复后应满足的行为，再生成可放入回归资产候选层的 eval cases。",
        ),
        (
            "业务信息要点",
            "输出中必须能直接读出：job_id、scope_kind、scope_id、status、eval_cases、results 和 no_action_reason。\n"
            "每个 eval case 必须覆盖 prompt、expected_behavior、checks_json 和 labels；"
            "prompt 应复现用户原始输入或最接近的反馈场景，expected_behavior 应描述修复后应满足的行为。"
            "checks_json 应表达可检查的行为点，labels 应标识问题类型、目标对象或风险域。"
            "能确定来源时补充 source_kind、source_id、source_refs、attribution_summary 或 proposal_summary。",
        ),
        (
            "约束",
            f"{NATURAL_LANGUAGE_CHINESE_RULE}"
            "prompt、expected_behavior、checks_json 中面向人的说明、labels 的中文含义必须清晰。"
            "不要凭空编造证据中不存在的业务事实；证据不足时输出 no_action_reason 并说明需要人工补充。",
        ),
        ("输入上下文", embedded_context),
    )


def regression_impact_analysis_prompt(input_path: str, *, input_payload: JsonObject | None = None) -> str:
    embedded_context = ""
    if input_payload is not None:
        embedded_context = (
            "\n\n以下是完整输入上下文，不需要调用工具读取文件。\n"
            f"regression_impact_input_json:\n{json.dumps(input_payload, ensure_ascii=False, indent=2)}\n"
        )
    return _structured_prompt(
        (
            "角色",
            "你是反馈闭环中的回归影响分析智能体 regression-impact-analyzer。你的职责是根据 eval_run、gate_result "
            "和每个 eval item 的结果，判断本次变更对长期回归资产的影响。",
        ),
        ("输入", f"输入文件：{input_path}"),
        (
            "工作方式",
            "先阅读 eval_run、gate_result 和 item 快照，再判断本次变更是否影响现有回归资产、是否需要新增或调整资产、"
            "以及是否需要人工复核。",
        ),
        (
            "业务信息要点",
            "输出中必须能直接读出：eval_run_id、status、result_status、gate_result、impacted_assets、recommendations、"
            "summary、risk_assessment、next_steps 和 no_action_reason。\n"
            "gate_result 要说明阻断或放行依据；impacted_assets 要说明受影响的 eval case、asset、状态或摘要；"
            "recommendations 要说明应新增、调整、保留或人工复核哪些回归资产；next_steps 要说明后续动作。"
            "无法判断时说明需要人工复核及缺少的信息。",
        ),
        (
            "约束",
            f"{NATURAL_LANGUAGE_CHINESE_RULE}"
            "summary、risk_assessment、recommendations、next_steps、no_action_reason 必须使用简体中文。",
        ),
        ("输入上下文", embedded_context),
    )
