from contextlib import nullcontext

import app.runtime.output_formatter as output_formatter_module
import pytest
from app.runtime.agent_job_types import AgentJobType
from app.runtime.feedback_schemas import (
    AttributionFormatterOutput,
    ExecutionPlanFormatterOutput,
    FeedbackEvalCaseGenerationFormatterOutput,
    FeedbackOptimizationPlanFormatterOutput,
    RegressionImpactAnalysisFormatterOutput,
    validate_execution_plan_output,
    validate_feedback_eval_case_generation_output,
    validate_feedback_optimization_plan_output,
    validate_regression_impact_analysis_output,
)
from app.runtime.normalizers.feedback_output_normalizers import (
    normalize_attribution_output,
    normalize_execution_plan_output,
    normalize_feedback_eval_case_generation_output,
    normalize_feedback_optimization_plan_output,
    normalize_regression_impact_analysis_output,
)
from app.runtime.normalizers.feedback_output_records import NormalizedExecutionPlanOutput
from app.runtime.normalizers.feedback_output_task_context import (
    external_context_target,
    infer_external_task_context,
    normalize_task_context_payload,
    task_context_has_external_specificity,
)
from app.runtime.output_formatter import DSPyOutputFormatter, OutputFormatterError

from feedback_store_test_utils import _batch_plan_output, _settings


def test_dspy_output_formatter_uses_dspy_with_raw_agent_output_only(tmp_path, monkeypatch):
    formatter = DSPyOutputFormatter(_settings(tmp_path))
    payload = _batch_plan_output({"input_json": {"batch_id": "fob-test"}})
    seen: dict[str, object] = {}

    def fake_formatter(**kwargs: object):
        seen.update(kwargs)
        return FeedbackOptimizationPlanFormatterOutput.model_validate(payload)

    monkeypatch.setattr(formatter, "_format_with_dspy", fake_formatter)

    result = formatter.format(
        job_type="batch_plan",
        raw_text=f"前缀\n{payload}",
        job_input={"batch_id": "fob-test"},
    )

    assert seen["job_type"] == "batch_plan"
    assert seen["output_model"] is FeedbackOptimizationPlanFormatterOutput
    assert "前缀" in str(seen["raw_text"])
    assert "job_input" not in seen
    assert "job_input_json" not in seen
    assert isinstance(result.output, FeedbackOptimizationPlanFormatterOutput)
    assert not hasattr(result.output, "batch_id")
    assert "_formatter" not in result.output.model_dump(mode="json")


def test_dspy_output_formatter_predictor_receives_no_job_input_context(tmp_path, monkeypatch):
    formatter = DSPyOutputFormatter(_settings(tmp_path))
    payload = _batch_plan_output({"input_json": {"batch_id": "fob-test"}})
    seen: dict[str, object] = {}

    class FakePredict:
        def __init__(self, signature: object) -> None:
            seen["signature"] = signature

        def __call__(self, **kwargs: object):
            seen["predictor_kwargs"] = kwargs
            return type("Prediction", (), {"formatted_output": payload})()

    monkeypatch.setattr(formatter, "_instrument_dspy", lambda: None)
    monkeypatch.setattr(formatter, "_lm_instance", lambda: object())
    monkeypatch.setattr(output_formatter_module, "_dspy_lm_context", lambda _lm: nullcontext())
    monkeypatch.setattr(output_formatter_module.dspy, "Predict", FakePredict)

    output = formatter._format_with_dspy(
        job_type=AgentJobType.BATCH_PLAN,
        raw_text="## 方案概览\n- 标题：测试批次优化方案\n- 阻断项：测试场景不生成可执行任务。",
        output_model=FeedbackOptimizationPlanFormatterOutput,
    )

    assert isinstance(output, FeedbackOptimizationPlanFormatterOutput)
    assert seen["predictor_kwargs"] == {"raw_agent_output": "## 方案概览\n- 标题：测试批次优化方案\n- 阻断项：测试场景不生成可执行任务。"}


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

    assert settings.dspy_output_formatter_timeout_seconds == 300


