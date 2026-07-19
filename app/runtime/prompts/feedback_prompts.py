from __future__ import annotations

import json

from ..json_types import JsonObject

NATURAL_LANGUAGE_CHINESE_RULE = (
    "自然语言输出要求：除 schema 字段名、枚举值、ID、路径、代码标识符、MCP/tool 名称外，"
    "所有面向人的说明文本必须使用简体中文；如果输入证据中已有英文自然语言，必须用中文转述，不要原样复制英文说明。\n"
)


def _structured_prompt(*sections: tuple[str, str]) -> str:
    return "\n\n".join(f"## {title}\n{body.strip()}" for title, body in sections if body.strip())


def attribution_prompt(*, prompt_context: JsonObject | None = None) -> str:
    return _structured_prompt(
        (
            "角色",
            "你是反馈闭环中的归因分析智能体。你的职责是基于后端提供的 attribution context 判断反馈问题归因。",
        ),
        (
            "输入",
            "输入上下文由后端从 SQLite、证据包和 Langfuse 观测数据构造；判断问题是否出在目标业务 Agent 配置上时，"
            "可用 Read/Glob/Grep 按需读取其 workspace 原始配置（见 read-business-agent-config skill）。",
        ),
        (
            "工作方式",
            "先阅读反馈、运行轨迹、工具调用、配置快照和证据内容，必要时用 Read 读取目标业务 Agent 的 workspace 配置核对真相，"
            "再判断问题类型、责任边界和下一步。证据不足时明确说明需要人工复核，不要为了凑结论而补充证据中没有的信息。",
        ),
        (
            "业务信息要点",
            "输出中必须能直接读出：\n"
            "- problem_type：反馈问题类别，说明为什么属于该类；"
            "区分数据缺口（evidence_gap/insufficient_information）、推理问题（reasoning_error：数据与工具充分但推断或结论本身有误）、"
            "工具问题（tool_*）和执行资产问题（instruction_gap/skill_gap/mcp_description_gap）；不要把推断错误伪装成数据缺口或工具问题。\n"
            "- optimization_object_type：应优化的对象或 not_actionable，说明判断依据。\n"
            "- actionability：可执行性判断，区分 workspace 修改、配置修改、评估用例、外部处理、运行时修复、人工分析或不可行动。\n"
            "- confidence 和 human_review_required：置信度与是否需要人工复核。\n"
            "- evidence_refs：引用哪些证据文件、证据引用原因、证据支持了什么结论。\n"
            "- responsibility_boundary：责任方 owner、责任边界和责任边界 reason。\n"
            "- rationale：完整归因理由。\n"
            "- counter_evidence 和 uncertainty_factors：反证、冲突证据或仍不确定的因素。\n"
            "- verification_suggestions：后续应如何复核或回归验证。\n"
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
            "如果有效 .mcp.json 或 .claude/settings.json 中仍存在占位符或错误运行环境路径，通常归因到 runtime-bootstrap 初始化或 Workspace 配置。\n"
            "只有在 MCP 配置已实例化且无占位符、MCP 仍连接失败或服务返回异常时，才优先判定 external_mcp_service。\n"
            "MAX_TURNS 达上限若伴随 MCP failed 或 MCP 配置未解析占位符，应视为放大器，不要把 turns 默认值当作唯一根因。",
        ),
        (
            "业务 Agent 配置",
            "目标业务 Agent 的权威路径只以输入上下文 target_agent_context 为准。归因到执行资产问题"
            "（instruction_gap/skill_gap/mcp_description_gap）或工具/权限问题前，必须用 Read/Glob/Grep 按需读取"
            "target_agent_context.workspace_dir 下的原始配置（CLAUDE.md、.claude/settings.json、.mcp.json、.claude/skills）"
            "确认当前配置是否缺失、冲突或描述不当，不要脱离实际配置臆断。"
            "/governor-workspace 只代表治理 Agent 自身配置；除非本次问题对象明确是 governor，否则不得把"
            "/governor-workspace 下的文件作为目标业务 Agent 配置证据。",
        ),
        ("约束", NATURAL_LANGUAGE_CHINESE_RULE),
        ("输入上下文", _prompt_context_section("attribution_prompt_context", prompt_context)),
    )


def _prompt_context_section(context_name: str, prompt_context: JsonObject | None) -> str:
    if prompt_context is None:
        return ""
    return f"以下是后端构造的输入上下文。\n{context_name}:\n{json.dumps(prompt_context, ensure_ascii=False, indent=2)}\n"


