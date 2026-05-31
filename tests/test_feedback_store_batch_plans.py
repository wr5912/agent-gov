from feedback_store_test_utils import (
    ClaudeRuntime,
    FeedbackOptimizationBatchPlanGenerateRequest,
    FeedbackSignalCreateRequest,
    LocalSessionStore,
    _create_batch_with_completed_attribution,
    _record_run,
    _store,
    asyncio,
    pytest,
    validate_feedback_optimization_plan_output,
)
from app.runtime.errors import ConflictError


def test_batch_plan_regeneration_records_instruction_and_replaces_plan(tmp_path):
    store, _ = _store(tmp_path)
    batch = _create_batch_with_completed_attribution(store)

    first = store.generate_batch_optimization_plan(
        batch["batch_id"],
        regeneration_instruction="优先修改 triage-alert skill 的使用说明",
    )
    second = store.generate_batch_optimization_plan(
        batch["batch_id"],
        regeneration_instruction="避免改动无关 MCP 配置",
    )

    first_plan = first["optimization_plan"]
    second_plan = second["optimization_plan"]
    assert first_plan["optimization_plan_id"] != second_plan["optimization_plan_id"]
    assert second_plan["status"] == "pending_approval"
    assert second_plan["regeneration_instruction"] == "避免改动无关 MCP 配置"
    assert "避免改动无关 MCP 配置" in second_plan["recommendation"]
    assert "避免改动无关 MCP 配置" in second_plan["rationale"]


def test_feedback_optimization_plan_output_requires_actionable_external_context():
    validated, error = validate_feedback_optimization_plan_output(
        {
            "schema_version": "feedback-optimization-plan-output/v1",
            "batch_id": "fob-test",
            "status": "pending_approval",
            "title": "外部服务优化方案",
            "summary": "统筹归因结果生成任务。",
            "confidence": "high",
            "actionability": "external_guidance",
            "target_type": "external_mcp_service",
            "target_path": None,
            "recommendation": "修复外部 MCP 服务返回数据不完整问题。",
            "expected_effect": "Agent 可获得完整数据。",
            "validation": "回归用例通过。",
            "risk": "外部系统需要变更。",
            "tasks": [
                {
                    "execution_kind": "external_webhook",
                    "title": "补齐外部数据",
                    "description": "任务缺少具体接口和问题对象。",
                    "objective": "提高数据完整性。",
                    "target_type": "external_mcp_service",
                    "owner": "sec-ops-data",
                    "actionability": "external_guidance",
                    "recommendation": "修复数据返回。",
                    "expected_effect": "数据完整。",
                    "validation": "回归通过。",
                    "risk": "需外部系统配合。",
                    "task_context": {"mcp_server": "sec-ops-data"},
                }
            ],
            "blocked_items": [],
        }
    )

    assert error is None
    assert validated is not None
    assert validated["status"] == "needs_human_review"
    assert validated["tasks"] == []
    assert validated["blocked_items"][0]["reason"] == "任务缺少明确的外部对象、接口或问题 ID，不能派发到外部系统。"


