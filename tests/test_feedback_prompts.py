import json

from app.runtime.prompts.feedback_prompt_contexts import (
    build_eval_case_generation_prompt_context,
    build_execution_prompt_context,
    build_proposal_prompt_context,
    build_regression_impact_prompt_context,
)
from app.runtime.prompts.feedback_prompts import (
    attribution_prompt,
    eval_case_generation_prompt,
    execution_plan_prompt,
    proposal_generator_prompt,
    regression_impact_analysis_prompt,
)


def test_proposal_generator_prompt_embeds_pruned_context_when_available():
    prompt = proposal_generator_prompt(
        "/tmp/input.json",
        prompt_context={
            "regeneration_instruction": "优先修改 triage-alert skill。",
        },
    )

    assert "optimization_plan_prompt_context" in prompt
    assert "不需要调用工具读取完整输入文件" in prompt
    assert "regeneration_instruction" in prompt
    assert "optimization_plan_input_json" not in prompt
    assert "schema_version" not in prompt
    assert "job_id" not in prompt
    assert "batch_id" not in prompt
    assert "不能覆盖中文输出、证据约束、target_policy 和可执行性要求" in prompt


def test_proposal_generator_prompt_delegates_wire_format_to_dspy():
    prompt = proposal_generator_prompt(
        "/tmp/batch-plan.json",
        prompt_context={"feedback_case_count": 1},
    )

    assert "optimization_plan_prompt_context" in prompt
    assert "不要输出 JSON、代码块、schema payload" in prompt
    assert "只输出 Markdown 小节或列表形式的结构化业务要点" in prompt
    assert "最终输出必须是一个 JSON 对象" not in prompt
    assert "不要输出 Markdown 方案、表格、代码围栏或解释性前后缀" not in prompt
    assert "系统会优先直接校验该 JSON" not in prompt
    assert "external_webhook" in prompt
    assert "blocked_items" in prompt


def test_prompt_context_builders_prune_backend_and_boundary_fields():
    proposal_context = build_proposal_prompt_context(
        {
            "schema_version": "feedback-optimization-plan-input/v1",
            "job_id": "fbp-test",
            "batch_id": "fob-test",
            "feedback_case_ids": ["fbc-test"],
            "main_agent_version_id": "agent-version",
            "main_agent_repository_path": "/main-workspace",
            "target_policy": {"type": "main_workspace_managed_full_with_excludes", "workspace_root": "/main-workspace"},
            "attribution_outputs": [
                {
                    "feedback_case_id": "fbc-test",
                    "attribution_job_id": "fba-test",
                    "problem_type": "tool_misuse",
                    "optimization_object_type": "main_agent_claude_md",
                    "actionability": "direct_workspace_change",
                    "confidence": "high",
                    "human_review_required": False,
                    "responsibility_boundary": {"owner": "main_agent_workspace", "reason": "需要补充规则"},
                    "rationale": "Agent 未读取配置。",
                    "recommended_next_step": "generate_proposal",
                }
            ],
        }
    )
    execution_context = build_execution_prompt_context(
        {
            "schema_version": "execution-input/v1",
            "execution_job_id": "fbe-test",
            "optimization_task_id": "fot-test",
            "baseline_agent_version_id": "agent-version",
            "main_agent_repository_path": "/main-workspace",
            "proposal": {
                "proposal_id": "fop-test",
                "status": "approved",
                "title": "补充配置读取要求",
                "recommendation": "修改 CLAUDE.md。",
                "target_path": "CLAUDE.md",
                "actionability": "direct_workspace_change",
            },
            "target_paths": ["CLAUDE.md"],
            "target_policy": {"type": "main_workspace_managed_full_with_excludes", "workspace_root": "/main-workspace"},
            "target_file_contexts": [
                {
                    "path": "CLAUDE.md",
                    "exists": True,
                    "sha256": "abc",
                    "content_text": "A" * 30_000,
                }
            ],
        }
    )
    regression_context = build_regression_impact_prompt_context(
        {
            "schema_version": "regression-impact-analysis-input/v1",
            "job_id": "riaj-test",
            "eval_run_id": "evr-test",
            "eval_run": {
                "eval_run_id": "evr-test",
                "status": "completed",
                "result_status": "failed",
                "gate_result": {"status": "failed", "blocked_case_ids": ["evc-block"]},
                "items": [{"eval_run_item_id": "eri-test", "status": "failed", "answer_summary": "未读取配置。"}],
            },
        }
    )

    serialized = json.dumps([proposal_context, execution_context, regression_context], ensure_ascii=False)
    assert "schema_version" not in serialized
    assert "job_id" not in serialized
    assert "batch_id" not in serialized
    assert "execution_job_id" not in serialized
    assert "optimization_task_id" not in serialized
    assert "baseline_agent_version_id" not in serialized
    assert "main_agent_repository_path" not in serialized
    assert "workspace_root" not in serialized
    assert "eval_run_id" not in serialized
    assert "eval_run_item_id" not in serialized
    assert "tool_misuse" in serialized
    assert "CLAUDE.md" in serialized
    assert "failed" in serialized
    assert "truncated" in serialized


