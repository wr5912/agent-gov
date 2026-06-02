from app.runtime.normalizers.feedback_output_normalizers import (
    normalize_attribution_output,
    normalize_feedback_optimization_plan_output,
)
from app.runtime.normalizers.feedback_output_records import NormalizedExecutionPlanOutput
from app.runtime.normalizers.feedback_output_task_context import (
    external_context_target,
    infer_external_task_context,
    normalize_task_context_payload,
    task_context_has_external_specificity,
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


def test_normalized_output_record_preserves_extra_agent_fields():
    record = NormalizedExecutionPlanOutput.model_validate(
        {
            "status": "ready",
            "operations": [],
            "agent_notes": {"source": "execution-optimizer"},
        }
    )

    assert record.to_payload()["agent_notes"] == {"source": "execution-optimizer"}
