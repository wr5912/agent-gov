from app.runtime.normalizers.feedback_output_normalizers import (
    normalize_attribution_output,
    normalize_execution_plan_output,
    normalize_feedback_eval_case_generation_output,
    normalize_feedback_optimization_plan_output,
    normalize_proposal_output,
    normalize_regression_impact_analysis_output,
)
from app.runtime.normalizers.feedback_output_records import NormalizedExecutionPlanOutput
from app.runtime.normalizers.feedback_output_task_context import (
    external_context_target,
    infer_external_task_context,
    normalize_task_context_payload,
    task_context_has_external_specificity,
)
from app.runtime.feedback_schemas import (
    validate_execution_plan_output,
    validate_feedback_eval_case_generation_output,
    validate_feedback_optimization_plan_output,
    validate_proposal_output,
    validate_regression_impact_analysis_output,
)


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
    assert context["extra_filter"] == {"severity": "high"}
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


def test_normalize_proposal_output_uses_intermediate_item_records_and_preserves_extra_fields():
    normalized = normalize_proposal_output(
        {
            "schema_version": "proposal-output/v1",
            "feedback_case_id": "fc-1",
            "proposal_job_id": "job-1",
            "proposals": [
                "not-an-object",
                {
                    "id": "proposal-1",
                    "target_path": "CLAUDE.md",
                    "recommendation": "补充工具使用约束。",
                    "agent_note": {"source": "proposal-governor"},
                },
            ],
            "external_guidance": [
                "not-an-object",
                {
                    "target": "sec-ops-data MCP service provider",
                    "actionability": "external_guidance",
                    "recommendation": "补齐告警数据字段。",
                    "rationale": "当前工具返回模拟时间戳。",
                    "agent_note": {"source": "proposal-governor"},
                },
            ],
        }
    )

    assert len(normalized["proposals"]) == 1
    assert normalized["proposals"][0]["proposal_id"] == "proposal-1"
    assert normalized["proposals"][0]["title"] == "补充工具使用约束。"
    assert normalized["proposals"][0]["target_type"] == "main_agent_claude_md"
    assert normalized["proposals"][0]["agent_note"] == {"source": "proposal-governor"}
    assert len(normalized["external_guidance"]) == 1
    assert normalized["external_guidance"][0]["owner"] == "sec-ops-data MCP service provider"
    assert normalized["external_guidance"][0]["reason"] == "当前工具返回模拟时间戳。"
    assert normalized["external_guidance"][0]["agent_note"] == {"source": "proposal-governor"}


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

    assert normalized["schema_version"] == "feedback-optimization-plan-output/v1"
    assert normalized["status"] == "pending_approval"
    assert normalized["confidence"] == "medium"
    assert normalized["actionability"] == "direct_workspace_change"
    assert normalized["tasks"] == []
    assert normalized["blocked_items"][0]["reason"] == "任务缺少 target_path，不能交给 execution-optimizer 执行。"
    assert normalized["blocked_items"][0]["title"].startswith("{")


def test_normalize_feedback_plan_output_uses_intermediate_task_records_and_preserves_extra_fields():
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
    assert task["task_context"]["extra_filter"] == {"section": "tools"}
    assert task["evidence_refs"][0]["id"] == "evidence/task.json"
    assert task["agent_note"] == {"source": "optimization-planner"}
    assert len(normalized["blocked_items"]) == 1
    blocked = normalized["blocked_items"][0]
    assert blocked["reason"] == "等待人工确认外部责任方。"
    assert blocked["evidence_refs"][0]["id"] == "evidence/blocked.json"
    assert blocked["agent_note"] == {"source": "optimization-planner"}


def test_normalize_execution_plan_output_uses_intermediate_operation_records_and_preserves_extra_fields():
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
    assert normalized["operations"][0]["agent_note"] == {"source": "execution-optimizer"}


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
    assert case["agent_note"] == {"source": "eval-case-governor"}


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
    assert normalized["impacted_assets"][1]["agent_note"] == {"source": "regression-impact-analyzer"}
    assert normalized["recommendations"][0].startswith("{")


def test_validated_feedback_outputs_preserve_agent_extra_fields():
    proposal, error = validate_proposal_output(
        {
            "schema_version": "proposal-output/v1",
            "feedback_case_id": "fbc-1",
            "proposal_job_id": "fbp-1",
            "status": "completed",
            "proposals": [
                {
                    "target_path": "CLAUDE.md",
                    "actionability": "direct_workspace_change",
                    "recommendation": "补充约束。",
                    "agent_note": {"source": "proposal-governor"},
                }
            ],
            "external_guidance": [
                {
                    "owner": "knowledge-base",
                    "actionability": "external_guidance",
                    "recommendation": "补齐外部说明。",
                    "agent_note": {"source": "proposal-governor"},
                }
            ],
        }
    )
    plan, plan_error = validate_feedback_optimization_plan_output(
        {
            "schema_version": "feedback-optimization-plan-output/v1",
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

    assert error is None
    assert plan_error is None
    assert proposal["proposals"][0]["agent_note"] == {"source": "proposal-governor"}
    assert proposal["external_guidance"][0]["agent_note"] == {"source": "proposal-governor"}
    assert plan["tasks"][0]["agent_note"] == {"source": "optimization-planner"}
    assert plan["tasks"][0]["task_context"]["agent_note"] == {"source": "planner"}


def test_validated_execution_eval_and_regression_outputs_preserve_agent_extra_fields():
    execution, execution_error = validate_execution_plan_output(
        {
            "schema_version": "execution-plan-output/v1",
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
            "schema_version": "feedback-eval-case-generation-output/v1",
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
            "schema_version": "regression-impact-analysis-output/v1",
            "eval_run_id": "erun-1",
            "status": "completed",
            "impacted_assets": [{"summary": "CLAUDE.md", "agent_note": {"source": "impact"}}],
            "recommendations": ["补充回归验证。"],
        }
    )

    assert execution_error is None
    assert eval_error is None
    assert impact_error is None
    assert execution["operations"][0]["agent_note"] == {"source": "execution-optimizer"}
    assert eval_cases["eval_cases"][0]["agent_note"] == {"source": "eval-case-governor"}
    assert impact["impacted_assets"][0]["agent_note"] == {"source": "impact"}


def test_normalized_output_record_preserves_extra_agent_fields():
    record = NormalizedExecutionPlanOutput.model_validate(
        {
            "status": "ready",
            "operations": [],
            "agent_notes": {"source": "execution-optimizer"},
        }
    )

    assert record.to_payload()["agent_notes"] == {"source": "execution-optimizer"}