def test_eval_case_generation_prompt_context_keeps_business_grounding_without_full_records():
    context = build_eval_case_generation_prompt_context(
        {
            "schema_version": "feedback-eval-case-generation-input/v1",
            "job_id": "evg-test",
            "scope_kind": "optimization_batch",
            "scope_id": "fob-test",
            "feedback_cases": [
                {
                    "feedback_case": {
                        "feedback_case_id": "fbc-test",
                        "title": "回答未核查配置",
                        "status": "pending_proposal",
                        "created_at": "2026-06-06T00:00:00Z",
                    },
                    "source_run": {
                        "run_id": "run-test",
                        "session_id": "sess-test",
                        "message": "当前 workspace 配置是什么？",
                        "answer_summary": "回答来自记忆。",
                        "agent_activity": {"large": "ignored"},
                    },
                    "attribution_output": {
                        "problem_type": "tool_misuse",
                        "optimization_object_type": "main_agent_claude_md",
                        "actionability": "direct_workspace_change",
                        "confidence": "high",
                        "human_review_required": False,
                        "responsibility_boundary": {"owner": "main_agent_workspace", "reason": "未读取配置"},
                        "rationale": "Agent 未读取配置。",
                    },
                    "optimization_plan": {
                        "title": "补充配置读取要求",
                        "recommendation": "增加回归用例。",
                        "created_at": "2026-06-06T00:00:00Z",
                    },
                }
            ],
        }
    )

    serialized = json.dumps(context, ensure_ascii=False)
    assert "回答未核查配置" in serialized
    assert "当前 workspace 配置是什么？" in serialized
    assert "tool_misuse" in serialized
    assert "agent_activity" not in serialized
    assert "scope_kind" not in serialized
    assert "job_id" not in serialized


def test_feedback_prompts_do_not_expose_formatter_implementation_details():
    prompts = [
        attribution_prompt("/tmp/attribution.json"),
        proposal_generator_prompt("/tmp/batch-plan.json"),
        execution_plan_prompt("/tmp/execution.json"),
        eval_case_generation_prompt("/tmp/eval-case.json"),
        regression_impact_analysis_prompt("/tmp/regression-impact.json"),
    ]

    forbidden = (
        "DSPy formatter",
        "Pydantic schema",
        "把你的输出格式化为",
        "feedback-optimization-plan-output/v1",
        "attribution-output/v1",
        "proposal-output/v1",
        "execution-plan-output/v1",
        "feedback-eval-case-generation-output/v1",
        "regression-impact-analysis-output/v1",
    )
    for prompt in prompts:
        for text in forbidden:
            assert text not in prompt


def test_feedback_prompts_are_structured():
    prompts = [
        attribution_prompt("/tmp/attribution.json"),
        proposal_generator_prompt("/tmp/batch-plan.json"),
        execution_plan_prompt("/tmp/execution.json"),
        eval_case_generation_prompt("/tmp/eval-case.json"),
        regression_impact_analysis_prompt("/tmp/regression-impact.json"),
    ]

    for prompt in prompts:
        assert prompt.startswith("## 角色\n")
        assert "\n\n## 输入\n" in prompt
        assert "\n\n## 工作方式\n" in prompt
        assert "\n\n## 业务信息要点\n" in prompt
        assert "\n\n## 约束\n" in prompt
        assert "必须包含足够清晰的业务信息" not in prompt
        assert "请提供足够清晰的任务语义" not in prompt
        assert "请输出足够清晰" not in prompt