def test_feedback_optimization_plan_output_promotes_actionable_blocked_external_item():
    validated, error = validate_feedback_optimization_plan_output(
        {
            "schema_version": "feedback-optimization-plan-output/v1",
            "batch_id": "fob-test",
            "status": "pending_approval",
            "title": "漏洞数据源优化方案",
            "summary": "统筹归因结果生成任务。",
            "confidence": "high",
            "actionability": "external_guidance",
            "target_type": "external_mcp_service",
            "target_path": None,
            "recommendation": "通知外部 MCP 工具提供方修复漏洞数据源。",
            "expected_effect": "Agent 可查询到完整漏洞数据。",
            "validation": "回归用例通过。",
            "risk": "外部系统需要变更。",
            "tasks": [],
            "blocked_items": [
                {
                    "title": "确认并上报漏洞数据源 2026 年数据缺失问题",
                    "target_type": "external_mcp_service",
                    "owner": "sec-ops-data",
                    "actionability": "external_guidance",
                    "problem_type": "tool_data_quality",
                    "reason": (
                        "归因显示 sec-ops-data MCP 工具 list_vulnerabilities 查询漏洞数据时，"
                        "无法获得 2026 年 CVE 数据。"
                    ),
                    "recommendation": (
                        "请 sec-ops-data 工具提供方核查 list_vulnerabilities_api_v1_vulnerabilities_get "
                        "的数据源覆盖范围，确认 2026 年漏洞数据是否缺失。"
                    ),
                    "feedback_case_ids": ["fbc-2026"],
                    "attribution_job_ids": ["fba-2026"],
                }
            ],
        }
    )

    assert error is None
    assert validated is not None
    assert validated["blocked_items"] == []
    assert len(validated["tasks"]) == 1
    task = validated["tasks"][0]
    assert task["execution_kind"] == "external_webhook"
    assert task["status"] == "pending_notification"
    assert task["actionability"] == "external_guidance"
    assert task["target_summary"] == "external:sec-ops-data"
    assert task["task_context"]["mcp_server"] == "sec-ops-data"
    assert task["task_context"]["tool_name"] == "list_vulnerabilities_api_v1_vulnerabilities_get"
    assert "2026" in task["task_context"]["observed_issue"]
    assert "year" in task["task_context"]["affected_fields"]
    assert "cve_coverage" in task["task_context"]["affected_fields"]
    assert task["feedback_case_ids"] == ["fbc-2026"]
    assert task["attribution_job_ids"] == ["fba-2026"]


def test_feedback_optimization_plan_output_promotes_blocked_item_with_plan_context():
    validated, error = validate_feedback_optimization_plan_output(
        {
            "schema_version": "feedback-optimization-plan-output/v1",
            "batch_id": "fob-test",
            "status": "pending_approval",
            "title": "漏洞数据查询优化方案",
            "summary": (
                "归因确认 sec-ops-data MCP 的 list_vulnerabilities_api_v1_vulnerabilities_get "
                "返回的漏洞记录缺少 2026 年 CVE 数据。"
            ),
            "confidence": "medium",
            "actionability": "external_guidance",
            "target_type": "mcp_description",
            "target_path": None,
            "recommendation": "统筹处理漏洞数据覆盖和工具描述问题。",
            "expected_effect": "Agent 能查询到 2026 年漏洞数据。",
            "validation": "回归用例通过。",
            "risk": "外部数据源可能需要修复。",
            "tasks": [],
            "blocked_items": [
                {
                    "title": "确认并上报漏洞数据源 2026 年数据缺失问题",
                    "reason": "当前方案正文已定位到数据源缺失，但该项被智能体放入 blocked_items。",
                    "recommendation": "联系 sec-ops-data 数据维护团队确认 2026 年 CVE 数据覆盖情况。",
                }
            ],
        }
    )

    assert error is None
    assert validated is not None
    assert validated["blocked_items"] == []
    task = validated["tasks"][0]
    assert task["execution_kind"] == "external_webhook"
    assert task["target_summary"] == "external:sec-ops-data"
    assert task["task_context"]["mcp_server"] == "sec-ops-data"
    assert task["task_context"]["tool_name"] == "list_vulnerabilities_api_v1_vulnerabilities_get"
    assert "2026" in task["task_context"]["observed_issue"]


def test_feedback_optimization_plan_output_keeps_generic_blocked_external_item():
    validated, error = validate_feedback_optimization_plan_output(
        {
            "schema_version": "feedback-optimization-plan-output/v1",
            "batch_id": "fob-test",
            "status": "pending_approval",
            "title": "外部问题优化方案",
            "summary": "统筹归因结果生成任务。",
            "confidence": "medium",
            "actionability": "external_guidance",
            "target_type": "external_mcp_service",
            "target_path": None,
            "recommendation": "通知外部系统修复。",
            "expected_effect": "Agent 可获得完整数据。",
            "validation": "回归用例通过。",
            "risk": "外部系统需要变更。",
            "tasks": [],
            "blocked_items": [
                {
                    "title": "确认外部数据缺失问题",
                    "target_type": "external_mcp_service",
                    "actionability": "external_guidance",
                    "reason": "数据不全，但没有定位到具体系统、工具或接口。",
                    "recommendation": "补充外部系统信息后重新生成优化方案。",
                }
            ],
        }
    )

    assert error is None
    assert validated is not None
    assert validated["tasks"] == []
    assert len(validated["blocked_items"]) == 1
    assert validated["blocked_items"][0]["title"] == "确认外部数据缺失问题"


