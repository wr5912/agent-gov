import json

import pytest
from app.runtime.agent_job_types import AgentJobType
from app.runtime.feedback_schemas import (
    AttributionFormatterOutput,
    ExecutionPlanFormatterOutput,
    RegressionTestDesignFormatterOutput,
    validate_execution_plan_output,
)
from app.runtime.normalizers.feedback_output_normalizers import (
    normalize_attribution_output,
    normalize_execution_plan_output,
)
from app.runtime.normalizers.feedback_output_records import NormalizedExecutionPlanOutput
from app.runtime_gateway.execution import _parse_structured_output, _with_json_contract
from app.runtime_gateway.store import RuntimeStateConflict
from pydantic import ValidationError


def test_agentscope_governor_prompt_contains_exact_json_contract() -> None:
    prompt = _with_json_contract("请生成方案", AgentJobType.OPTIMIZATION_PLAN)

    assert prompt.startswith("请生成方案\n\n## 输出契约")
    assert "ImprovementOptimizationPlanFormatterOutput" in prompt
    assert "必须且只能是一个 UTF-8 JSON object" in prompt


def test_agentscope_governor_output_is_validated_as_current_model() -> None:
    output = _parse_structured_output(
        AgentJobType.OPTIMIZATION_PLAN,
        json.dumps(
            {
                "summary": "收紧时间窗口核验",
                "changes": [{"target": "AGENT.md", "change": "新增时间一致性检查。"}],
                "risk_level": "medium",
            },
            ensure_ascii=False,
        ),
    )

    assert output.summary == "收紧时间窗口核验"
    assert output.changes[0].target == "AGENT.md"
    assert not hasattr(output, "batch_id")


@pytest.mark.parametrize(
    "raw",
    ["```json\n{}\n```", "说明：{}", "[]", "{not-json}"],
)
def test_agentscope_governor_output_rejects_non_bare_or_invalid_json(raw: str) -> None:
    with pytest.raises(RuntimeStateConflict):
        _parse_structured_output(AgentJobType.NORMALIZED_FEEDBACK, raw)


def test_agentscope_governor_output_rejects_schema_violation() -> None:
    with pytest.raises(RuntimeStateConflict, match="violates"):
        _parse_structured_output(AgentJobType.NORMALIZED_FEEDBACK, '{"title":"缺少 problem"}')


def test_formatter_models_run_current_normalizers_before_strict_validation() -> None:
    attribution = AttributionFormatterOutput.model_validate(
        {
            "problem_type": "tool_usage_gap",
            "optimization_object_type": "agent",
            "actionability": "manual_review",
            "confidence": "high",
            "human_review_required": True,
            "responsibility_boundary": "sec-ops-data",
            "rationale": "反馈显示工具数据不完整。",
            "recommended_next_step": "review",
        },
    )
    execution = ExecutionPlanFormatterOutput.model_validate(
        {
            "status": "blocked",
            "summary": "缺少可安全修改的目标文件。",
            "operations": [],
            "no_action_reason": "target_paths 为空。",
        },
    )
    regression = RegressionTestDesignFormatterOutput.model_validate(
        {"no_action_reason": "证据不足，无法形成回归测试设计。"},
    )

    assert attribution.problem_type == "tool_data_quality"
    assert attribution.optimization_object_type == "business_agent_agent_md"
    assert attribution.actionability == "needs_human_analysis"
    assert attribution.recommended_next_step == "needs_human_review"
    assert attribution.responsibility_boundary.owner == "sec-ops-data"
    assert execution.status == "needs_human_review"
    assert regression.no_action_reason == "证据不足，无法形成回归测试设计。"


def test_normalize_attribution_output_uses_agentscope_harness_name() -> None:
    normalized = normalize_attribution_output(
        {
            "problem_type": "tool_usage_gap",
            "optimization_object_type": "agent",
            "actionability": "manual_review",
            "recommended_next_step": "review",
            "evidence_refs": ["evidence/a.json"],
            "responsibility_boundary": "sec-ops-data",
        },
    )

    assert normalized["optimization_object_type"] == "business_agent_agent_md"
    assert normalized["evidence_refs"][0]["id"] == "evidence/a.json"


def test_attribution_formatter_drops_backend_owned_fields() -> None:
    model = AttributionFormatterOutput.model_validate(
        {
            "problem_type": "reasoning_error",
            "optimization_object_type": "business_agent_agent_md",
            "actionability": "workspace_config_change",
            "confidence": "medium",
            "human_review_required": False,
            "responsibility_boundary": {"owner": "internal", "reason": "推断错误。"},
            "rationale": "推理问题。",
            "recommended_next_step": "generate_proposal",
            "feedback_case_id": "fc-evil",
            "attribution_job_id": "aj-evil",
        },
    )

    dumped = model.model_dump()
    assert "feedback_case_id" not in dumped
    assert "attribution_job_id" not in dumped


def test_normalize_execution_plan_output_keeps_only_owned_fields() -> None:
    normalized = normalize_execution_plan_output(
        {
            "status": "safe_to_apply",
            "patches": [
                {
                    "op": "append",
                    "path": "AGENT.md",
                    "content": "\n补充说明。",
                    "rationale": {"reason": "根据反馈补充。"},
                    "agent_note": {"source": "execution-optimizer"},
                },
            ],
        },
    )

    assert normalized["status"] == "ready"
    assert normalized["operations"][0]["operation"] == "append_text"
    assert normalized["operations"][0]["path"] == "AGENT.md"
    assert "agent_note" not in normalized["operations"][0]


def test_validated_execution_output_drops_nested_extra_fields() -> None:
    execution, error = validate_execution_plan_output(
        {
            "status": "ready",
            "summary": "执行补丁",
            "operations": [
                {
                    "operation": "append_text",
                    "path": "AGENT.md",
                    "append_text": "\n补充说明。",
                    "agent_note": {"source": "execution-optimizer"},
                },
            ],
        },
    )

    assert error is None
    assert "agent_note" not in execution["operations"][0]


def test_regression_design_requires_test_or_no_action_reason() -> None:
    with pytest.raises(ValidationError, match="tests or no_action_reason"):
        RegressionTestDesignFormatterOutput.model_validate({})


def test_normalized_record_drops_extra_agent_fields() -> None:
    record = NormalizedExecutionPlanOutput.model_validate(
        {"status": "ready", "operations": [], "agent_notes": {"source": "optimizer"}},
    )

    assert "agent_notes" not in record.to_payload()