def test_feedback_prompts_do_not_require_agents_to_repeat_backend_context_fields():
    attribution = attribution_prompt("/tmp/attribution.json")
    proposal = proposal_generator_prompt("/tmp/batch-plan.json")
    execution = execution_plan_prompt("/tmp/execution.json")
    eval_case_generation = eval_case_generation_prompt("/tmp/eval-case.json")
    regression_impact = regression_impact_analysis_prompt("/tmp/regression-impact.json")

    assert "feedback_case_id 和 attribution_job_id 对应的反馈与归因任务" not in attribution
    assert "顶层方案必须能直接读出：title、summary、problem_types、confidence、actionability、target_type、target_path、recommendation、expected_effect、validation、risk、source_refs" not in proposal
    assert "输出中必须能直接读出：optimization_task_id、execution_job_id" not in execution
    assert "输出中必须能直接读出：job_id、scope_kind、scope_id" not in eval_case_generation
    assert "输出中必须能直接读出：eval_run_id、status、result_status、gate_result" not in regression_impact
    for prompt in (proposal, execution, eval_case_generation, regression_impact):
        assert "Agent 不需要复述任何系统 ID" in prompt
    assert "批次标识、计划标识、创建时间、来源关联、反馈范围、评估范围和归因关联" in proposal
    assert "任务标识、执行作业标识和基线版本" in execution
    assert "生成任务标识、作用范围、处理结果、计数、评估用例标识、时间戳和生命周期状态" in eval_case_generation
    assert "评估运行标识、结果状态、门禁结果和受影响资产" in regression_impact


def test_feedback_prompts_spell_out_business_information_points():
    prompts = {
        "attribution": attribution_prompt("/tmp/attribution.json"),
        "optimization_plan": proposal_generator_prompt("/tmp/batch-plan.json"),
        "execution": execution_plan_prompt("/tmp/execution.json"),
        "eval_case_generation": eval_case_generation_prompt("/tmp/eval-case.json"),
        "regression_impact_analysis": regression_impact_analysis_prompt("/tmp/regression-impact.json"),
    }

    expected = {
        "attribution": (
            "problem_type",
            "optimization_object_type",
            "actionability",
            "confidence",
            "human_review_required",
            "evidence_refs",
            "responsibility_boundary",
            "rationale",
            "recommended_next_step",
        ),
        "optimization_plan": (
            "目标路径或外部对象",
            "证据引用",
            "任务标题",
            "验收标准",
            "task_context",
            "external_webhook",
            "blocked_items",
        ),
        "execution": (
            "operations[].operation",
            "expected_sha256",
            "content 或 append_text",
            "no_action_reason",
        ),
        "eval_case_generation": (
            "eval_cases",
            "prompt",
            "expected_behavior",
            "checks_json",
            "labels",
            "no_action_reason",
        ),
        "regression_impact_analysis": (
            "eval_run",
            "gate_result",
            "recommendations",
            "risk_assessment",
            "next_steps",
            "no_action_reason",
        ),
    }
    for prompt_name, required_texts in expected.items():
        prompt = prompts[prompt_name]
        for text in required_texts:
            assert text in prompt


def test_attribution_and_proposal_generator_prompts_require_chinese_user_facing_text():
    attribution = attribution_prompt("/tmp/attribution.json")
    proposal = proposal_generator_prompt("/tmp/batch-plan.json")

    assert "所有面向人的说明文本必须使用简体中文" in attribution
    assert "证据引用原因" in attribution
    assert "责任边界" in attribution
    assert "所有面向人的说明文本必须使用简体中文" in proposal
    assert "任务标题、任务描述、任务目标、任务建议动作" in proposal
    assert "阻断项原因和阻断项建议必须使用简体中文" in proposal
