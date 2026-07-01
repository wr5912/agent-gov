from contextlib import nullcontext

import app.runtime.output_formatter as output_formatter_module
import pytest
from app.runtime.agent_job_types import AgentJobType
from app.runtime.feedback_schemas import (
    AttributionFormatterOutput,
    ExecutionPlanFormatterOutput,
    FeedbackEvalCaseGenerationFormatterOutput,
    ImprovementOptimizationPlanFormatterOutput,
    validate_execution_plan_output,
    validate_feedback_eval_case_generation_output,
)
from app.runtime.normalizers.feedback_output_normalizers import (
    normalize_attribution_output,
    normalize_execution_plan_output,
    normalize_feedback_eval_case_generation_output,
)
from app.runtime.normalizers.feedback_output_records import NormalizedExecutionPlanOutput
from app.runtime.output_formatter import DSPyOutputFormatter, OutputFormatterError

from feedback_store_test_utils import _settings


def test_dspy_output_formatter_uses_current_optimization_plan_model(tmp_path, monkeypatch):
    formatter = DSPyOutputFormatter(_settings(tmp_path))
    payload = {
        "summary": "收紧时间窗口核验",
        "changes": [{"target": "CLAUDE.md", "change": "新增 OCSF/STIX 时间一致性检查。"}],
        "risk_level": "medium",
    }
    seen: dict[str, object] = {}

    def fake_formatter(**kwargs: object):
        seen.update(kwargs)
        return ImprovementOptimizationPlanFormatterOutput.model_validate(payload)

    monkeypatch.setattr(formatter, "_format_with_dspy", fake_formatter)

    result = formatter.format(
        job_type=AgentJobType.OPTIMIZATION_PLAN,
        raw_text="治理 Agent 输出事项级优化方案。",
        job_input={"improvement_id": "imp-1"},
    )

    assert seen["job_type"] == AgentJobType.OPTIMIZATION_PLAN
    assert seen["output_model"] is ImprovementOptimizationPlanFormatterOutput
    assert result.output.summary == "收紧时间窗口核验"
    assert not hasattr(result.output, "batch_id")


def test_dspy_output_formatter_predictor_receives_only_raw_agent_output(tmp_path, monkeypatch):
    formatter = DSPyOutputFormatter(_settings(tmp_path))
    seen: dict[str, object] = {}

    class FakePredict:
        def __init__(self, signature: object) -> None:
            seen["signature"] = signature

        def __call__(self, **kwargs: object):
            seen["predictor_kwargs"] = kwargs
            return type(
                "Prediction",
                (),
                {
                    "formatted_output": {
                        "summary": "事项级优化方案",
                        "changes": [{"target": "CLAUDE.md", "change": "补充规则。"}],
                    }
                },
            )()

    monkeypatch.setattr(formatter, "_instrument_dspy", lambda: None)
    monkeypatch.setattr(formatter, "_lm_instance", lambda: object())
    monkeypatch.setattr(output_formatter_module, "_dspy_lm_context", lambda _lm: nullcontext())
    monkeypatch.setattr(output_formatter_module.dspy, "Predict", FakePredict)

    output = formatter._format_with_dspy(
        job_type=AgentJobType.OPTIMIZATION_PLAN,
        raw_text="## 优化方案\n- 补充规则",
        output_model=ImprovementOptimizationPlanFormatterOutput,
    )

    assert isinstance(output, ImprovementOptimizationPlanFormatterOutput)
    assert seen["predictor_kwargs"] == {"raw_agent_output": "## 优化方案\n- 补充规则"}