def test_feedback_optimization_plan_output_normalizes_string_attribution_summaries():
    validated, error = validate_feedback_optimization_plan_output(
        {
            "schema_version": "feedback-optimization-plan-output/v1",
            "batch_id": "fob-test",
            "status": "ready_for_execution",
            "title": "漏洞数据查询优化方案",
            "summary": "统筹归因结果生成任务。",
            "problem_types": ["tool_data_quality"],
            "confidence": "medium",
            "actionability": "needs_human_analysis",
            "target_type": "mcp_description",
            "target_path": "",
            "recommendation": "核查漏洞查询工具是否支持年份筛选。",
            "expected_effect": "减少同类数据缺失反馈。",
            "validation": "运行反馈对应回归用例。",
            "risk": "外部数据源可能仍不完整。",
            "attribution_summaries": ["归因确认漏洞查询缺少 2026 年数据。"],
            "tasks": [
                {
                    "execution_kind": "workspace_execution",
                    "title": "核查漏洞查询工具描述",
                    "description": "确认工具是否支持年份筛选。",
                    "objective": "让 Agent 能查询指定年份漏洞。",
                    "target_type": "mcp_description",
                    "target_path": "",
                    "actionability": "direct_workspace_change",
                    "recommendation": "补充年份筛选说明。",
                    "expected_effect": "查询结果覆盖指定年份。",
                    "validation": "回归用例通过。",
                    "risk": "底层数据可能缺失。",
                }
            ],
            "blocked_items": [],
        }
    )

    assert error is None
    assert validated is not None
    assert validated["status"] == "needs_human_review"
    assert validated["attribution_summaries"] == [{"summary": "归因确认漏洞查询缺少 2026 年数据。"}]
    assert validated["tasks"] == []
    assert validated["blocked_items"][0]["reason"] == "任务缺少 target_path，不能交给 execution-optimizer 执行。"