def test_dspy_output_formatter_retries_transient_predictor_failure(tmp_path, monkeypatch):
    formatter = DSPyOutputFormatter(_settings(tmp_path))
    payload = _batch_plan_output({"input_json": {"batch_id": "fob-test"}})
    calls: list[dict[str, object]] = []

    class FakePredict:
        def __init__(self, _signature: object) -> None:
            pass

        def __call__(self, **kwargs: object):
            calls.append(kwargs)
            if len(calls) == 1:
                raise RuntimeError("transient formatter failure")
            return type("Prediction", (), {"formatted_output": payload})()

    monkeypatch.setattr(formatter, "_instrument_dspy", lambda: None)
    monkeypatch.setattr(formatter, "_lm_instance", lambda: object())
    monkeypatch.setattr(output_formatter_module, "_dspy_lm_context", lambda _lm: nullcontext())
    monkeypatch.setattr(output_formatter_module.dspy, "Predict", FakePredict)

    output = formatter._format_with_dspy(
        job_type=AgentJobType.BATCH_PLAN,
        raw_text="## 方案概览\n- 标题：测试批次优化方案",
        output_model=FeedbackOptimizationPlanFormatterOutput,
    )

    assert isinstance(output, FeedbackOptimizationPlanFormatterOutput)
    assert calls == [
        {"raw_agent_output": "## 方案概览\n- 标题：测试批次优化方案"},
        {"raw_agent_output": "## 方案概览\n- 标题：测试批次优化方案"},
    ]


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
            job_type="batch_plan",
            raw_text="## 方案概览\n- 标题：测试批次优化方案",
            job_input={"batch_id": "fob-test"},
        )

    assert calls == [{"raw_agent_output": "## 方案概览\n- 标题：测试批次优化方案"}]
    assert exc_info.value.raw_output_json["_formatter"]["status"] == "failed"
    assert exc_info.value.raw_output_json["_formatter"]["error_type"] == "RuntimeError"
    assert "permanent formatter failure" in exc_info.value.raw_output_json["_formatter"]["error_message"]


def test_dspy_output_formatter_error_preserves_raw_output_diagnostics(tmp_path, monkeypatch):
    formatter = DSPyOutputFormatter(_settings(tmp_path))

    def fail_formatter(**_: object):
        raise RuntimeError("adapter returned reasoning without formatted_output")

    monkeypatch.setattr(formatter, "_format_with_dspy", fail_formatter)

    with pytest.raises(OutputFormatterError) as exc_info:
        formatter.format(
            job_type="batch_plan",
            raw_text="优化方案生成智能体输出了一段自然语言方案。",
            job_input={"batch_id": "fob-test"},
        )

    raw_output = exc_info.value.raw_output_json
    assert raw_output["_formatter"]["status"] == "failed"
    assert "自然语言方案" in raw_output["raw_text"]


def test_formatter_models_run_normalizers_before_strict_validation():
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
    regression = RegressionImpactAnalysisFormatterOutput.model_validate({"recommendations": "重新运行受影响回归集。"})

    assert attribution.problem_type == "tool_data_quality"
    assert attribution.optimization_object_type == "main_agent_claude_md"
    assert attribution.actionability == "needs_human_analysis"
    assert attribution.recommended_next_step == "needs_human_review"
    assert attribution.responsibility_boundary.owner == "sec-ops-data"
    assert execution.status == "needs_human_review"
    assert eval_generation.no_action_reason == "eval-case-governor 未生成可用评估用例。"
    assert regression.recommendations == ["重新运行受影响回归集。"]


def test_batch_plan_formatter_converts_incomplete_task_to_blocked_item():
    output = FeedbackOptimizationPlanFormatterOutput.model_validate(
        {
            "status": "pending_execution",
            "title": "统筹优化 sec-ops 数据源问题",
            "tasks": [
                {
                    "title": "修复 sec-ops 数据源 2026 年数据缺失",
                    "description": "在运行时环境中补充数据源覆盖说明。",
                }
            ],
        }
    )

    assert output.status == "needs_human_review"
    assert output.tasks == []
    assert output.blocked_items[0].title == "修复 sec-ops 数据源 2026 年数据缺失"
    assert "Agent 输出的优化任务缺少可执行字段" in output.blocked_items[0].reason
    assert "execution_kind" in output.blocked_items[0].reason
    assert "objective" in output.blocked_items[0].reason
    assert output.blocked_items[0].recommendation == "在运行时环境中补充数据源覆盖说明。"


