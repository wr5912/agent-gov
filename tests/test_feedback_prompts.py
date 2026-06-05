from app.runtime.prompts.feedback_prompts import (
    attribution_prompt,
    eval_case_generation_prompt,
    execution_plan_prompt,
    proposal_generator_prompt,
    regression_impact_analysis_prompt,
)


def test_proposal_generator_prompt_embeds_context_when_available():
    prompt = proposal_generator_prompt(
        "/tmp/input.json",
        input_payload={
            "schema_version": "feedback-optimization-plan-input/v1",
            "job_id": "fbp-test",
            "batch_id": "fob-test",
            "regeneration_instruction": "优先修改 triage-alert skill。",
        },
    )

    assert "optimization_plan_input_json" in prompt
    assert "不需要调用工具读取文件" in prompt
    assert "regeneration_instruction" in prompt
    assert "不能覆盖中文输出、证据约束、target_policy 和可执行性要求" in prompt


def test_proposal_generator_prompt_delegates_wire_format_to_dspy():
    prompt = proposal_generator_prompt(
        "/tmp/batch-plan.json",
        input_payload={
            "schema_version": "feedback-optimization-plan-input/v1",
            "batch_id": "fob-test",
        },
    )

    assert "optimization_plan_input_json" in prompt
    assert "最终输出必须是一个 JSON 对象" not in prompt
    assert "不要输出 Markdown 方案、表格、代码围栏或解释性前后缀" not in prompt
    assert "系统会优先直接校验该 JSON" not in prompt
    assert "external_webhook" in prompt
    assert "blocked_items" in prompt


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
    assert "由后端从输入上下文注入，不需要复述" in proposal
    assert "由后端从输入上下文注入，不需要复述" in execution
    assert "由后端注入，不需要复述" in eval_case_generation
    assert "由后端从 eval_run 注入，不需要复述" in regression_impact


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
            "attribution_summaries",
            "evidence_refs",
            "tasks[].title",
            "acceptance_criteria",
            "task_context",
            "external_webhook",
            "blocked_items",
        ),
        "execution": (
            "optimization_task_id",
            "execution_job_id",
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
            "eval_run_id",
            "gate_result",
            "impacted_assets",
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
    assert "tasks[].title/description/objective/recommendation" in proposal
    assert "blocked_items[].title/reason/recommendation" in proposal
