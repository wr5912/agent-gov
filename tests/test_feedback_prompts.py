import json

from app.runtime.prompts.feedback_prompt_contexts import (
    build_attribution_prompt_context,
    build_execution_prompt_context,
    build_improvement_optimization_prompt_context,
    build_regression_test_design_prompt_context,
)
from app.runtime.prompts.feedback_prompts import (
    attribution_prompt,
    execution_plan_prompt,
    improvement_optimization_plan_prompt,
    regression_test_design_prompt,
)


def test_improvement_optimization_prompt_delegates_wire_format_to_dspy():
    prompt = improvement_optimization_plan_prompt(
        prompt_context={
            "improvement": {"title": "OCSF 时间窗口误判"},
            "attribution": {"summary": "时间字段语义误映射"},
        },
    )

    assert "improvement_optimization_plan_prompt_context" in prompt
    assert "不要输出 JSON 代码块" in prompt
    assert "formatter 会转换为结构化模型" in prompt
    assert "summary" in prompt
    assert "changes" in prompt
    assert "risk_level" in prompt
    assert "batch_id" not in prompt
    assert "optimization_task_id" not in prompt


def test_prompt_context_builders_prune_backend_and_boundary_fields():
    attribution_context = build_attribution_prompt_context(
        {
            "schema_version": "attribution-input/v1",
            "job_id": "job-hidden",
            "feedback_case": {
                "title": "回答未核查配置",
                "problem": "工具数据不完整",
                "agent_id": "sec-ops-data",
            },
        }
    )
    optimization_context = build_improvement_optimization_prompt_context(
        {
            "schema_version": "optimization-input/v1",
            "job_id": "job-hidden",
            "improvement": {"improvement_id": "imp-hidden", "title": "映射误判"},
            "normalized_feedback": {"problem": "时间窗口误判"},
            "attribution": {"summary": "OCSF 字段语义错误"},
        }
    )
    execution_context = build_execution_prompt_context(
        {
            "schema_version": "execution-input/v1",
            "execution_job_id": "exec-hidden",
            "proposal": {
                "title": "补充配置读取要求",
                "recommendation": "修改 CLAUDE.md。",
                "target_path": "CLAUDE.md",
                "actionability": "direct_workspace_change",
            },
            "target_paths": ["CLAUDE.md"],
            "target_policy": {"type": "managed", "workspace_root": "/main-workspace"},
            "target_file_contexts": [{"path": "CLAUDE.md", "exists": True, "content_text": "A" * 30_000}],
        }
    )
    regression_context = build_regression_test_design_prompt_context(
        {
            "job_id": "job-hidden",
            "scope_kind": "improvement",
            "scope_id": "imp-hidden",
            "feedback_cases": [
                {
                    "feedback_case": {"title": "回答未核查配置", "status": "pending_review"},
                    "source_run": {"message": "当前 workspace 配置是什么？", "agent_activity": {"large": "ignored"}},
                    "attribution_output": {"problem_type": "tool_misuse", "rationale": "Agent 未读取配置。"},
                    "optimization_plan": {"summary": "增加回归用例。"},
                }
            ],
        }
    )

    serialized = json.dumps(
        [attribution_context, optimization_context, execution_context, regression_context],
        ensure_ascii=False,
    )
    assert "schema_version" not in serialized
    assert "job_id" not in serialized
    assert "execution_job_id" not in serialized
    assert "workspace_root" not in serialized
    assert "scope_kind" not in serialized
    assert "scope_id" not in serialized
    assert "tool_misuse" in serialized
    assert "CLAUDE.md" in serialized
    assert "truncated" in serialized


def test_target_agent_context_is_preserved_as_locator_not_config_snapshot():
    target_context = {
        "agent_id": "main-agent",
        "workspace_dir": "/data/business-agents/main-agent/workspace",
        "claude_path": "/data/business-agents/main-agent/workspace/CLAUDE.md",
        "settings_path": "/data/business-agents/main-agent/workspace/.claude/settings.json",
        "mcp_path": "/data/business-agents/main-agent/workspace/.mcp.json",
        "skills_glob": "/data/business-agents/main-agent/workspace/.claude/skills/*/SKILL.md",
        "agents_glob": "/data/business-agents/main-agent/workspace/.claude/agents/*.md",
        "allowed_evidence_roots": ["/data/business-agents/main-agent/workspace"],
        "forbidden_evidence_roots": ["/governor-workspace"],
        "CLAUDE.md": "SHOULD_NOT_INLINE_FULL_PROMPT",
    }

    attribution_context = build_attribution_prompt_context(
        {"feedback_case": {"agent_id": "main-agent", "problem": "Bash 权限问题"}, "target_agent_context": target_context}
    )
    optimization_context = build_improvement_optimization_prompt_context(
        {
            "improvement": {"agent_id": "main-agent", "title": "Bash 权限问题"},
            "target_agent_context": target_context,
        }
    )
    serialized = json.dumps([attribution_context, optimization_context], ensure_ascii=False)

    assert "/data/business-agents/main-agent/workspace/.claude/settings.json" in serialized
    assert "/governor-workspace" in serialized
    assert "SHOULD_NOT_INLINE_FULL_PROMPT" not in serialized


def test_feedback_prompts_do_not_expose_formatter_implementation_details():
    prompts = [
        attribution_prompt(),
        improvement_optimization_plan_prompt(),
        execution_plan_prompt(),
        regression_test_design_prompt(),
    ]

    forbidden = (
        "DSPy formatter",
        "Pydantic schema",
        "把你的输出格式化为",
        "schema_version",
        "batch_id",
        "optimization_task_id",
        "proposal_id",
        "execution_job_id",
    )
    for prompt in prompts:
        for text in forbidden:
            assert text not in prompt