def test_batch_plan_generation_uses_proposal_generator_agent_output(tmp_path, monkeypatch):
    store, settings = _store(tmp_path)
    batch = _create_batch_with_completed_attribution(store)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), feedback_store=store)
    monkeypatch.setattr(runtime, "_provider_configured", lambda: True)
    seen = {}

    async def fake_run_profile_json(**kwargs):
        seen.update(kwargs)
        job_input = kwargs["job_input"]
        return {
            "schema_version": "feedback-optimization-plan-output/v1",
            "batch_id": job_input["batch_id"],
            "status": "pending_approval",
            "title": "补强工作区配置核查",
            "summary": "根据归因结果生成一个 workspace 优化任务。",
            "problem_types": ["tool_misuse"],
            "confidence": "high",
            "actionability": "direct_workspace_change",
            "target_type": "main_agent_claude_md",
            "target_path": "CLAUDE.md",
            "recommendation": "在 CLAUDE.md 中补充回答工作区配置问题前必须读取配置文件的要求。",
            "expected_effect": "Agent 回答同类问题时先读取当前配置。",
            "validation": "使用批次回归用例验证是否读取配置并回答完整。",
            "risk": "可能增加少量工具调用。",
            "source_refs": job_input["source_refs"],
            "feedback_case_ids": job_input["feedback_case_ids"],
            "eval_case_ids": job_input["eval_case_ids"],
            "attribution_job_ids": job_input["attribution_job_ids"],
            "attribution_summaries": [],
            "rationale": "归因结果显示 Agent 未读取当前工作区配置。",
            "evidence_refs": [{"type": "evidence_file", "id": "messages.json", "reason": "回答未核查当前配置。"}],
            "tasks": [
                {
                    "execution_kind": "workspace_execution",
                    "status": "pending_execution",
                    "title": "补充工作区配置核查指令",
                    "description": "在主智能体指令中要求回答工作区配置问题前读取当前配置文件。",
                    "objective": "让 Agent 对配置枚举类问题使用当前文件内容作答。",
                    "target_summary": "workspace:CLAUDE.md",
                    "target_type": "main_agent_claude_md",
                    "target_path": "CLAUDE.md",
                    "owner": "main_agent_workspace",
                    "actionability": "direct_workspace_change",
                    "confidence": "high",
                    "problem_type": "tool_misuse",
                    "recommendation": "追加配置核查要求。",
                    "recommended_actions": ["由 execution-optimizer 生成 CLAUDE.md 的受控追加方案。"],
                    "acceptance_criteria": ["回归用例通过，且回答前读取当前配置文件。"],
                    "expected_effect": "同类反馈不再复现。",
                    "validation": "运行批次回归测试。",
                    "risk": "回答耗时略增。",
                    "analysis_summary": "Agent 未读取当前配置。",
                    "evidence_summary": "messages.json 显示回答来自记忆。",
                    "evidence_refs": [{"type": "evidence_file", "id": "messages.json", "reason": "回答未核查当前配置。"}],
                    "task_context": {"target_file": "CLAUDE.md", "config_section": "workspace-capability-answering"},
                    "feedback_case_ids": job_input["feedback_case_ids"],
                    "eval_case_ids": job_input["eval_case_ids"],
                    "attribution_job_ids": job_input["attribution_job_ids"],
                }
            ],
            "blocked_items": [],
        }

    monkeypatch.setattr(runtime, "_run_profile_json", fake_run_profile_json)

    updated = asyncio.run(runtime.run_batch_optimization_plan(batch["batch_id"], regeneration_instruction="优先保持指令简洁"))
    plan = updated["optimization_plan"]
    plan_task = plan["tasks"][0]
    job = store.get_job(updated["optimization_plan_job_id"])

    assert seen["profile_name"] == "proposal-generator"
    assert seen["expected_schema_version"] == "feedback-optimization-plan-output/v1"
    assert seen["job_type"] == "batch_plan"
    assert "batch_plan_input_json" in seen["prompt"]
    assert seen["job_input"]["regeneration_instruction"] == "优先保持指令简洁"
    assert job["job_type"] == "batch_plan"
    assert job["profile_name"] == "proposal-generator"
    assert job["status"] == "completed"
    assert plan["generated_by"] == "proposal-generator"
    assert plan["status"] == "pending_approval"
    assert plan["source_output_schema_version"] == "feedback-optimization-plan-output/v1"
    assert plan_task["title"] == "补充工作区配置核查指令"
    assert plan_task["target_path"] == "CLAUDE.md"
    assert plan_task["task_context"]["target_file"] == "CLAUDE.md"
    assert plan_task["task_context"]["config_section"] == "workspace-capability-answering"


def test_complete_batch_plan_job_rolls_back_when_batch_update_fails(tmp_path, monkeypatch):
    store, _ = _store(tmp_path)
    batch = _create_batch_with_completed_attribution(store)
    job = store.create_batch_plan_job(batch["batch_id"])

    def fail_batch_update(*args, **kwargs):
        raise RuntimeError("batch update failed")

    monkeypatch.setattr(store, "_update_batch_row", fail_batch_update)

    with pytest.raises(RuntimeError, match="batch update failed"):
        store.complete_batch_plan_job(job["job_id"], store.offline_batch_plan_output(job))

    unchanged_job = store.get_job(job["job_id"])
    unchanged_batch = store.find_optimization_batch(batch["batch_id"])
    assert unchanged_job["status"] == "queued"
    assert unchanged_job["raw_output_json"] is None
    assert unchanged_job["validated_output_json"] is None
    assert unchanged_job["completed_at"] is None
    assert unchanged_batch["status"] == "optimization_plan_queued"
    assert unchanged_batch["optimization_plan_error"] is None


def test_fail_batch_plan_job_rolls_back_when_batch_update_fails(tmp_path, monkeypatch):
    store, _ = _store(tmp_path)
    batch = _create_batch_with_completed_attribution(store)
    job = store.create_batch_plan_job(batch["batch_id"])

    def fail_batch_update(*args, **kwargs):
        raise RuntimeError("batch update failed")

    monkeypatch.setattr(store, "_update_batch_row", fail_batch_update)

    with pytest.raises(RuntimeError, match="batch update failed"):
        store.fail_job(job["job_id"], error_code="AGENT_RUNTIME_ERROR", message="failed")

    unchanged_job = store.get_job(job["job_id"])
    unchanged_batch = store.find_optimization_batch(batch["batch_id"])
    assert unchanged_job["status"] == "queued"
    assert unchanged_job["error_json"] is None
    assert unchanged_job["completed_at"] is None
    assert unchanged_batch["status"] == "optimization_plan_queued"
    assert unchanged_batch["optimization_plan_error"] is None