def improvement_optimization_plan_prompt(*, prompt_context: JsonObject | None = None) -> str:
    return _structured_prompt(
        (
            "角色",
            "你是改进事项闭环中的优化方案生成智能体。你的职责是基于已确认的反馈整理和归因分析，生成事项级优化方案。",
        ),
        (
            "输入",
            "输入上下文由后端从当前改进事项、NormalizedFeedback 和 Attribution 构造；提方案前可用 Read 按需读取目标业务 Agent 的 workspace 原始配置，确保 changes 针对真实存在的配置资产。",
        ),
        (
            "工作方式",
            "先确认问题、责任边界和证据，再提出可以被后续执行阶段消费的优化方向。"
            "本阶段只产出方案，不推进状态、不创建批次、不创建任务队列、不生成外部治理 webhook。",
        ),
        (
            "业务信息要点",
            "输出中必须能直接读出：summary、changes 和 risk_level。\n"
            "summary 用一段话说明优化策略、目标和收益。\n"
            "changes 是变更项列表，每项必须包含 target 和 change；target 应是 prompt、skill、subagent、mcp_config、"
            "runtime_config、tests 或其他明确 Workspace 资产，change 写清要改什么以及为什么。\n"
            "risk_level 说明低/中/高风险或等价中文描述。",
        ),
        (
            "约束",
            f"{NATURAL_LANGUAGE_CHINESE_RULE}"
            "不要输出后端系统 ID、路由名、队列名、工具调用参数或后端版本字段。"
            "不要输出 JSON 代码块；用自然语言小节或列表表达即可，formatter 会转换为结构化模型。",
        ),
        (
            "业务 Agent 配置",
            "目标业务 Agent 的权威路径只以输入上下文 target_agent_context 为准。"
            "提方案前用 Read/Glob/Grep 按需读取 target_agent_context.workspace_dir 下的原始配置"
            "（CLAUDE.md/.claude/settings.json/.mcp.json/.claude/skills）。"
            "changes[].target 与 change 必须针对真实存在的配置资产提出具体改动"
            "（例如改 CLAUDE.md 某段、补/改某个 skill、调整 settings 权限或 MCP），不要提出与当前配置无关或已存在的改动。"
            "/governor-workspace 只代表治理 Agent 自身配置；除非本次问题对象明确是 governor，否则不得把"
            "/governor-workspace 下的文件作为目标业务 Agent 配置或优化对象。",
        ),
        ("输入上下文", _prompt_context_section("improvement_optimization_plan_prompt_context", prompt_context)),
    )


def execution_plan_prompt(*, prompt_context: JsonObject | None = None) -> str:
    return _structured_prompt(
        (
            "角色",
            "你是反馈闭环中的执行优化智能体。你不能直接修改主智能体 workspace，只能输出受控执行方案。",
        ),
        ("输入", "输入上下文由后端从 SQLite 和受管 workspace 文件快照构造；不需要读取 job 输入文件或临时目录。"),
        (
            "工作方式",
            "基于 input 中的 proposal、target_paths 和 target_file_contexts 生成操作方案。"
            "target_file_contexts 已提供每个目标的当前内容、sha256、存在状态和跳过原因；必须基于这些内容生成方案。"
            "不要调用 Skill、Read、Glob、Grep、Bash、Write、Edit 或其他工具。",
        ),
        (
            "业务信息要点",
            "输出中必须能直接读出：status、summary、operations、validation、risk、"
            "human_review_required 和 no_action_reason。\n"
            "任务标识、执行作业标识和修复前版本由后端保存时补齐；Agent 不需要复述任何系统 ID。\n"
            "每个 operation 必须说明 operations[].operation、path、expected_sha256、content 或 append_text、rationale。"
            "summary 要说明本次准备改什么；validation 要说明如何验证；risk 要说明可能的退化或人工注意点。",
        ),
        (
            "操作约束",
            "只允许 operations[].operation 使用 append_text、replace_file、create_file 或 noop。\n"
            "path 必须逐字符复制 input.target_paths 中的某一项；禁止补前缀、改写路径或根据 proposal 猜测新路径。\n"
            "proposal 若提到 target_paths 之外的目标，必须忽略该目标；没有安全可执行目标时返回 needs_human_review。\n"
            "若目标存在 skipped_reason，或目标不是 UTF-8 文本文件，不能输出 ready。\n"
            "append_text 只用于追加文本；replace_file/create_file 必须给出完整 content。\n"
            "append_text/replace_file 应填写 expected_sha256，使用 target_file_contexts 中的 sha256。\n"
            "如无法安全生成 patch，status 设为 needs_human_review，operations 置空并填写 no_action_reason。",
        ),
        (
            "约束",
            f"{NATURAL_LANGUAGE_CHINESE_RULE}summary、operations[].rationale、validation、risk、no_action_reason 必须使用简体中文。",
        ),
        ("输入上下文", _prompt_context_section("execution_prompt_context", prompt_context)),
    )