def test_batch_plan_formatter_infers_target_type_from_target_path():
    output = FeedbackOptimizationPlanFormatterOutput.model_validate(
        {
            "status": "pending_execution",
            "title": "统筹优化 MCP 配置和回归资产",
            "target_path": ".mcp.json",
            "recommendation": "修复 MCP 配置并补充回归验证。",
            "expected_effect": "降低同类配置问题复现概率。",
            "validation": "运行关联回归用例。",
            "risk": "默认地址可能不适用于生产环境。",
            "tasks": [
                {
                    "execution_kind": "workspace_execution",
                    "actionability": "workspace_config_change",
                    "target_path": ".mcp.json",
                    "title": "修复 MCP 配置",
                    "description": "为 sec-ops-data MCP 地址补充本地默认值。",
                    "objective": "避免变量缺失导致 MCP 服务器连接失败。",
                    "recommendation": "在 .mcp.json 中补充默认地址。",
                    "expected_effect": "MCP 工具能正常初始化。",
                    "validation": "启动会话检查 MCP 状态。",
                    "risk": "默认地址仅适合本地环境。",
                },
                {
                    "actionability": "eval_only",
                    "target_path": ".planning/eval/sec-ops-data.json",
                    "title": "补充 MCP 配置回归用例",
                    "description": "注册回归用例覆盖 MCP 地址缺失场景。",
                    "objective": "防止配置问题回归。",
                    "recommendation": "新增配置回归验证。",
                    "expected_effect": "回归流水线能发现同类问题。",
                    "validation": "运行新增回归用例。",
                    "risk": "用例可能需要人工校准断言。",
                },
            ],
        }
    )

    assert output.target_type == "mcp_config"
    assert output.tasks[0].target_type == "mcp_config"
    assert output.tasks[1].execution_kind == "workspace_execution"
    assert output.tasks[1].target_type == "eval_case"
    assert output.blocked_items == []


def test_batch_plan_formatter_promotes_eval_case_task_to_internal_action():
    output = FeedbackOptimizationPlanFormatterOutput.model_validate(
        {
            "status": "pending_execution",
            "title": "反馈优化批次方案",
            "tasks": [
                {
                    "title": "将评估用例提升为活跃回归用例",
                    "description": "把本批次候选评估用例纳入长期回归资产。",
                    "objective": "让同类反馈修复后进入稳定回归验证。",
                    "recommendation": "晋级关联评估用例。",
                    "expected_effect": "后续版本回归可覆盖该反馈场景。",
                    "validation": "检查用例状态和审计记录。",
                    "risk": "用例断言过宽时可能降低回归信号质量。",
                    "eval_case_ids": ["evc-1"],
                }
            ],
        }
    )

    assert output.blocked_items == []
    task = output.tasks[0]
    assert task.execution_kind == "internal_action"
    assert task.internal_action == "promote_eval_cases"
    assert task.target_type == "eval_case"
    assert task.actionability == "regression_asset_governance"
    assert task.eval_case_ids == ["evc-1"]


def test_normalize_task_context_payload_coerces_lists_and_drops_empty_values():
    context = normalize_task_context_payload(
        {
            "mcp_server": "sec-ops-data",
            "tool_names": "list_alerts_api_v1_alerts_get",
            "query_ids": [" alert-123 ", "", None],
            "observed_issue": "",
            "extra_filter": {"severity": "high"},
            "empty": "",
        }
    )

    assert context["mcp_server"] == "sec-ops-data"
    assert context["tool_names"] == ["list_alerts_api_v1_alerts_get"]
    assert context["query_ids"] == ["alert-123"]
    assert "extra_filter" not in context
    assert "observed_issue" not in context
    assert "empty" not in context
    assert task_context_has_external_specificity(context)