def test_batch_plan_lists_workspace_tasks_and_prepares_task_execution(tmp_path):
    store, _ = _store(tmp_path)
    batch = _create_batch_with_completed_attribution(store)

    batch = store.generate_batch_optimization_plan(batch["batch_id"])
    plan_task = batch["optimization_plan"]["tasks"][0]
    prepared = store.prepare_batch_plan_task_execution(batch["batch_id"], plan_task["plan_task_id"], comment="执行任务")

    assert plan_task["schema_version"] == "feedback-optimization-plan-task/v2"
    assert plan_task["execution_kind"] == "workspace_execution"
    assert plan_task["target_path"] == "CLAUDE.md"
    assert plan_task["description"]
    assert plan_task["objective"]
    assert plan_task["target_summary"] == "workspace:CLAUDE.md"
    assert "main_agent_claude_md" not in plan_task["title"]
    assert plan_task["recommended_actions"]
    assert plan_task["acceptance_criteria"]
    assert any("回归测试用例通过" in item for item in plan_task["acceptance_criteria"])
    assert all("execution-optimizer" not in item for item in plan_task["acceptance_criteria"])
    assert all("版本快照" not in item for item in plan_task["acceptance_criteria"])
    assert "归因依据" not in plan_task["recommendation"]
    assert "未读取当前配置文件" in plan_task["analysis_summary"]
    assert prepared["optimization_task"]["source"] == "feedback_optimization_batch"
    assert prepared["optimization_task"]["source_plan_task_id"] == plan_task["plan_task_id"]
    assert prepared["optimization_task"]["proposal"]["description"] == plan_task["description"]
    assert prepared["optimization_task"]["proposal"]["recommended_actions"] == plan_task["recommended_actions"]
    assert prepared["optimization_task"]["proposal"]["acceptance_criteria"] == plan_task["acceptance_criteria"]
    assert prepared["plan_task"]["optimization_task_id"] == prepared["optimization_task"]["optimization_task_id"]