def regression_test_design_prompt(*, prompt_context: JsonObject | None = None) -> str:
    return _structured_prompt(
        (
            "角色",
            "你是四阶段改进治理中的回归测试代码生成智能体。你的职责是基于原始反馈、已校验归因和优化方案，生成可直接执行的 pytest 测试代码。",
        ),
        ("输入", "输入上下文由后端从 SQLite 中的反馈、归因和优化方案构造；不需要读取 job 输入文件或临时目录。"),
        (
            "工作方式",
            "先定位原始失败场景和修复目标，再把原始输入写入测试代码，并使用业务 Agent 的 agent fixture 验证修复后的可观察结果。"
            "所需事实已在输入上下文中，不要调用 Skill、Read、Glob、Grep、Bash、Write、Edit 或其他工具。",
        ),
        (
            "业务信息要点",
            "输出中只能包含 tests 或 no_action_reason。tests 最多包含一个 item；该 item 表示本次改进事项的一个完整 pytest 模块，"
            "必须同时包含 test_code、test_intent 和 assertion_rationale。\n"
            "test_code 是不带 Markdown 围栏的完整 UTF-8 Python 模块；必须使用真实换行符，不能把整段代码编码成含字面量反斜杠加 n 的单行字符串。"
            "模块应保持单焦点且不超过 60 行，只定义验证本次修复所必需的一个同步 test_* 函数；不得添加模块级或函数级长篇说明。"
            "test_* 函数必须在参数中声明平台提供的 agent fixture；不得自行定义、导入、实例化或覆盖 agent fixture，也不得定义其他 fixture，"
            "agent fixture 由 pytest plugin 按参数名注入，模块不得写 `from agentgov_testkit import agent`，通常无需为 fixture 添加任何 import。"
            "调用 result = agent.run(原始输入)，第一条结果断言必须逐字写成 `assert not result.errors`；errors 是 tuple，禁止写 `result.errors == []`。"
            "自然语言回答会因 Markdown 排版在标签内产生空格或换行；对固定业务词的断言必须先写 `normalized_text = \"\".join(result.text.split())`，"
            "再使用 normalized_text 对每个关键业务结果分别做无空白字面量断言。"
            "原始反馈、已确认整理和优化方案中每个独立可观察的修复结果都必须有一条单独的正向断言；"
            "例如同时要求展示来源 A、展示来源 B、标记冲突、将置信度降为低时，四项都要分别断言；"
            "test_intent 或 assertion_rationale 中提到该结果不能代替可执行断言。"
            "必须直接断言应出现的目标结果，不能只断言相反结果未出现。"
            "不得用 any(...)、A or B 或宽泛关键词候选列表代替明确结果断言。\n"
            "测试必须可重复：原始反馈已给出判断所需事实时，应把这些事实完整写入 agent.run 输入，不得改写成依赖未声明 MCP、数据库或网络数据的查询。"
            "对这种事实已完整内嵌的自包含用例，agent.run 输入必须逐字包含「仅依据以下已给定事实回答，不调用任何工具或读取文件。」，"
            "并在 `assert not result.errors` 之后逐字写入 `assert result.raw[\"agent_activity\"][\"tool_calls\"] == []`，证明测试未偷偷依赖外部工具。"
            "只有输入上下文提供了可运行的固定外部资源引用时，测试才能依赖该资源。\n"
            "只允许导入 Python 标准库、pytest 和 agentgov_testkit。只断言请求无错误、回答非空、常量或检查点字符串非空不构成业务回归验证。"
            "不得使用 skip、xfail、agent.invoke 或任意目标路径。\n"
            "Agent ID、文件路径、反馈关联、变更编号、commit、时间戳和生命周期由后端绑定。证据不足时只输出 no_action_reason。",
        ),
        (
            "约束",
            f"{NATURAL_LANGUAGE_CHINESE_RULE}"
            "test_intent 和 assertion_rationale 必须清晰说明测试目的及断言与修复效果的关系。"
            "不要凭空编造证据中不存在的业务事实；证据不足时输出 no_action_reason 并说明需要人工补充。",
        ),
        ("输入上下文", _prompt_context_section("regression_test_design_prompt_context", prompt_context)),
    )