def test_dspy_output_formatter_lm_uses_configured_max_tokens(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    settings.dspy_output_formatter_max_tokens = 12_345
    formatter = DSPyOutputFormatter(settings)
    seen: dict[str, object] = {}

    def fake_lm(**kwargs: object):
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(output_formatter_module.dspy, "LM", fake_lm)

    formatter._lm_instance()

    assert seen["max_tokens"] == 12_345


def test_dspy_output_formatter_timeout_default_matches_agent_job_timeout(tmp_path):
    settings = _settings(tmp_path)

    assert settings.dspy_output_formatter_timeout_seconds == settings.governance_agent_timeout_seconds == 300


def test_dspy_output_formatter_retries_transient_predictor_failure(tmp_path, monkeypatch):
    formatter = DSPyOutputFormatter(_settings(tmp_path))
    calls: list[dict[str, object]] = []

    class FakePredict:
        def __init__(self, _signature: object) -> None:
            pass

        def __call__(self, **kwargs: object):
            calls.append(kwargs)
            if len(calls) == 1:
                raise RuntimeError("transient formatter failure")
            return type("Prediction", (), {"formatted_output": {"summary": "归因摘要", "changes": []}})()

    monkeypatch.setattr(formatter, "_instrument_dspy", lambda: None)
    monkeypatch.setattr(formatter, "_lm_instance", lambda: object())
    monkeypatch.setattr(output_formatter_module, "_dspy_lm_context", lambda _lm: nullcontext())
    monkeypatch.setattr(output_formatter_module.dspy, "Predict", FakePredict)

    output = formatter._format_with_dspy(
        job_type=AgentJobType.OPTIMIZATION_PLAN,
        raw_text="## 优化方案",
        output_model=ImprovementOptimizationPlanFormatterOutput,
    )

    assert isinstance(output, ImprovementOptimizationPlanFormatterOutput)
    assert calls == [{"raw_agent_output": "## 优化方案"}, {"raw_agent_output": "## 优化方案"}]


def test_dspy_output_formatter_zero_retries_attempts_once_and_preserves_diagnostics(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    settings.dspy_output_formatter_max_retries = 0
    formatter = DSPyOutputFormatter(settings)
    calls: list[dict[str, object]] = []

    class FakePredict:
        def __init__(self, _signature: object) -> None:
            pass

        def __call__(self, **kwargs: object):
            calls.append(kwargs)
            raise RuntimeError("permanent formatter failure")

    monkeypatch.setattr(formatter, "_instrument_dspy", lambda: None)
    monkeypatch.setattr(formatter, "_lm_instance", lambda: object())
    monkeypatch.setattr(output_formatter_module, "_dspy_lm_context", lambda _lm: nullcontext())
    monkeypatch.setattr(output_formatter_module.dspy, "Predict", FakePredict)

    with pytest.raises(OutputFormatterError) as exc_info:
        formatter.format(
            job_type=AgentJobType.OPTIMIZATION_PLAN,
            raw_text="优化方案生成智能体输出了一段自然语言方案。",
            job_input={"improvement_id": "imp-1"},
        )

    assert calls == [{"raw_agent_output": "优化方案生成智能体输出了一段自然语言方案。"}]
    assert exc_info.value.raw_output_json["_formatter"]["status"] == "failed"
    assert exc_info.value.raw_output_json["_formatter"]["error_type"] == "RuntimeError"


def test_formatter_models_run_current_normalizers_before_strict_validation():
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
        }
    )
    execution = ExecutionPlanFormatterOutput.model_validate(
        {
            "status": "blocked",
            "summary": "缺少可安全修改的目标文件。",
            "operations": [],
            "no_action_reason": "target_paths 为空。",
        }
    )
    eval_generation = FeedbackEvalCaseGenerationFormatterOutput.model_validate({})

    assert attribution.problem_type == "tool_data_quality"
    assert attribution.optimization_object_type == "main_agent_claude_md"
    assert attribution.actionability == "needs_human_analysis"
    assert attribution.recommended_next_step == "needs_human_review"
    assert attribution.responsibility_boundary.owner == "sec-ops-data"
    assert execution.status == "needs_human_review"
    assert eval_generation.no_action_reason == "eval-case-governor 未生成可用评估用例。"


def test_normalize_attribution_output_uses_intermediate_record_for_agent_shapes():
    normalized = normalize_attribution_output(
        {
            "problem_type": "tool_usage_gap",
            "optimization_object_type": "agent",
            "actionability": "manual_review",
            "recommended_next_step": "review",
            "evidence_refs": ["evidence/a.json"],
            "responsibility_boundary": "sec-ops-data",
        }
    )

    assert normalized["problem_type"] == "tool_data_quality"
    assert normalized["optimization_object_type"] == "main_agent_claude_md"
    assert normalized["actionability"] == "needs_human_analysis"
    assert normalized["recommended_next_step"] == "needs_human_review"
    assert normalized["evidence_refs"][0]["id"] == "evidence/a.json"
    assert normalized["responsibility_boundary"]["owner"] == "sec-ops-data"