def test_execution_prompt_requires_exact_backend_allowed_target_path():
    prompt = execution_plan_prompt()

    assert "path 必须逐字符复制 input.target_paths 中的某一项" in prompt
    assert "禁止补前缀、改写路径或根据 proposal 猜测新路径" in prompt
    assert "target_paths 之外的目标" in prompt


def test_only_grounding_stages_invite_workspace_tool_reads():
    attribution = attribution_prompt(prompt_context={"target_agent_context": {"agent_id": "main-agent"}})
    optimization = improvement_optimization_plan_prompt(
        prompt_context={"target_agent_context": {"agent_id": "main-agent"}}
    )
    execution = execution_plan_prompt(prompt_context={"target_paths": ["CLAUDE.md"]})
    regression = regression_test_design_prompt(prompt_context={"feedback": {"problem": "冲突证据"}})

    assert "read-business-agent-config skill" in attribution
    assert "target_agent_context.workspace_dir" in optimization
    assert "不要调用 Skill、Read、Glob、Grep、Bash、Write、Edit 或其他工具" in execution
    assert "不要调用 Skill、Read、Glob、Grep、Bash、Write、Edit 或其他工具" in regression
    assert "可用 Read/Glob/Grep 按需读取" not in execution
    assert "可用 Read/Glob/Grep 按需读取" not in regression


def test_feedback_prompts_are_structured():
    for prompt in (
        attribution_prompt(),
        improvement_optimization_plan_prompt(),
        execution_plan_prompt(),
        regression_test_design_prompt(),
    ):
        assert prompt.startswith("## 角色\n")
        assert "\n\n## 输入\n" in prompt
        assert "\n\n## 工作方式\n" in prompt
        assert "\n\n## 业务信息要点\n" in prompt
        assert "\n\n## 约束\n" in prompt
        assert "输入文件：" not in prompt


def test_feedback_prompts_spell_out_current_business_information_points():
    expected = {
        "attribution": (
            "problem_type",
            "optimization_object_type",
            "actionability",
            "responsibility_boundary",
            "counter_evidence",
            "verification_suggestions",
        ),
        "optimization_plan": ("summary", "changes", "risk_level"),
        "execution": ("operations[].operation", "expected_sha256", "content 或 append_text", "no_action_reason"),
        "regression_test_design": ("tests", "test_code", "test_intent", "assertion_rationale", "no_action_reason"),
    }
    prompts = {
        "attribution": attribution_prompt(),
        "optimization_plan": improvement_optimization_plan_prompt(),
        "execution": execution_plan_prompt(),
        "regression_test_design": regression_test_design_prompt(),
    }
    for prompt_name, required_texts in expected.items():
        prompt = prompts[prompt_name]
        for text in required_texts:
            assert text in prompt

    assert "文件路径" in prompts["regression_test_design"] and "由后端绑定" in prompts["regression_test_design"]
    assert "必须使用真实换行符" in prompts["regression_test_design"]
    assert "不得自行定义、导入、实例化或覆盖 agent fixture" in prompts["regression_test_design"]
    assert "Python 标准库、pytest 和 agentgov_testkit" in prompts["regression_test_design"]
    assert "tests 最多包含一个 item" in prompts["regression_test_design"]
    assert "不超过 60 行" in prompts["regression_test_design"]
    assert "只定义验证本次修复所必需的一个同步 test_* 函数" in prompts["regression_test_design"]
    assert "不得定义其他 fixture" in prompts["regression_test_design"]
    assert "assert not result.errors" in prompts["regression_test_design"]
    assert "result.errors == []" in prompts["regression_test_design"]
    assert 'normalized_text = "".join(result.text.split())' in prompts["regression_test_design"]
    assert "Markdown 排版" in prompts["regression_test_design"]
    assert "每个独立可观察的修复结果都必须有一条单独的正向断言" in prompts["regression_test_design"]
    assert "将置信度降为低" in prompts["regression_test_design"]
    assert "test_intent 或 assertion_rationale" in prompts["regression_test_design"]
    assert "仅依据以下已给定事实回答，不调用任何工具或读取文件。" in prompts["regression_test_design"]
    assert 'assert result.raw["agent_activity"]["tool_calls"] == []' in prompts["regression_test_design"]
    assert "不能只断言相反结果未出现" in prompts["regression_test_design"]
    assert "不得用 any(...)、A or B" in prompts["regression_test_design"]
    assert "from agentgov_testkit import agent" in prompts["regression_test_design"]
    assert "测试必须可重复" in prompts["regression_test_design"]
    assert "不得改写成依赖未声明 MCP、数据库或网络数据的查询" in prompts["regression_test_design"]


def test_attribution_and_optimization_prompts_require_chinese_user_facing_text():
    attribution = attribution_prompt()
    optimization = improvement_optimization_plan_prompt()

    assert "所有面向人的说明文本必须使用简体中文" in attribution
    assert "证据引用原因" in attribution
    assert "责任边界" in attribution
    assert "所有面向人的说明文本必须使用简体中文" in optimization


def test_attribution_and_optimization_prompts_forbid_governor_workspace_as_business_agent_evidence():
    attribution = attribution_prompt()
    optimization = improvement_optimization_plan_prompt()

    assert "target_agent_context.workspace_dir" in attribution
    assert "/governor-workspace 只代表治理 Agent 自身配置" in attribution
    assert "target_agent_context.workspace_dir" in optimization
    assert "/governor-workspace 只代表治理 Agent 自身配置" in optimization