def test_batch_plan_external_task_notifies_selected_webhook(tmp_path):
    store, settings = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["external_mcp_service"], comment="alert-0002 事件数据不全"))
    batch = store.create_optimization_batch([{"source_kind": "signal", "source_id": signal["signal_id"]}], title="外部 MCP 优化")
    attribution_job = store.create_attribution_job(batch["feedback_case_ids"][0])
    completed = store.complete_attribution_job(
        attribution_job["job_id"],
        {
            "schema_version": "attribution-output/v1",
            "feedback_case_id": batch["feedback_case_ids"][0],
            "attribution_job_id": attribution_job["job_id"],
            "status": "completed",
            "problem_type": "tool_data_quality",
            "optimization_object_type": "external_mcp_service",
            "actionability": "external_guidance",
            "confidence": "high",
            "human_review_required": False,
            "evidence_refs": [{"type": "evidence_file", "id": "tool_calls.json", "reason": "list_events 查询 alert-0002 时 event_time 与告警窗口不匹配"}],
            "responsibility_boundary": {"owner": "sec-ops-data", "reason": "sec-ops-data 的 list_events 接口需要支持按告警上下文返回事件"},
            "rationale": (
                "sec-ops-data MCP 工具 mcp__sec-ops-data__local_api__list_events_api_v1_events_get 查询 alert-0002 时，"
                "返回事件的 event_time 全部是 2026-02-11，无法支撑 2026-05-25 的告警研判。"
                "需要修复 list_events 接口或底层数据源，按 alert_id 和告警时间窗口返回完整事件。"
            ),
            "recommended_next_step": "generate_proposal",
        },
    )
    batch = store.record_batch_attribution_jobs(batch["batch_id"], [completed])
    batch = store.generate_batch_optimization_plan(batch["batch_id"])
    plan_task = batch["optimization_plan"]["tasks"][0]
    (settings.data_dir / "external-governance-webhooks.yaml").write_text(
        "webhooks:\n  - alias: kb\n    name: 知识库\n    url: http://example.invalid/kb\n",
        encoding="utf-8",
    )
    seen = {}

    def fake_sender(webhook, payload):
        seen["webhook"] = webhook
        seen["payload"] = payload
        return {"http_status": 202, "response_body": "accepted"}

    result = store.notify_batch_plan_task_external(batch["batch_id"], plan_task["plan_task_id"], webhook_alias="kb", sender=fake_sender)

    assert plan_task["execution_kind"] == "external_webhook"
    assert plan_task["schema_version"] == "feedback-optimization-plan-task/v2"
    assert plan_task["target_summary"] == "external:sec-ops-data"
    assert "external_mcp_service" not in plan_task["title"]
    assert "sec-ops-data" in plan_task["title"]
    assert "alert-0002" in plan_task["description"]
    assert "event_time" in plan_task["description"]
    assert plan_task["task_context"]["mcp_server"] == "sec-ops-data"
    assert plan_task["task_context"]["tool_name"] == "mcp__sec-ops-data__local_api__list_events_api_v1_events_get"
    assert plan_task["task_context"]["endpoint"] == "GET /api/v1/events"
    assert "alert-0002" in plan_task["task_context"]["query_ids"]
    assert "event_time" in plan_task["task_context"]["affected_fields"]
    assert plan_task["recommended_actions"]
    assert plan_task["acceptance_criteria"]
    assert any("mcp__sec-ops-data__local_api__list_events_api_v1_events_get" in item for item in plan_task["acceptance_criteria"])
    assert any("alert-0002" in item for item in plan_task["acceptance_criteria"])
    assert all("时 时" not in item for item in plan_task["acceptance_criteria"])
    assert all("Webhook" not in item for item in plan_task["acceptance_criteria"])
    assert all("2xx" not in item for item in plan_task["acceptance_criteria"])
    assert all("payload" not in item for item in plan_task["acceptance_criteria"])
    assert "归因依据" not in plan_task["recommendation"]
    assert seen["payload"]["batch_id"] == batch["batch_id"]
    assert seen["payload"]["plan_task_id"] == plan_task["plan_task_id"]
    assert seen["payload"]["title"] == plan_task["title"]
    assert seen["payload"]["description"] == plan_task["description"]
    assert seen["payload"]["objective"] == plan_task["objective"]
    assert seen["payload"]["target_summary"] == "external:sec-ops-data"
    assert seen["payload"]["task_context"]["mcp_server"] == "sec-ops-data"
    assert seen["payload"]["task_context"]["endpoint"] == "GET /api/v1/events"
    assert seen["payload"]["recommended_actions"] == plan_task["recommended_actions"]
    assert seen["payload"]["acceptance_criteria"] == plan_task["acceptance_criteria"]
    assert "alert-0002" in seen["payload"]["analysis_summary"]
    assert seen["payload"]["source_attribution_job_ids"] == [attribution_job["job_id"]]
    assert result["external_item"]["status"] == "notified"
    assert result["plan_task"]["external_item_id"] == result["external_item"]["external_item_id"]
    assert result["plan_task"]["latest_webhook_alias"] == "kb"


def test_batch_plan_blocks_external_task_without_specific_object_context(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["external_mcp_service"], comment="MCP 数据不全"))
    batch = store.create_optimization_batch([{"source_kind": "signal", "source_id": signal["signal_id"]}], title="外部 MCP 优化")
    attribution_job = store.create_attribution_job(batch["feedback_case_ids"][0])
    completed = store.complete_attribution_job(
        attribution_job["job_id"],
        {
            "schema_version": "attribution-output/v1",
            "feedback_case_id": batch["feedback_case_ids"][0],
            "attribution_job_id": attribution_job["job_id"],
            "status": "completed",
            "problem_type": "tool_data_quality",
            "optimization_object_type": "external_mcp_service",
            "actionability": "external_guidance",
            "confidence": "medium",
            "human_review_required": False,
            "evidence_refs": [{"type": "evidence_file", "id": "tool_calls.json", "reason": "MCP 返回字段缺失"}],
            "responsibility_boundary": {"owner": "knowledge-base-mcp", "reason": "外部 MCP 服务需要补齐字段"},
            "rationale": "知识库 MCP 返回的数据字段不足，需要外部服务侧处理。",
            "recommended_next_step": "generate_proposal",
        },
    )
    batch = store.record_batch_attribution_jobs(batch["batch_id"], [completed])
    batch = store.generate_batch_optimization_plan(batch["batch_id"])
    plan = batch["optimization_plan"]

    assert plan["tasks"] == []
    assert plan["task_summary"]["total"] == 0
    assert len(plan["blocked_items"]) == 1
    assert "证据不足以定位具体外部对象" in plan["blocked_items"][0]["reason"]