def test_infer_external_task_context_derives_external_api_details_from_text():
    context = infer_external_task_context(
        {
            "title": "确认并上报漏洞数据源 2026 年数据缺失问题",
            "owner": "sec-ops-data",
            "reason": "查询 alert-123 时无法获得 2026 年 CVE-2026-1234 数据。",
            "recommendation": "请核查 list_vulnerabilities_api_v1_vulnerabilities_get 的数据源覆盖范围。",
        }
    )

    assert context["mcp_server"] == "sec-ops-data"
    assert context["external_system"] == "sec-ops-data"
    assert context["tool_name"] == "list_vulnerabilities_api_v1_vulnerabilities_get"
    assert context["api_name"] == "list_vulnerabilities"
    assert context["api_path"] == "/api/v1/vulnerabilities"
    assert context["api_method"] == "GET"
    assert context["endpoint"] == "GET /api/v1/vulnerabilities"
    assert context["query_ids"] == ["alert-123", "CVE-2026-1234"]
    assert "2026" in context["dates"]
    assert "year" in context["affected_fields"]
    assert "cve_coverage" in context["affected_fields"]
    assert "2026" in context["observed_issue"]
    assert external_context_target(context) == "GET /api/v1/vulnerabilities"
    assert task_context_has_external_specificity(context)


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
    assert normalized["evidence_refs"] == [
        {
            "type": "evidence_file",
            "id": "evidence/a.json",
            "reason": "归因分析智能体引用了该证据文件。",
        }
    ]
    assert normalized["responsibility_boundary"] == {
        "owner": "sec-ops-data",
        "reason": "归因分析智能体输出了责任边界标签，系统归一化为结构化对象。",
    }


def test_attribution_distinguishes_reasoning_error_from_data_and_tool_problems():
    """AGV-032：归因可把"推理问题"独立于数据缺口/工具问题/执行资产问题分类。

    reasoning_error 是 ProblemType 的独立类目（数据与工具充分但推断本身有误），
    常见推理类原始措辞经 normalizer 归一到 reasoning_error，并能通过严格契约校验。
    """
    for raw_problem_type in ("inference_error", "flawed_reasoning", "logic_error", "reasoning_gap"):
        normalized = normalize_attribution_output({"problem_type": raw_problem_type})
        assert normalized["problem_type"] == "reasoning_error"

    # reasoning_error 通过强类型契约校验，且与数据缺口/工具问题是并列的不同类目。
    model = AttributionFormatterOutput.model_validate(
        {
            "status": "completed",
            "problem_type": "reasoning_error",
            "optimization_object_type": "main_agent_claude_md",
            "actionability": "workspace_config_change",
            "confidence": "high",
            "human_review_required": False,
            "evidence_refs": [{"type": "run_log", "id": "run-1", "reason": "数据与工具完整。"}],
            "responsibility_boundary": {"owner": "internal", "reason": "结论与证据不一致，属推断错误。"},
            "rationale": "证据齐全、工具可用，但 Agent 把高危误判为低危，属推理问题而非数据缺口。",
            "recommended_next_step": "generate_proposal",
        }
    )
    assert model.problem_type == "reasoning_error"
    assert model.problem_type not in {"evidence_gap", "tool_misuse", "instruction_gap"}


def test_attribution_formatter_output_drops_backend_owned_fields_on_reasoning_error():
    """测试纵深：AI 输出契约变更后，backend-owned 字段污染仍被拒绝回填（字段所有权边界）。"""
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
            # hostile：恶意注入 backend-owned 上下文字段，不应进入 agent-owned 输出。
            "feedback_case_id": "fc-evil",
            "attribution_job_id": "aj-evil",
        }
    )
    dumped = model.model_dump()
    assert "feedback_case_id" not in dumped
    assert "attribution_job_id" not in dumped
    assert model.problem_type == "reasoning_error"


def test_normalize_feedback_plan_output_records_blocked_workspace_task_reason():
    normalized = normalize_feedback_optimization_plan_output(
        {
            "batch_id": "fob-test",
            "status": "ready",
            "confidence": "certain",
            "actionability": "workspace_change",
            "tasks": [
                {
                    "execution_kind": "workspace_execution",
                    "title": {"text": "补充工具说明"},
                    "target_type": "mcp_description",
                    "target_path": "",
                    "actionability": "workspace_change",
                    "recommendation": ["补充年份筛选说明"],
                    "expected_effect": "减少同类反馈。",
                    "validation": "回归通过。",
                    "risk": "底层数据仍可能缺失。",
                }
            ],
        }
    )

    assert normalized["status"] == "pending_execution"
    assert normalized["confidence"] == "medium"
    assert normalized["actionability"] == "direct_workspace_change"
    assert normalized["tasks"] == []
    assert normalized["blocked_items"][0]["reason"] == "任务缺少 target_path，不能交给 execution-optimizer 执行。"
    assert normalized["blocked_items"][0]["title"].startswith("{")