def test_attribution_formatter_output_drops_backend_owned_fields():
    model = AttributionFormatterOutput.model_validate(
        {
            "problem_type": "reasoning_error",
            "optimization_object_type": "main_agent_claude_md",
            "actionability": "workspace_config_change",
            "confidence": "medium",
            "human_review_required": False,
            "responsibility_boundary": {"owner": "internal", "reason": "推断错误。"},
            "rationale": "推理问题。",
            "recommended_next_step": "generate_proposal",
            "feedback_case_id": "fc-evil",
            "attribution_job_id": "aj-evil",
        }
    )
    dumped = model.model_dump()
    assert "feedback_case_id" not in dumped
    assert "attribution_job_id" not in dumped


def test_normalize_execution_plan_output_uses_intermediate_operation_records_and_drops_extra_fields():
    normalized = normalize_execution_plan_output(
        {
            "status": "safe_to_apply",
            "patches": [
                "not-an-object",
                {
                    "op": "append",
                    "path": "CLAUDE.md",
                    "content": "\n补充说明。",
                    "rationale": {"reason": "根据反馈补充。"},
                    "agent_note": {"source": "execution-optimizer"},
                },
            ],
        }
    )

    assert normalized["status"] == "ready"
    assert len(normalized["operations"]) == 1
    assert normalized["operations"][0]["operation"] == "append_text"
    assert normalized["operations"][0]["append_text"] == "\n补充说明。"
    assert normalized["operations"][0]["rationale"].startswith("{")
    assert "agent_note" not in normalized["operations"][0]


def test_normalize_feedback_eval_case_generation_output_uses_intermediate_case_records():
    normalized = normalize_feedback_eval_case_generation_output(
        {
            "eval_cases": [
                "not-an-object",
                {
                    "title": "复现工具数据缺失",
                    "status": "approved",
                    "expected_behavior": {"text": "应说明数据缺失并请求补充。"},
                    "labels": "tool-data",
                    "checks_json": ["not", "object"],
                    "agent_note": {"source": "eval-case-governor"},
                },
            ],
        }
    )

    assert normalized["status"] == "completed"
    assert len(normalized["eval_cases"]) == 1
    case = normalized["eval_cases"][0]
    assert case["asset_layer"] == "candidate"
    assert case["prompt"] == "复现工具数据缺失"
    assert case["expected_behavior"].startswith("{")
    assert case["labels"] == ["tool-data"]
    assert case["checks_json"] == {}
    assert "agent_note" not in case


def test_validated_execution_and_eval_outputs_drop_agent_extra_fields():
    execution, execution_error = validate_execution_plan_output(
        {
            "status": "ready",
            "summary": "执行补丁",
            "operations": [
                {
                    "operation": "append_text",
                    "path": "CLAUDE.md",
                    "append_text": "\n补充说明。",
                    "agent_note": {"source": "execution-optimizer"},
                }
            ],
        }
    )
    eval_cases, eval_error = validate_feedback_eval_case_generation_output(
        {
            "status": "completed",
            "eval_cases": [
                {
                    "prompt": "复现问题",
                    "expected_behavior": "应说明缺失数据。",
                    "agent_note": {"source": "eval-case-governor"},
                }
            ],
        }
    )

    assert execution_error is None
    assert eval_error is None
    assert "agent_note" not in execution["operations"][0]
    assert "agent_note" not in eval_cases["eval_cases"][0]


def test_normalized_output_record_drops_extra_agent_fields():
    record = NormalizedExecutionPlanOutput.model_validate(
        {
            "status": "ready",
            "operations": [],
            "agent_notes": {"source": "execution-optimizer"},
        }
    )

    assert "agent_notes" not in record.to_payload()