def test_batch_plan_puts_non_executable_results_in_blocked_items(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["not_actionable"], comment="无法优化"))
    batch = store.create_optimization_batch([{"source_kind": "signal", "source_id": signal["signal_id"]}], title="不可执行批次")
    attribution_job = store.create_attribution_job(batch["feedback_case_ids"][0])
    completed = store.complete_attribution_job(
        attribution_job["job_id"],
        {
            "schema_version": "attribution-output/v1",
            "feedback_case_id": batch["feedback_case_ids"][0],
            "attribution_job_id": attribution_job["job_id"],
            "status": "needs_human_review",
            "problem_type": "insufficient_information",
            "optimization_object_type": "not_actionable",
            "actionability": "needs_human_analysis",
            "confidence": "low",
            "human_review_required": True,
            "evidence_refs": [{"type": "evidence_file", "id": "feedback.json", "reason": "反馈信息不足"}],
            "responsibility_boundary": {"owner": "developer", "reason": "缺少可落地优化目标"},
            "rationale": "当前反馈没有足够上下文，不能形成 workspace 或外部系统优化任务。",
            "recommended_next_step": "needs_human_review",
        },
    )
    batch = store.record_batch_attribution_jobs(batch["batch_id"], [completed])
    batch = store.generate_batch_optimization_plan(batch["batch_id"])
    plan = batch["optimization_plan"]

    assert batch["status"] == "needs_human_review"
    assert plan["tasks"] == []
    assert plan["task_summary"]["total"] == 0
    assert len(plan["blocked_items"]) == 1
    assert plan["blocked_items"][0]["blocked_item_id"].startswith("fobi-")
    assert plan["blocked_items"][0]["attribution_job_ids"] == [attribution_job["job_id"]]
    assert "未指向可由当前 workspace" in plan["blocked_items"][0]["reason"]


def test_legacy_manual_plan_is_exposed_as_blocked_item_not_task(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["not_actionable"], comment="历史数据"))
    batch = store.create_optimization_batch([{"source_kind": "signal", "source_id": signal["signal_id"]}], title="历史批次")
    legacy_plan = {
        "schema_version": "feedback-optimization-plan/v1",
        "optimization_plan_id": "fop-legacy-manual",
        "batch_id": batch["batch_id"],
        "created_at": "2026-05-20T00:00:00+00:00",
        "status": "needs_human_review",
        "title": "历史不可执行方案",
        "actionability": "needs_human_analysis",
        "target_type": "not_actionable",
        "target_path": None,
        "recommendation": "历史方案没有可执行任务。",
        "no_action_reason": "历史方案需要人工分析。",
    }

    updated = store._update_batch(batch["batch_id"], status="needs_human_review", fields={"optimization_plan": legacy_plan})  # noqa: SLF001
    plan = updated["optimization_plan"]

    assert plan["tasks"] == []
    assert plan["task_summary"]["total"] == 0
    assert len(plan["blocked_items"]) == 1
    assert plan["blocked_items"][0]["blocked_item_id"] == "fopt-legacy-fop-legacy-manual"
    assert plan["blocked_items"][0]["reason"] == "历史方案需要人工分析。"


def test_batch_plan_regeneration_rejects_approved_plan(tmp_path):
    store, _ = _store(tmp_path)
    batch = _create_batch_with_completed_attribution(store)
    batch = store.generate_batch_optimization_plan(batch["batch_id"], regeneration_instruction="优先保留现有技能入口")
    approved = store.approve_batch_optimization_plan(batch["batch_id"], comment="同意执行")
    proposal = store.find_proposal(approved["batch"]["internal_proposal_id"])

    assert proposal["regeneration_instruction"] == "优先保留现有技能入口"
    with pytest.raises(ConflictError, match="已执行或进入执行链路"):
        store.generate_batch_optimization_plan(batch["batch_id"], regeneration_instruction="重新改写目标")