def test_normalize_feedback_plan_output_blocks_invalid_internal_action_task():
    normalized = normalize_feedback_optimization_plan_output(
        {
            "batch_id": "fob-test",
            "status": "pending_execution",
            "tasks": [
                {
                    "execution_kind": "internal_action",
                    "internal_action": "promote_eval_cases",
                    "title": "晋级回归资产",
                    "description": "缺少明确 eval_case_ids。",
                    "objective": "纳入长期回归资产。",
                    "target_type": "eval_case",
                    "actionability": "regression_asset_governance",
                    "recommendation": "执行内部晋级动作。",
                    "expected_effect": "回归计划可复用该资产。",
                    "validation": "检查评估用例状态。",
                    "risk": "误晋级会污染回归集。",
                }
            ],
        }
    )

    assert normalized["tasks"] == []
    assert normalized["status"] == "pending_execution"
    assert normalized["blocked_items"][0]["reason"] == "内部回归资产治理任务缺少 eval_case_ids 或受支持的 internal_action，不能自动执行。"


def test_normalize_feedback_plan_output_sanitizes_attribution_summary_extras():
    normalized = normalize_feedback_optimization_plan_output(
        {
            "batch_id": "fob-test",
            "attribution_summaries": [
                {
                    "job_id": "fba-1",
                    "owner": "mcp_config",
                    "feedback_case_id": "fc-1",
                    "problem_type": "tool_data_quality",
                    "optimization_object_type": "mcp_description",
                    "actionability": "external_guidance",
                    "confidence": "high",
                    "rationale": "外部 MCP 返回数据不完整。",
                    "summary": "归因指向外部 MCP 数据源。",
                }
            ],
            "blocked_items": [{"reason": "外部系统需要人工处理。"}],
        }
    )

    assert normalized["attribution_summaries"] == [
        {
            "attribution_job_id": "fba-1",
            "feedback_case_id": "fc-1",
            "problem_type": "tool_data_quality",
            "optimization_object_type": "mcp_description",
            "actionability": "external_guidance",
            "confidence": "high",
            "rationale": "外部 MCP 返回数据不完整。",
            "summary": "归因指向外部 MCP 数据源。",
        }
    ]


def test_normalize_feedback_plan_output_uses_intermediate_task_records_and_drops_extra_fields():
    normalized = normalize_feedback_optimization_plan_output(
        {
            "batch_id": "fob-test",
            "status": "ready",
            "confidence": "high",
            "actionability": "workspace_change",
            "evidence_refs": ["evidence/plan.json"],
            "tasks": [
                {
                    "execution_kind": "workspace_execution",
                    "title": "补充主智能体工具使用约束",
                    "target_type": "main_agent_claude_md",
                    "target_path": "CLAUDE.md",
                    "actionability": "workspace_change",
                    "recommendation": "补充读取配置前必须核查 workspace 的约束。",
                    "expected_effect": "减少同类反馈。",
                    "validation": "回归通过。",
                    "risk": "可能增加一次文件读取。",
                    "task_context": {
                        "target_file": "CLAUDE.md",
                        "extra_filter": {"section": "tools"},
                    },
                    "evidence_refs": ["evidence/task.json"],
                    "agent_note": {"source": "optimization-planner"},
                }
            ],
            "blocked_items": [
                {
                    "title": "缺少外部系统归属",
                    "target_type": "not_actionable",
                    "recommendation": "等待人工确认外部责任方。",
                    "evidence_refs": ["evidence/blocked.json"],
                    "agent_note": {"source": "optimization-planner"},
                }
            ],
        }
    )

    assert normalized["evidence_refs"] == [
        {
            "type": "evidence_file",
            "id": "evidence/plan.json",
            "reason": "优化方案生成智能体引用了该证据。",
        }
    ]
    assert len(normalized["tasks"]) == 1
    task = normalized["tasks"][0]
    assert task["execution_kind"] == "workspace_execution"
    assert task["target_path"] == "CLAUDE.md"
    assert task["task_context"]["target_file"] == "CLAUDE.md"
    assert "extra_filter" not in task["task_context"]
    assert task["evidence_refs"][0]["id"] == "evidence/task.json"
    assert "agent_note" not in task
    assert len(normalized["blocked_items"]) == 1
    blocked = normalized["blocked_items"][0]
    assert blocked["reason"] == "等待人工确认外部责任方。"
    assert blocked["evidence_refs"][0]["id"] == "evidence/blocked.json"
    assert "agent_note" not in blocked


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
    assert case["schema_version"] == "feedback-eval-case/v1"
    assert case["status"] == "active"
    assert case["asset_layer"] == "candidate"
    assert case["promotion_status"] == "approved"
    assert case["blocking_policy"] == "blocking"
    assert case["prompt"] == "复现工具数据缺失"
    assert case["expected_behavior"].startswith("{")
    assert case["labels"] == ["tool-data"]
    assert case["checks_json"] == {}
    assert "agent_note" not in case


def test_normalize_regression_impact_analysis_output_uses_intermediate_asset_records():
    normalized = normalize_regression_impact_analysis_output(
        {
            "eval_run_id": "erun-1",
            "status": "completed",
            "gate_result": ["not", "object"],
            "impacted_assets": [
                "CLAUDE.md",
                {
                    "asset_id": "eval-1",
                    "summary": "核心回归资产受影响。",
                    "agent_note": {"source": "regression-impact-analyzer"},
                },
            ],
            "summary": {"text": "需要补充回归验证。"},
        }
    )

    assert normalized["status"] == "completed"
    assert normalized["gate_result"] == {}
    assert normalized["impacted_assets"][0] == {"summary": "CLAUDE.md"}
    assert normalized["impacted_assets"][1]["asset_id"] == "eval-1"
    assert "agent_note" not in normalized["impacted_assets"][1]
    assert normalized["recommendations"][0].startswith("{")


def test_validated_feedback_optimization_plan_output_drops_agent_extra_fields():
    plan, plan_error = validate_feedback_optimization_plan_output(
        {
            "batch_id": "fob-1",
            "status": "ready",
            "confidence": "high",
            "actionability": "workspace_change",
            "tasks": [
                {
                    "execution_kind": "workspace_execution",
                    "title": "补充主智能体约束",
                    "target_type": "main_agent_claude_md",
                    "target_path": "CLAUDE.md",
                    "recommendation": "补充读取配置前核查 workspace 的约束。",
                    "task_context": {"target_file": "CLAUDE.md", "agent_note": {"source": "planner"}},
                    "agent_note": {"source": "optimization-planner"},
                }
            ],
        }
    )

    assert plan_error is None
    assert "agent_note" not in plan["tasks"][0]
    assert "agent_note" not in plan["tasks"][0]["task_context"]


def test_validated_execution_eval_and_regression_outputs_drop_agent_extra_fields():
    execution, execution_error = validate_execution_plan_output(
        {
            "optimization_task_id": "opt-1",
            "execution_job_id": "job-1",
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
    impact, impact_error = validate_regression_impact_analysis_output(
        {
            "eval_run_id": "erun-1",
            "status": "completed",
            "impacted_assets": [{"summary": "CLAUDE.md", "agent_note": {"source": "impact"}}],
            "recommendations": ["补充回归验证。"],
        }
    )

    assert execution_error is None
    assert eval_error is None
    assert impact_error is None
    assert "agent_note" not in execution["operations"][0]
    assert "agent_note" not in eval_cases["eval_cases"][0]
    assert "agent_note" not in impact["impacted_assets"][0]


def test_normalized_output_record_drops_extra_agent_fields():
    record = NormalizedExecutionPlanOutput.model_validate(
        {
            "status": "ready",
            "operations": [],
            "agent_notes": {"source": "execution-optimizer"},
        }
    )

    assert "agent_notes" not in record.to_payload()
