import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.feedback_schemas import validate_execution_plan_output, validate_feedback_optimization_plan_output
from app.runtime.feedback_store import FeedbackStore
from app.runtime.schemas import (
    FeedbackAnalysisJobResponse,
    FeedbackOptimizationBatchPlanGenerateRequest,
    FeedbackProposalRegenerateRequest,
    FeedbackSignalCreateRequest,
    SocEventIngestRequest,
)
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import AppSettings


def _settings(tmp_path):
    workspace = tmp_path / "docker" / "volume" / "main-workspace"
    attribution_workspace = tmp_path / "docker" / "volume" / "attribution-analyzer-workspace"
    proposal_workspace = tmp_path / "docker" / "volume" / "proposal-generator-workspace"
    data = tmp_path / "docker" / "volume" / "data"
    claude_root = tmp_path / "docker" / "volume" / "claude-roots" / "main"
    attribution_root = tmp_path / "docker" / "volume" / "claude-roots" / "attribution-analyzer"
    proposal_root = tmp_path / "docker" / "volume" / "claude-roots" / "proposal-generator"
    for path in (workspace, attribution_workspace, proposal_workspace, claude_root / ".claude", attribution_root / ".claude", proposal_root / ".claude"):
        path.mkdir(parents=True, exist_ok=True)
    (workspace / "CLAUDE.md").write_text("# Test Agent\n", encoding="utf-8")
    (workspace / ".mcp.json").write_text("{}\n", encoding="utf-8")
    return AppSettings(
        _env_file=None,
        WORKSPACE_DIR=workspace,
        MAIN_WORKSPACE_DIR=workspace,
        ATTRIBUTION_ANALYZER_WORKSPACE_DIR=attribution_workspace,
        PROPOSAL_GENERATOR_WORKSPACE_DIR=proposal_workspace,
        DATA_DIR=data,
        CLAUDE_ROOT=claude_root,
        MAIN_CLAUDE_ROOT=claude_root,
        ATTRIBUTION_ANALYZER_CLAUDE_ROOT=attribution_root,
        PROPOSAL_GENERATOR_CLAUDE_ROOT=proposal_root,
        CLAUDE_HOME=claude_root / ".claude",
        ENABLE_POLICY_HOOKS=True,
    )


def _store(tmp_path):
    settings = _settings(tmp_path)
    return FeedbackStore(data_dir=settings.data_dir, agent_version_provider=lambda: "main-v-test"), settings


def _record_run(store: FeedbackStore):
    return store.record_run(
        {
            "run_id": "run-1",
            "session_id": "session-1",
            "alert_id": "alert-1",
            "case_id": "case-1",
            "message": "研判告警",
            "messages": [{"event": "AssistantMessage", "content": [{"text": "告警研判摘要"}]}],
            "langfuse_trace_id": "trace-1",
            "langfuse_trace_url": "http://langfuse.local/project/traces/trace-1",
            "answer_summary": "告警研判摘要",
            "agent_activity": {
                "tool_names": ["mcp__sec-ops-data__asset"],
                "tool_calls": [{"name": "mcp__sec-ops-data__asset", "input": {"token": "secret-token"}}],
            },
            "created_at": "2026-05-20T00:00:00+00:00",
            "completed_at": "2026-05-20T00:00:01+00:00",
        }
    )


def _create_eval_case(store: FeedbackStore):
    _record_run(store)
    signal = store.create_signal(
        FeedbackSignalCreateRequest(
            run_id="run-1",
            labels=["tool_data_incomplete"],
            comment="数据不全",
        )
    )
    feedback_case = store.create_case(source_ids=[signal["signal_id"]], title="数据不全")
    store.create_evidence_package(feedback_case["feedback_case_id"])
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    store.complete_attribution_job(
        attribution_job["job_id"],
        {
            "schema_version": "attribution-output/v1",
            "feedback_case_id": feedback_case["feedback_case_id"],
            "attribution_job_id": attribution_job["job_id"],
            "status": "completed",
            "problem_type": "tool_data_quality",
            "optimization_object_type": "main_agent_claude_md",
            "actionability": "direct_workspace_change",
            "confidence": "high",
            "human_review_required": False,
            "evidence_refs": [{"type": "evidence_file", "id": "tool_calls.json", "reason": "没有工具调用"}],
            "responsibility_boundary": {"owner": "main_agent_workspace", "reason": "需要读取配置"},
            "rationale": "回答不完整。",
            "recommended_next_step": "generate_proposal",
        },
    )
    proposal_job = store.create_proposal_job(feedback_case["feedback_case_id"])
    store.complete_proposal_job(
        proposal_job["job_id"],
        {
            "schema_version": "proposal-output/v1",
            "feedback_case_id": feedback_case["feedback_case_id"],
            "proposal_job_id": proposal_job["job_id"],
            "status": "completed",
            "proposals": [
                {
                    "proposal_id": "prop-eval",
                    "title": "补充工具核查要求",
                    "actionability": "direct_workspace_change",
                    "target_type": "main_agent_claude_md",
                    "target_path": "CLAUDE.md",
                    "recommendation": "回答配置问题前读取配置文件。",
                    "expected_effect": "回答更完整。",
                    "validation": "复测原始输入并确认产生工具调用。",
                    "risk": "响应耗时增加。",
                    "requires_approval": True,
                }
            ],
            "external_guidance": [],
            "no_action_reason": None,
        },
    )
    sync = store.sync_feedback_eval_cases(feedback_case_id=feedback_case["feedback_case_id"])
    return sync["eval_cases"][0], feedback_case


def _create_batch_with_completed_attribution(store: FeedbackStore):
    _record_run(store)
    signal = store.create_signal(
        FeedbackSignalCreateRequest(
            run_id="run-1",
            labels=["tool_data_incomplete"],
            comment="数据不全，需要进入批次优化",
        )
    )
    batch = store.create_optimization_batch(
        [{"source_kind": "signal", "source_id": signal["signal_id"]}],
        title="数据不全批次",
    )
    attribution_job = store.create_attribution_job(batch["feedback_case_ids"][0])
    completed = store.complete_attribution_job(
        attribution_job["job_id"],
        {
            "schema_version": "attribution-output/v1",
            "feedback_case_id": batch["feedback_case_ids"][0],
            "attribution_job_id": attribution_job["job_id"],
            "status": "completed",
            "problem_type": "tool_misuse",
            "optimization_object_type": "main_agent_claude_md",
            "actionability": "direct_workspace_change",
            "confidence": "high",
            "human_review_required": False,
            "evidence_refs": [{"type": "evidence_file", "id": "messages.json", "reason": "回答未核查当前配置"}],
            "responsibility_boundary": {"owner": "main_agent_workspace", "reason": "主智能体指令需要约束配置核查"},
            "rationale": "Agent 回答工作区能力问题时未读取当前配置文件。",
            "recommended_next_step": "generate_proposal",
        },
    )
    return store.record_batch_attribution_jobs(batch["batch_id"], [completed])


def _create_approved_task_for_target(store: FeedbackStore, target_path: str):
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_incomplete"]))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    store.complete_attribution_job(attribution_job["job_id"], store.offline_attribution_output(attribution_job))
    proposal_job = store.create_proposal_job(feedback_case["feedback_case_id"])
    store.complete_proposal_job(
        proposal_job["job_id"],
        {
            "schema_version": "proposal-output/v1",
            "feedback_case_id": feedback_case["feedback_case_id"],
            "proposal_job_id": proposal_job["job_id"],
            "status": "completed",
            "proposals": [
                {
                    "title": f"修改 {target_path}",
                    "actionability": "direct_workspace_change",
                    "target_type": "workspace_file",
                    "target_path": target_path,
                    "recommendation": f"按反馈调整 {target_path}。",
                    "expected_effect": "提高反馈场景表现。",
                    "validation": "复测反馈场景。",
                    "risk": "需确认文件内容变更符合预期。",
                    "requires_approval": True,
                }
            ],
            "external_guidance": [],
            "no_action_reason": None,
        },
    )
    proposal = store.list_proposals(feedback_case_id=feedback_case["feedback_case_id"])[0]
    store.review_proposal(proposal["proposal_id"], action="approve", comment="确认")
    return store.create_task(proposal_id=proposal["proposal_id"])


def test_feedback_signal_only_writes_signal_pool(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)

    signal = store.create_signal(
        FeedbackSignalCreateRequest(
            run_id="run-1",
            source_type="explicit_feedback",
            labels=["evidence_gap"],
            comment="证据不足",
        )
    )

    assert signal["signal_id"].startswith("fbs-")
    assert signal["matched_run_id"] == "run-1"
    assert signal["session_id"] == "session-1"
    assert store.list_proposals() == []
    assert store.get_job("missing-job") is None


def test_proposal_regenerate_request_trims_optional_instruction():
    assert FeedbackProposalRegenerateRequest().regeneration_instruction is None
    assert FeedbackProposalRegenerateRequest(regeneration_instruction="   ").regeneration_instruction is None
    assert FeedbackProposalRegenerateRequest(regeneration_instruction="  优先修改 skill  ").regeneration_instruction == "优先修改 skill"
    with pytest.raises(ValidationError):
        FeedbackProposalRegenerateRequest(regeneration_instruction="x" * 2001)


def test_batch_plan_generate_request_trims_optional_instruction():
    assert FeedbackOptimizationBatchPlanGenerateRequest().regeneration_instruction is None
    assert FeedbackOptimizationBatchPlanGenerateRequest(regeneration_instruction="   ").regeneration_instruction is None
    assert (
        FeedbackOptimizationBatchPlanGenerateRequest(regeneration_instruction="  优先保留 triage-alert 约束  ").regeneration_instruction
        == "优先保留 triage-alert 约束"
    )
    with pytest.raises(ValidationError):
        FeedbackOptimizationBatchPlanGenerateRequest(regeneration_instruction="x" * 2001)


def test_implicit_signal_defaults_to_review(tmp_path):
    store, _ = _store(tmp_path)

    signal = store.create_signal(
        FeedbackSignalCreateRequest(
            source_type="implicit_feedback",
            session_id="session-2",
            labels=["timeout"],
        )
    )

    assert signal["auto_captured"] is True
    assert signal["requires_review"] is True


def test_feedback_source_batch_generates_eval_plan_and_task(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(
        FeedbackSignalCreateRequest(
            run_id="run-1",
            labels=["tool_data_incomplete"],
            comment="数据不全，需要复测",
        )
    )

    source = store.update_feedback_source_annotation(
        "signal",
        signal["signal_id"],
        {
            "comment": "数据不全，需要复测",
            "labels": ["tool_data_incomplete", "workspace_config"],
            "priority": "high",
            "status": "triaged",
        },
    )
    generated = store.generate_eval_cases_for_sources(
        [{"source_kind": "signal", "source_id": signal["signal_id"]}],
    )
    batch = store.create_optimization_batch(
        [{"source_kind": "signal", "source_id": signal["signal_id"]}],
        title="数据不全批次",
        priority="high",
    )

    assert source["comment"] == "数据不全，需要复测"
    assert generated["created"] == 1
    assert batch["status"] == "draft"
    assert batch["eval_case_ids"] == [generated["eval_cases"][0]["eval_case_id"]]

    feedback_case_id = batch["feedback_case_ids"][0]
    attribution_job = store.create_attribution_job(feedback_case_id)
    completed_job = store.complete_attribution_job(
        attribution_job["job_id"],
        {
            "schema_version": "attribution-output/v1",
            "feedback_case_id": feedback_case_id,
            "attribution_job_id": attribution_job["job_id"],
            "status": "completed",
            "problem_type": "tool_data_quality",
            "optimization_object_type": "main_agent_claude_md",
            "actionability": "direct_workspace_change",
            "confidence": "high",
            "human_review_required": False,
            "evidence_refs": [{"type": "evidence_file", "id": "tool_calls.json", "reason": "工具调用不足"}],
            "responsibility_boundary": {"owner": "main_agent_workspace", "reason": "需要补充工作区配置核查要求"},
            "rationale": "Agent 没有基于当前工作区配置核查后回答。",
            "recommended_next_step": "generate_proposal",
        },
    )
    batch = store.record_batch_attribution_jobs(batch["batch_id"], [completed_job])
    batch = store.generate_batch_optimization_plan(batch["batch_id"])
    approved = store.approve_batch_optimization_plan(batch["batch_id"], comment="同意执行")

    assert batch["status"] == "pending_approval"
    assert batch["optimization_plan"]["target_path"] == "CLAUDE.md"
    assert approved["optimization_task"]["source"] == "feedback_optimization_batch"
    assert approved["optimization_task"]["source_batch_id"] == batch["batch_id"]
    assert approved["optimization_task"]["eval_case_ids"] == batch["eval_case_ids"]
    assert store.find_proposal(approved["batch"]["internal_proposal_id"])["status"] == "approved"


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
    with pytest.raises(ValueError, match="已执行或进入执行链路"):
        store.generate_batch_optimization_plan(batch["batch_id"], regeneration_instruction="重新改写目标")


def test_soc_event_idempotency_and_pending_correlation(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    matched = store.ingest_soc_event(
        SocEventIngestRequest(
            event_id="evt-1",
            source_system="sec-ops-ui",
            event_type="case.verdict_changed",
            timestamp="2026-05-20T00:02:00+00:00",
            run_id="run-1",
            case_id="case-1",
        )
    )
    duplicate = store.ingest_soc_event(
        SocEventIngestRequest(
            event_id="evt-1",
            source_system="sec-ops-ui",
            event_type="case.verdict_changed",
            timestamp="2026-05-20T00:02:00+00:00",
            run_id="run-1",
        )
    )
    pending = store.ingest_soc_event(
        SocEventIngestRequest(
            event_id="evt-2",
            source_system="sec-ops-ui",
            event_type="evidence.added",
            timestamp="2026-05-20T00:03:00+00:00",
            case_id="missing-case",
        )
    )

    assert matched["correlation_status"] == "matched"
    assert duplicate["correlation_status"] == "duplicate"
    assert pending["correlation_status"] == "pending_correlation"
    assert pending["pending_correlation"]["pending_id"].startswith("pc-")


def test_case_evidence_and_job_outputs(tmp_path):
    store, _ = _store(tmp_path)
    store.set_langfuse_trace_fetcher(lambda trace_id: {"id": trace_id, "input": {"raw": True}, "observations": [{"name": "tool"}]})
    _record_run(store)
    signal = store.create_signal(
        FeedbackSignalCreateRequest(run_id="run-1", labels=["evidence_gap"], comment="证据不足")
    )
    feedback_case = store.create_case(source_ids=[signal["signal_id"]], priority="high")
    evidence = store.create_evidence_package(feedback_case["feedback_case_id"])
    duplicate_evidence = store.create_evidence_package(feedback_case["feedback_case_id"])

    assert feedback_case["status"] == "pending_evidence"
    assert evidence["schema_version"] == "evidence-package/v1"
    assert duplicate_evidence["evidence_package_id"] == evidence["evidence_package_id"]
    assert store.get_evidence_package_file(evidence["evidence_package_id"], "feedback.json")["file_name"] == "feedback.json"
    assert store.get_evidence_package_file(evidence["evidence_package_id"], "../feedback.json") is None
    assert store.get_evidence_package_file(evidence["evidence_package_id"], "manifest.json") is None
    assert evidence["completeness"]["has_feedback"] is True
    assert {item["path"] for item in evidence["included_files"]} >= {
        "feedback.json",
        "runs.json",
        "sessions.json",
        "tool_calls.json",
        "soc_events.json",
        "trace_summary.json",
        "main_agent_version.json",
        "redaction_report.json",
        "messages.json",
        "agent_activity.json",
        "langfuse_trace_refs.json",
    }
    assert evidence["source_refs"]["trace_ids"] == ["trace-1"]
    assert evidence["completeness"]["has_messages"] is True
    assert evidence["completeness"]["has_langfuse_trace_refs"] is True
    assert evidence["completeness"]["has_langfuse_trace_details"] is False
    assert store.get_evidence_package_file(evidence["evidence_package_id"], "messages.json")["content"][0]["messages"]
    trace_refs = store.get_evidence_package_file(evidence["evidence_package_id"], "langfuse_trace_refs.json")["content"]
    assert trace_refs[0]["trace_url"] == "http://langfuse.local/project/traces/trace-1"
    assert store.get_evidence_package_file(evidence["evidence_package_id"], "langfuse_traces.json") is None

    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    store.start_job(attribution_job["job_id"])
    completed = store.complete_attribution_job(attribution_job["job_id"], store.offline_attribution_output(attribution_job))
    output = store.get_job_output(attribution_job["job_id"], "attribution")

    assert completed["status"] == "completed"
    assert store.create_attribution_job(feedback_case["feedback_case_id"])["job_id"] == attribution_job["job_id"]
    assert output["schema_version"] == "attribution-output/v1"
    assert output["actionability"] == "needs_human_analysis"

    proposal_job = store.create_proposal_job(feedback_case["feedback_case_id"])
    store.start_job(proposal_job["job_id"])
    completed_proposal = store.complete_proposal_job(proposal_job["job_id"], store.offline_proposal_output(proposal_job))
    proposal_output = store.get_job_output(proposal_job["job_id"], "proposal")

    assert completed_proposal["status"] == "completed"
    assert store.create_proposal_job(feedback_case["feedback_case_id"])["job_id"] == proposal_job["job_id"]
    assert proposal_output["external_guidance"]
    assert store.list_proposals() == []


def test_regenerated_proposal_job_records_single_use_instruction(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_incomplete"], comment="数据不全"))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    store.create_evidence_package(feedback_case["feedback_case_id"])
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    store.complete_attribution_job(attribution_job["job_id"], store.offline_attribution_output(attribution_job))

    empty_instruction_job = store.create_proposal_job(
        feedback_case["feedback_case_id"],
        force=True,
        regeneration_instruction="   ",
    )
    assert "regeneration_instruction" not in empty_instruction_job["input_json"]

    job = store.create_proposal_job(
        feedback_case["feedback_case_id"],
        force=True,
        regeneration_instruction="  请优先考虑修改 triage-alert skill。  ",
    )
    input_payload = json.loads(Path(job["input_path"]).read_text(encoding="utf-8"))

    assert job["input_json"]["regeneration_instruction"] == "请优先考虑修改 triage-alert skill。"
    assert input_payload["regeneration_instruction"] == "请优先考虑修改 triage-alert skill。"


def test_external_guidance_creates_governance_item_and_notifies_selected_webhook(tmp_path):
    store, settings = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_quality"], comment="知识库缺少条目"))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    store.complete_attribution_job(attribution_job["job_id"], store.offline_attribution_output(attribution_job))
    proposal_job = store.create_proposal_job(feedback_case["feedback_case_id"])
    store.complete_proposal_job(
        proposal_job["job_id"],
        {
            "schema_version": "proposal-output/v1",
            "feedback_case_id": feedback_case["feedback_case_id"],
            "proposal_job_id": proposal_job["job_id"],
            "status": "completed",
            "proposals": [],
            "external_guidance": [
                {
                    "owner": "knowledge-base",
                    "actionability": "external_guidance",
                    "recommendation": "补充漏洞处置 SOP 条目。",
                    "reason": "当前知识库无对应处置流程。",
                }
            ],
            "no_action_reason": None,
        },
    )

    output = store.get_job_output(proposal_job["job_id"], "proposal")
    items = store.list_external_governance_items(feedback_case_id=feedback_case["feedback_case_id"])

    assert len(items) == 1
    assert output["external_guidance"][0]["external_item_id"] == items[0]["external_item_id"]
    assert items[0]["status"] == "pending_notification"

    (settings.data_dir / "external-governance-webhooks.yaml").write_text(
        """
webhooks:
  - alias: knowledge-base
    name: 知识库
    url: http://example.invalid/kb
    token: dev-token
""".strip(),
        encoding="utf-8",
    )
    seen = {}

    def fake_sender(webhook, payload):
        seen["webhook"] = webhook
        seen["payload"] = payload
        return {"http_status": 201, "response_body": "created"}

    updated = store.notify_external_governance_item(items[0]["external_item_id"], webhook_alias="knowledge-base", sender=fake_sender)

    assert store.list_external_webhooks()[0]["alias"] == "knowledge-base"
    assert seen["webhook"]["token"] == "dev-token"
    assert seen["payload"]["schema_version"] == "external-governance-notification/v1"
    assert seen["payload"]["webhook_alias"] == "knowledge-base"
    assert updated["status"] == "notified"
    assert updated["latest_notification"]["status"] == "sent"
    assert updated["latest_notification"]["http_status"] == 201


def test_external_governance_notify_requires_known_webhook_alias(tmp_path):
    store, settings = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_quality"]))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    store.complete_attribution_job(attribution_job["job_id"], store.offline_attribution_output(attribution_job))
    proposal_job = store.create_proposal_job(feedback_case["feedback_case_id"])
    store.complete_proposal_job(
        proposal_job["job_id"],
        {
            "schema_version": "proposal-output/v1",
            "feedback_case_id": feedback_case["feedback_case_id"],
            "proposal_job_id": proposal_job["job_id"],
            "status": "completed",
            "proposals": [],
            "external_guidance": [
                {
                    "owner": "sec-ops-data-mcp",
                    "actionability": "external_guidance",
                    "recommendation": "检查 MCP 服务数据字段。",
                    "reason": "字段缺失。",
                }
            ],
            "no_action_reason": None,
        },
    )
    item = store.list_external_governance_items(feedback_case_id=feedback_case["feedback_case_id"])[0]

    try:
        store.notify_external_governance_item(item["external_item_id"], webhook_alias="missing")
    except ValueError as exc:
        assert "External governance webhook config not found" in str(exc)
    else:
        raise AssertionError("missing webhook config should fail")

    (settings.data_dir / "external-governance-webhooks.yaml").write_text(
        "webhooks:\n  - alias: other\n    name: Other\n    url: http://example.invalid/other\n",
        encoding="utf-8",
    )
    try:
        store.notify_external_governance_item(item["external_item_id"], webhook_alias="missing")
    except ValueError as exc:
        assert "Unknown external governance webhook alias" in str(exc)
    else:
        raise AssertionError("unknown webhook alias should fail")


def test_list_cases_returns_latest_case_versions_only(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["evidence_gap"]))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])

    assert len(store.list_cases()) == 1

    evidence = store.create_evidence_package(feedback_case["feedback_case_id"])
    cases_after_evidence = store.list_cases()

    assert len(cases_after_evidence) == 1
    assert cases_after_evidence[0]["feedback_case_id"] == feedback_case["feedback_case_id"]
    assert cases_after_evidence[0]["status"] == "pending_attribution"
    assert cases_after_evidence[0]["evidence_package_ids"] == [evidence["evidence_package_id"]]
    assert store.list_cases(status="pending_evidence") == []

    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    cases_after_attribution = store.list_cases()

    assert len(cases_after_attribution) == 1
    assert cases_after_attribution[0]["feedback_case_id"] == feedback_case["feedback_case_id"]
    assert cases_after_attribution[0]["status"] == "attribution_queued"
    assert cases_after_attribution[0]["evidence_package_ids"] == [evidence["evidence_package_id"]]
    assert cases_after_attribution[0]["attribution_job_ids"] == [attribution_job["job_id"]]


def test_debug_evidence_can_be_disabled(tmp_path):
    settings = _settings(tmp_path)
    store = FeedbackStore(
        data_dir=settings.data_dir,
        agent_version_provider=lambda: "main-v-test",
        enable_debug_evidence=False,
    )
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["evidence_gap"]))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    evidence = store.create_evidence_package(feedback_case["feedback_case_id"])

    included = {item["path"] for item in evidence["included_files"]}
    tool_calls = store.get_evidence_package_file(evidence["evidence_package_id"], "tool_calls.json")["content"]

    assert "messages.json" not in included
    assert "langfuse_traces.json" not in included
    assert tool_calls[0]["input"]["token"] == "[REDACTED]"


def test_failed_feedback_jobs_can_retry_without_duplicating_active_jobs(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["evidence_gap"]))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])

    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    assert store.create_attribution_job(feedback_case["feedback_case_id"])["job_id"] == attribution_job["job_id"]
    failed_attribution = store.fail_job(attribution_job["job_id"], error_code="AGENT_RUNTIME_ERROR", message="failed")
    failed_case = store.find_case(feedback_case["feedback_case_id"])
    retried_attribution = store.create_attribution_job(feedback_case["feedback_case_id"])

    assert failed_attribution["error_json"]["message"] == "failed"
    assert FeedbackAnalysisJobResponse(**failed_attribution).error_json["error_code"] == "AGENT_RUNTIME_ERROR"
    assert failed_case["status"] == "pending_attribution"
    assert retried_attribution["job_id"] != attribution_job["job_id"]
    store.complete_attribution_job(retried_attribution["job_id"], store.offline_attribution_output(retried_attribution))

    proposal_job = store.create_proposal_job(feedback_case["feedback_case_id"])
    assert store.create_proposal_job(feedback_case["feedback_case_id"])["job_id"] == proposal_job["job_id"]
    failed_proposal = store.fail_job(proposal_job["job_id"], error_code="AGENT_RUNTIME_ERROR", message="failed")
    failed_proposal_case = store.find_case(feedback_case["feedback_case_id"])
    retried_proposal = store.create_proposal_job(feedback_case["feedback_case_id"])

    assert failed_proposal["error_json"]["message"] == "failed"
    assert failed_proposal_case["status"] == "pending_proposal"
    assert retried_proposal["job_id"] != proposal_job["job_id"]


def test_force_attribution_discards_current_job_and_downstream_proposal(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_incomplete"]))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    store.complete_attribution_job(attribution_job["job_id"], store.offline_attribution_output(attribution_job))
    proposal_job = store.create_proposal_job(feedback_case["feedback_case_id"])
    store.complete_proposal_job(proposal_job["job_id"], store.offline_proposal_output(proposal_job))

    regenerated = store.create_attribution_job(feedback_case["feedback_case_id"], force=True)
    updated_case = store.find_case(feedback_case["feedback_case_id"])

    assert regenerated["job_id"] != attribution_job["job_id"]
    assert store.get_job(attribution_job["job_id"]) is None
    assert store.get_job(proposal_job["job_id"]) is None
    assert updated_case["attribution_job_ids"] == [regenerated["job_id"]]
    assert updated_case["proposal_job_ids"] == []


def test_stale_running_attribution_is_discarded_before_retry(tmp_path):
    store, settings = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_incomplete"]))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    stale_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    store._append_job_update(stale_job["job_id"], status="running", started_at="2026-01-01T00:00:00+00:00")  # noqa: SLF001
    stale_dir = settings.data_dir / ".runtime-tmp" / "jobs" / stale_job["job_id"]

    retried = store.create_attribution_job(feedback_case["feedback_case_id"])
    updated_case = store.find_case(feedback_case["feedback_case_id"])

    assert retried["job_id"] != stale_job["job_id"]
    assert store.get_job(stale_job["job_id"]) is None
    assert not stale_dir.exists()
    assert updated_case["attribution_job_ids"] == [retried["job_id"]]


def test_batch_attribution_uses_current_jobs_and_resets_downstream_plan(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_incomplete"], comment="数据不全"))
    batch = store.create_optimization_batch([{"source_kind": "signal", "source_id": signal["signal_id"]}])
    feedback_case_id = batch["feedback_case_ids"][0]
    attribution_job = store.create_attribution_job(feedback_case_id)
    completed = store.complete_attribution_job(attribution_job["job_id"], store.offline_attribution_output(attribution_job))
    batch = store.record_batch_attribution_jobs(batch["batch_id"], [completed])
    batch = store.generate_batch_optimization_plan(batch["batch_id"])

    reset = store.reset_batch_attribution(batch["batch_id"])

    assert batch["optimization_plan"]
    assert reset["status"] == "draft"
    assert reset["attribution_job_ids"] == []
    assert reset["optimization_plan"] is None
    assert store.get_job(attribution_job["job_id"]) is None


def test_batch_attribution_status_requires_all_current_jobs_completed(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    first = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_incomplete"], comment="第一条"))
    second = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_incomplete"], comment="第二条"))
    batch = store.create_optimization_batch(
        [
            {"source_kind": "signal", "source_id": first["signal_id"]},
            {"source_kind": "signal", "source_id": second["signal_id"]},
        ]
    )
    first_job = store.create_attribution_job(batch["feedback_case_ids"][0])
    second_job = store.create_attribution_job(batch["feedback_case_ids"][1])
    completed = store.complete_attribution_job(first_job["job_id"], store.offline_attribution_output(first_job))
    running = store.start_job(second_job["job_id"])

    running_batch = store.record_batch_attribution_jobs(batch["batch_id"], [completed, running])
    failed = store.fail_job(second_job["job_id"], error_code="AGENT_RUNTIME_ERROR", message="failed")
    failed_batch = store.record_batch_attribution_jobs(batch["batch_id"], [completed, failed])

    assert running_batch["status"] == "attribution_running"
    assert running_batch["attribution_summary"]["running"] == 1
    assert failed_batch["status"] == "needs_human_review"
    assert failed_batch["attribution_summary"]["needs_review_or_failed"] == 1


def test_batch_detail_refreshes_latest_task_execution_job(tmp_path):
    store, _ = _store(tmp_path)
    batch = _create_batch_with_completed_attribution(store)
    batch = store.generate_batch_optimization_plan(batch["batch_id"])
    approved = store.approve_batch_optimization_plan(batch["batch_id"], comment="执行优化")
    task_id = approved["optimization_task"]["optimization_task_id"]

    first = store.create_execution_job(task_id, force=True)
    store.start_execution_job(first["execution_job_id"])
    store.complete_execution_job(
        first["execution_job_id"],
        {
            "schema_version": "execution-plan-output/v1",
            "optimization_task_id": task_id,
            "execution_job_id": first["execution_job_id"],
            "status": "needs_human_review",
            "summary": "首次执行需要复核。",
            "operations": [],
            "no_action_reason": "首次执行不可用。",
        },
    )
    second = store.create_execution_job(task_id, force=True)
    store.start_execution_job(second["execution_job_id"])
    store.complete_execution_job(
        second["execution_job_id"],
        {
            "schema_version": "execution-plan-output/v1",
            "optimization_task_id": task_id,
            "execution_job_id": second["execution_job_id"],
            "status": "needs_human_review",
            "summary": "重试执行需要复核。",
            "operations": [],
            "no_action_reason": "重试执行不可用。",
        },
    )

    refreshed = store.find_optimization_batch(batch["batch_id"])

    assert refreshed["optimization_task"]["latest_execution_job_id"] == second["execution_job_id"]
    assert refreshed["execution_job_id"] == second["execution_job_id"]
    assert refreshed["execution_job"]["validated_output_json"]["summary"] == "重试执行需要复核。"


def test_schema_review_jobs_are_not_implicitly_recreated(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["evidence_gap"]))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])

    reviewed_attribution = store.complete_attribution_job(attribution_job["job_id"], {"schema_version": "attribution-output/v1"})
    attribution_case = store.find_case(feedback_case["feedback_case_id"])
    reused_attribution = store.create_attribution_job(feedback_case["feedback_case_id"])

    assert reviewed_attribution["status"] == "needs_human_review"
    assert reviewed_attribution["error_json"]["message"] == "分析 Agent 输出不符合 schema。"
    assert reviewed_attribution["error_json"]["validation_errors"]
    assert attribution_case["status"] == "needs_human_review"
    assert reused_attribution["job_id"] == attribution_job["job_id"]
    regenerated_attribution = store.create_attribution_job(feedback_case["feedback_case_id"], force=True)
    assert regenerated_attribution["job_id"] != attribution_job["job_id"]
    assert regenerated_attribution["status"] == "queued"

    proposal_signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_incomplete"]))
    proposal_case = store.create_case(source_ids=[proposal_signal["signal_id"]])
    valid_attribution = store.create_attribution_job(proposal_case["feedback_case_id"])
    store.complete_attribution_job(valid_attribution["job_id"], store.offline_attribution_output(valid_attribution))
    proposal_job = store.create_proposal_job(proposal_case["feedback_case_id"])
    reviewed_proposal = store.complete_proposal_job(proposal_job["job_id"], {"schema_version": "proposal-output/v1"})
    proposal_case_after_review = store.find_case(proposal_case["feedback_case_id"])
    reused_proposal = store.create_proposal_job(proposal_case["feedback_case_id"])

    assert reviewed_proposal["status"] == "needs_human_review"
    assert proposal_case_after_review["status"] == "needs_human_review"
    assert reused_proposal["job_id"] == proposal_job["job_id"]


def test_legacy_schema_error_message_is_normalized_on_read(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["evidence_gap"]))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    validation_errors = [{"type": "literal_error", "loc": ["problem_type"], "msg": "invalid enum"}]

    store._set_job_json(  # noqa: SLF001 - regression coverage for legacy persisted job payloads.
        attribution_job["job_id"],
        error_json={
            "error_code": "SCHEMA_VALIDATION_FAILED",
            "message": json.dumps(validation_errors),
            "job_id": attribution_job["job_id"],
        },
    )
    job = store.get_job(attribution_job["job_id"])

    assert job["error_json"]["message"] == "分析 Agent 输出不符合 schema。"
    assert job["error_json"]["validation_errors"] == validation_errors


def test_proposal_target_policy_and_task_requires_approval(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["skill_gap"]))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    store.complete_attribution_job(
        attribution_job["job_id"],
        {
            "schema_version": "attribution-output/v1",
            "feedback_case_id": feedback_case["feedback_case_id"],
            "attribution_job_id": attribution_job["job_id"],
            "status": "completed",
            "problem_type": "skill_gap",
            "optimization_object_type": "skill",
            "actionability": "direct_workspace_change",
            "confidence": "medium",
            "human_review_required": True,
            "evidence_refs": [{"type": "run", "id": "run-1", "reason": "缺少证据链"}],
            "responsibility_boundary": {"owner": "main_agent_workspace", "reason": "skill 说明不足"},
            "rationale": "需要补强技能",
            "recommended_next_step": "generate_proposal",
        },
    )
    proposal_job = store.create_proposal_job(feedback_case["feedback_case_id"])
    store.complete_proposal_job(
        proposal_job["job_id"],
        {
            "schema_version": "proposal-output/v1",
            "feedback_case_id": feedback_case["feedback_case_id"],
            "proposal_job_id": proposal_job["job_id"],
            "status": "completed",
            "proposals": [
                {
                    "title": "补强证据链要求",
                    "actionability": "direct_workspace_change",
                    "target_type": "skill",
                    "target_path": ".claude/skills/alert-triage/SKILL.md",
                    "recommendation": "增加 evidence_refs 输出要求。",
                    "expected_effect": "提高可核查性。",
                    "validation": "新增回归样例。",
                    "risk": "回答略变长。",
                    "requires_approval": True,
                },
                {
                    "title": "排除目标",
                    "actionability": "direct_workspace_change",
                    "target_type": "secret",
                    "target_path": "node_modules/pkg/index.js",
                    "recommendation": "不应进入 task。",
                    "expected_effect": "无",
                    "validation": "无",
                    "risk": "高",
                    "requires_approval": True,
                },
            ],
            "external_guidance": [],
            "no_action_reason": None,
        },
    )

    proposals = store.list_proposals()
    assert len(proposals) == 1
    assert proposals[0]["target_path"] == ".claude/skills/alert-triage/SKILL.md"
    assert store.create_task(proposal_id=proposals[0]["proposal_id"]) is None

    store.review_proposal(proposals[0]["proposal_id"], action="approve", comment="确认")
    task = store.create_task(proposal_id=proposals[0]["proposal_id"], comment="执行")
    assert task["optimization_task_id"].startswith("opt-")
    assert task["target_paths"] == [".claude/skills/alert-triage/SKILL.md"]
    task_again = store.create_task(proposal_id=proposals[0]["proposal_id"], comment="重复点击")
    assert task_again["optimization_task_id"] == task["optimization_task_id"]
    tasks = [item for item in store.list_tasks() if item["proposal_id"] == proposals[0]["proposal_id"]]
    assert len(tasks) == 1


def test_execution_job_lifecycle_updates_task(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_incomplete"], comment="数据不全"))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    store.complete_attribution_job(attribution_job["job_id"], store.offline_attribution_output(attribution_job))
    proposal_job = store.create_proposal_job(feedback_case["feedback_case_id"])
    store.complete_proposal_job(
        proposal_job["job_id"],
        {
            "schema_version": "proposal-output/v1",
            "feedback_case_id": feedback_case["feedback_case_id"],
            "proposal_job_id": proposal_job["job_id"],
            "status": "completed",
            "proposals": [
                {
                    "proposal_id": "prop-exec",
                    "title": "追加配置读取要求",
                    "actionability": "direct_workspace_change",
                    "target_type": "main_agent_claude_md",
                    "target_path": "CLAUDE.md",
                    "recommendation": "在 CLAUDE.md 增加配置读取要求。",
                    "expected_effect": "提高回答完整性。",
                    "validation": "复测反馈场景。",
                    "risk": "响应略变慢。",
                    "requires_approval": True,
                }
            ],
            "external_guidance": [],
            "no_action_reason": None,
        },
    )
    proposal = store.list_proposals(feedback_case_id=feedback_case["feedback_case_id"])[0]
    store.review_proposal(proposal["proposal_id"], action="approve", comment="确认")
    task = store.create_task(proposal_id=proposal["proposal_id"])

    job = store.create_execution_job(task["optimization_task_id"])
    assert job["input_path"].endswith("/execution/input.json")
    input_payload = json.loads(Path(job["input_path"]).read_text(encoding="utf-8"))
    assert input_payload["execution_job_id"] == job["execution_job_id"]
    assert input_payload["target_paths"] == ["CLAUDE.md"]

    store.start_execution_job(job["execution_job_id"])
    completed = store.complete_execution_job(
        job["execution_job_id"],
        {
            "schema_version": "execution-plan-output/v1",
            "optimization_task_id": task["optimization_task_id"],
            "execution_job_id": job["execution_job_id"],
            "status": "ready",
            "baseline_agent_version_id": task["baseline_agent_version_id"],
            "summary": "追加一条配置读取要求。",
            "operations": [
                {
                    "operation": "append_text",
                    "path": "CLAUDE.md",
                    "append_text": "\n配置读取要求。\n",
                    "rationale": "让 Agent 回答前读取配置。",
                }
            ],
            "validation": "复测反馈场景。",
            "risk": "响应略变慢。",
            "human_review_required": True,
        },
    )
    updated_task = store.find_task(task["optimization_task_id"])

    assert completed["status"] == "ready"
    assert updated_task["status"] == "execution_ready"
    assert updated_task["latest_execution_job_id"] == job["execution_job_id"]
    assert updated_task["latest_execution_job"]["validated_output_json"]["operations"][0]["path"] == "CLAUDE.md"


def test_execution_job_accepts_any_managed_workspace_file(tmp_path):
    store, settings = _store(tmp_path)
    target = settings.main_workspace_dir / "mcp_servers" / "security_kb_mcp" / "kb.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("rules:\n  - old\n", encoding="utf-8")
    task = _create_approved_task_for_target(store, "mcp_servers/security_kb_mcp/kb.yaml")

    job = store.create_execution_job(task["optimization_task_id"])
    input_payload = json.loads(Path(job["input_path"]).read_text(encoding="utf-8"))
    context = input_payload["target_file_contexts"][0]

    assert input_payload["allowed_target_paths"] == ["mcp_servers/security_kb_mcp/kb.yaml"]
    assert input_payload["target_policy"]["type"] == "main_workspace_managed_full_with_excludes"
    assert context["path"] == "mcp_servers/security_kb_mcp/kb.yaml"
    assert context["managed"] is True
    assert context["exists"] is True
    assert context["type"] == "file"
    assert context["content_text"] == "rules:\n  - old\n"
    assert context["sha256"]


def test_execution_targets_reject_workspace_excluded_paths(tmp_path):
    store, _ = _store(tmp_path)

    assert store.target_allowed("README.md") is True
    assert store.target_allowed("mcp_servers/security_kb_mcp/kb.yaml") is True
    assert store.target_allowed("node_modules/pkg/index.js") is False
    assert store.target_allowed(".git/config") is False
    assert store.target_allowed("dist/bundle.js") is False
    assert store.target_allowed(".venv/bin/python") is False
    assert store.target_allowed("../escape") is False
    assert store.target_allowed("/main-workspace/CLAUDE.md") is False


def test_execution_plan_binds_expected_sha_from_target_context(tmp_path):
    store, _ = _store(tmp_path)
    task = _create_approved_task_for_target(store, "CLAUDE.md")
    job = store.create_execution_job(task["optimization_task_id"])
    context = job["input_json"]["target_file_contexts"][0]
    store.start_execution_job(job["execution_job_id"])

    completed = store.complete_execution_job(
        job["execution_job_id"],
        {
            "schema_version": "execution-plan-output/v1",
            "optimization_task_id": task["optimization_task_id"],
            "execution_job_id": job["execution_job_id"],
            "status": "ready",
            "baseline_agent_version_id": task["baseline_agent_version_id"],
            "summary": "替换 CLAUDE.md。",
            "operations": [
                {
                    "operation": "replace_file",
                    "path": "CLAUDE.md",
                    "content": "# Updated\n",
                    "rationale": "测试绑定 hash。",
                }
            ],
            "validation": "检查文件内容。",
            "risk": "测试风险。",
            "human_review_required": True,
        },
    )

    operation = completed["validated_output_json"]["operations"][0]
    assert operation["expected_sha256"] == context["sha256"]


def test_execution_output_fills_system_fields_from_job_context(tmp_path):
    store, _ = _store(tmp_path)
    task = _create_approved_task_for_target(store, ".mcp.json")
    job = store.create_execution_job(task["optimization_task_id"])
    store.start_execution_job(job["execution_job_id"])

    completed = store.complete_execution_job(
        job["execution_job_id"],
        {
            "schema_version": "execution-plan-output/v1",
            "execution_job_id": job["execution_job_id"],
            "status": "needs_human_review",
            "summary": "目标文件与提案意图不匹配，需要人工确认。",
            "operations": [],
            "no_action_reason": "提案要求调整 Agent 行为，但 .mcp.json 仅用于 MCP 连接配置。",
            "validation": None,
            "risk": None,
        },
    )

    assert completed["status"] == "needs_human_review"
    assert completed["validated_output_json"]["optimization_task_id"] == task["optimization_task_id"]
    assert completed["validated_output_json"]["baseline_agent_version_id"] == task["baseline_agent_version_id"]
    assert completed["error_json"] is None


def test_execution_plan_rejects_non_text_or_skipped_target(tmp_path):
    store, settings = _store(tmp_path)
    target = settings.main_workspace_dir / "assets" / "logo.bin"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x00\x01binary")
    task = _create_approved_task_for_target(store, "assets/logo.bin")
    job = store.create_execution_job(task["optimization_task_id"])
    store.start_execution_job(job["execution_job_id"])

    failed = store.complete_execution_job(
        job["execution_job_id"],
        {
            "schema_version": "execution-plan-output/v1",
            "optimization_task_id": task["optimization_task_id"],
            "execution_job_id": job["execution_job_id"],
            "status": "ready",
            "baseline_agent_version_id": task["baseline_agent_version_id"],
            "summary": "替换二进制文件。",
            "operations": [
                {
                    "operation": "replace_file",
                    "path": "assets/logo.bin",
                    "content": "not-binary",
                    "rationale": "二进制目标不应自动改。",
                }
            ],
            "validation": "不应通过。",
            "risk": "不应通过。",
            "human_review_required": True,
        },
    )

    assert failed["status"] == "failed"
    assert failed["error_json"]["error_code"] == "EXECUTION_PLAN_UNSAFE"
    assert "not safely editable" in failed["error_json"]["message"]


def test_execution_optimizer_uses_materialized_input_path(tmp_path, monkeypatch):
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
    import claude_agent_sdk

    store, settings = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_incomplete"], comment="数据不全"))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    store.complete_attribution_job(attribution_job["job_id"], store.offline_attribution_output(attribution_job))
    proposal_job = store.create_proposal_job(feedback_case["feedback_case_id"])
    store.complete_proposal_job(
        proposal_job["job_id"],
        {
            "schema_version": "proposal-output/v1",
            "feedback_case_id": feedback_case["feedback_case_id"],
            "proposal_job_id": proposal_job["job_id"],
            "status": "completed",
            "proposals": [
                {
                    "proposal_id": "prop-exec-runtime",
                    "title": "补充配置读取要求",
                    "actionability": "direct_workspace_change",
                    "target_type": "main_agent_claude_md",
                    "target_path": "CLAUDE.md",
                    "recommendation": "在 CLAUDE.md 增加配置读取要求。",
                    "expected_effect": "回答更完整。",
                    "validation": "复测配置类问题。",
                    "risk": "响应耗时可能增加。",
                    "requires_approval": True,
                }
            ],
            "external_guidance": [],
            "no_action_reason": None,
        },
    )
    proposal = store.list_proposals(feedback_case_id=feedback_case["feedback_case_id"])[0]
    store.review_proposal(proposal["proposal_id"], action="approve", comment="确认")
    task = store.create_task(proposal_id=proposal["proposal_id"])
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), store)
    seen: dict[str, object] = {}

    async def fake_query(*, prompt, options, transport=None):
        prompt_items = []
        async for item in prompt:
            prompt_items.append(item)
        prompt_text = prompt_items[0]["message"]["content"]
        input_path = prompt_text.split("输入文件：", 1)[1].splitlines()[0]
        input_payload = json.loads(Path(input_path).read_text(encoding="utf-8"))
        output = {
            "schema_version": "execution-plan-output/v1",
            "optimization_task_id": input_payload["optimization_task_id"],
            "execution_job_id": input_payload["execution_job_id"],
            "status": "ready",
            "baseline_agent_version_id": input_payload["baseline_agent_version_id"],
            "summary": "追加配置读取要求。",
            "operations": [
                {
                    "operation": "append_text",
                    "path": "CLAUDE.md",
                    "append_text": "\n回答配置类问题前必须读取当前 workspace 配置。\n",
                    "rationale": "根据已批准方案补充主智能体配置读取要求。",
                }
            ],
            "validation": "运行评估套件。",
            "risk": "用例格式需人工确认。",
            "human_review_required": True,
        }
        text = json.dumps(output, ensure_ascii=False)
        seen["prompt_text"] = prompt_text
        seen["input_path"] = input_path
        seen["allowed_tools"] = options.allowed_tools
        seen["disallowed_tools"] = options.disallowed_tools
        yield AssistantMessage(content=[TextBlock(text=text)], model="<synthetic>", session_id="sdk-execution-session")
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="sdk-execution-session",
            result=text,
        )

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    monkeypatch.setattr(runtime, "_provider_configured", lambda: True)

    job = asyncio.run(runtime.run_execution_job(task["optimization_task_id"]))
    updated_task = store.find_task(task["optimization_task_id"])

    assert job["status"] == "ready"
    assert seen["input_path"] == job["input_path"]
    assert str(seen["input_path"]).endswith("/execution/input.json")
    assert "execution-input.json" not in str(seen["input_path"])
    assert seen["allowed_tools"] == []
    assert set(seen["disallowed_tools"]) >= {"Read", "Grep", "Glob", "Bash", "Edit", "Write"}
    assert updated_task["latest_execution_job_id"] == job["execution_job_id"]
    assert updated_task["latest_execution_job"]["validated_output_json"]["operations"][0]["path"] == "CLAUDE.md"


def test_execution_optimizer_uses_deterministic_eval_plan_without_agent(tmp_path, monkeypatch):
    import claude_agent_sdk

    store, settings = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_incomplete"], comment="数据不全"))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    store.complete_attribution_job(attribution_job["job_id"], store.offline_attribution_output(attribution_job))
    proposal_job = store.create_proposal_job(feedback_case["feedback_case_id"])
    store.complete_proposal_job(
        proposal_job["job_id"],
        {
            "schema_version": "proposal-output/v1",
            "feedback_case_id": feedback_case["feedback_case_id"],
            "proposal_job_id": proposal_job["job_id"],
            "status": "completed",
            "proposals": [
                {
                    "proposal_id": "prop-exec-eval",
                    "title": "增加告警误报评估用例",
                    "actionability": "direct_workspace_change",
                    "target_type": "eval_case",
                    "target_path": "evals/alert-triage-false-positive.json",
                    "recommendation": "创建告警误报评估用例。",
                    "expected_effect": "覆盖误报回归场景。",
                    "validation": "运行评估套件。",
                    "risk": "用例格式需人工确认。",
                    "requires_approval": True,
                }
            ],
            "external_guidance": [],
            "no_action_reason": None,
        },
    )
    proposal = store.list_proposals(feedback_case_id=feedback_case["feedback_case_id"])[0]
    store.review_proposal(proposal["proposal_id"], action="approve", comment="确认")
    task = store.create_task(proposal_id=proposal["proposal_id"])
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), store)

    async def fail_query(*, prompt, options, transport=None):
        raise AssertionError("eval execution plans should be generated deterministically")
        if False:
            yield None

    monkeypatch.setattr(claude_agent_sdk, "query", fail_query)
    monkeypatch.setattr(runtime, "_provider_configured", lambda: True)

    job = asyncio.run(runtime.run_execution_job(task["optimization_task_id"]))
    operation = job["validated_output_json"]["operations"][0]

    assert job["status"] == "ready"
    assert operation["operation"] == "create_file"
    assert operation["path"] == "evals/alert-triage-false-positive.json"
    assert "feedback-eval-case/v1" in operation["content"]
    assert "创建告警误报评估用例" in operation["content"]
    assert "手动运行回归验证" in job["validated_output_json"]["validation"]


def test_execution_plan_output_normalizes_agent_friendly_fields():
    validated, error = validate_execution_plan_output(
        {
            "schema_version": "execution-plan-output/v1",
            "optimization_task_id": "opt-1",
            "execution_job_id": "fbe-1",
            "status": "safe_to_apply",
            "summary": "创建评估用例。",
            "operations": [
                {
                    "operation": "create_file",
                    "path": "evals/example.json",
                    "content": "{}",
                    "rationale": {"reason": "根据建议创建文件"},
                }
            ],
            "validation": {"steps": ["检查 JSON 语法"], "expected_result": "评估用例可加载"},
            "risk": {"level": "low", "reason": "仅新增评估文件"},
            "human_review_required": True,
        }
    )

    assert error is None
    assert validated["status"] == "ready"
    assert "检查 JSON 语法" in validated["validation"]
    assert "仅新增评估文件" in validated["risk"]
    assert "根据建议创建文件" in validated["operations"][0]["rationale"]


def test_proposal_output_normalizes_compact_agent_proposal(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_incomplete"]))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    store.complete_attribution_job(attribution_job["job_id"], store.offline_attribution_output(attribution_job))
    proposal_job = store.create_proposal_job(feedback_case["feedback_case_id"])

    completed = store.complete_proposal_job(
        proposal_job["job_id"],
        {
            "schema_version": "proposal-output/v1",
            "feedback_case_id": feedback_case["feedback_case_id"],
            "proposal_job_id": proposal_job["job_id"],
            "status": "completed",
            "proposals": [
                {
                    "id": "prop-001",
                    "target_path": "CLAUDE.md",
                    "actionability": "direct_workspace_change",
                    "rationale": "Agent 未验证 workspace 能力清单。",
                    "recommendation": "Add a Workspace Discovery section to CLAUDE.md.",
                }
            ],
            "external_guidance": [],
            "no_action_reason": None,
        },
    )
    output = store.get_job_output(proposal_job["job_id"], "proposal")
    proposals = store.list_proposals(feedback_case_id=feedback_case["feedback_case_id"])

    assert completed["status"] == "completed"
    assert output["proposals"][0]["proposal_id"] == "prop-001"
    assert output["proposals"][0]["target_type"] == "main_agent_claude_md"
    assert output["proposals"][0]["title"] == "Add a Workspace Discovery section to CLAUDE.md."
    assert output["proposals"][0]["expected_effect"]
    assert proposals[0]["target_path"] == "CLAUDE.md"


def test_proposal_output_normalizes_external_guidance_aliases(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_quality"]))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    store.complete_attribution_job(attribution_job["job_id"], store.offline_attribution_output(attribution_job))
    proposal_job = store.create_proposal_job(feedback_case["feedback_case_id"])

    completed = store.complete_proposal_job(
        proposal_job["job_id"],
        {
            "schema_version": "proposal-output/v1",
            "feedback_case_id": feedback_case["feedback_case_id"],
            "proposal_job_id": proposal_job["job_id"],
            "status": "completed",
            "proposals": [
                {
                    "id": "prop-001",
                    "target_path": "CLAUDE.md",
                    "actionability": "direct_workspace_change",
                    "recommendation": "说明实时数据限制。",
                }
            ],
            "external_guidance": [
                {
                    "target": "sec-ops-data MCP service provider",
                    "actionability": "external_guidance",
                    "recommendation": "接入真实告警数据源。",
                    "rationale": "当前工具返回模拟时间戳。",
                }
            ],
            "no_action_reason": None,
        },
    )
    output = store.get_job_output(proposal_job["job_id"], "proposal")
    items = store.list_external_governance_items(feedback_case_id=feedback_case["feedback_case_id"])

    assert completed["status"] == "completed"
    assert len(store.list_proposals(feedback_case_id=feedback_case["feedback_case_id"])) == 1
    assert output["external_guidance"][0]["owner"] == "sec-ops-data MCP service provider"
    assert output["external_guidance"][0]["reason"] == "当前工具返回模拟时间戳。"
    assert items[0]["owner"] == "sec-ops-data MCP service provider"


def test_revalidate_proposal_job_raw_output_persists_legacy_suggestions(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_quality"]))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    store.complete_attribution_job(attribution_job["job_id"], store.offline_attribution_output(attribution_job))
    proposal_job = store.create_proposal_job(feedback_case["feedback_case_id"])
    raw_output = {
        "schema_version": "proposal-output/v1",
        "feedback_case_id": feedback_case["feedback_case_id"],
        "proposal_job_id": proposal_job["job_id"],
        "status": "needs_human_review",
        "proposals": [
            {
                "id": "prop-001",
                "target_path": "CLAUDE.md",
                "actionability": "direct_workspace_change",
                "recommendation": "说明 MCP 数据限制。",
            }
        ],
        "external_guidance": [
            {
                "target": "sec-ops-data MCP service provider",
                "actionability": "external_guidance",
                "recommendation": "接入真实告警数据源。",
                "rationale": "历史 Agent 使用 target/rationale 字段。",
            }
        ],
        "no_action_reason": None,
    }
    store._set_job_json(
        proposal_job["job_id"],
        raw_output_json=raw_output,
        error_json={"error_code": "SCHEMA_VALIDATION_FAILED", "message": "legacy validation failed"},
    )
    store._append_job_update(proposal_job["job_id"], status="needs_human_review")

    revalidated = store.revalidate_proposal_job(proposal_job["job_id"])
    output = store.get_job_output(proposal_job["job_id"], "proposal")
    proposals = store.list_proposals(feedback_case_id=feedback_case["feedback_case_id"])
    items = store.list_external_governance_items(feedback_case_id=feedback_case["feedback_case_id"])

    assert revalidated["status"] == "completed"
    assert revalidated["error_json"] is None
    assert store.find_case(feedback_case["feedback_case_id"])["status"] == "pending_review"
    assert len(proposals) == 1
    assert proposals[0]["proposal_id"] == "prop-001"
    assert output["external_guidance"][0]["owner"] == "sec-ops-data MCP service provider"
    assert items[0]["owner"] == "sec-ops-data MCP service provider"


def test_force_regenerate_supersedes_unused_existing_proposals(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_quality"]))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    store.complete_attribution_job(attribution_job["job_id"], store.offline_attribution_output(attribution_job))
    proposal_job = store.create_proposal_job(feedback_case["feedback_case_id"])
    store.complete_proposal_job(
        proposal_job["job_id"],
        {
            "schema_version": "proposal-output/v1",
            "feedback_case_id": feedback_case["feedback_case_id"],
            "proposal_job_id": proposal_job["job_id"],
            "status": "completed",
            "proposals": [
                {
                    "id": "prop-001",
                    "target_path": "CLAUDE.md",
                    "actionability": "direct_workspace_change",
                    "recommendation": "说明 MCP 数据限制。",
                }
            ],
            "external_guidance": [
                {
                    "owner": "knowledge-base",
                    "actionability": "external_guidance",
                    "recommendation": "补充知识库条目。",
                    "reason": "知识库缺少对应说明。",
                }
            ],
            "no_action_reason": None,
        },
    )

    regenerated = store.create_proposal_job(feedback_case["feedback_case_id"], force=True)
    active_proposals = store.list_proposals(feedback_case_id=feedback_case["feedback_case_id"])
    superseded_proposals = store.list_proposals(feedback_case_id=feedback_case["feedback_case_id"], status="superseded")
    active_external_items = store.list_external_governance_items(feedback_case_id=feedback_case["feedback_case_id"])
    superseded_external_items = store.list_external_governance_items(feedback_case_id=feedback_case["feedback_case_id"], status="superseded")

    assert regenerated["job_id"] != proposal_job["job_id"]
    assert regenerated["status"] == "queued"
    assert active_proposals == []
    assert superseded_proposals[0]["proposal_id"] == "prop-001"
    assert superseded_proposals[0]["superseded_by_job_id"] == regenerated["job_id"]
    assert active_external_items == []
    assert superseded_external_items[0]["owner"] == "knowledge-base"


def test_data_incomplete_bbb_feedback_eval_calls_main_agent_and_records_result(tmp_path, monkeypatch):
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
    import claude_agent_sdk

    settings = _settings(tmp_path)
    store = FeedbackStore(data_dir=settings.data_dir, agent_version_provider=lambda: "main-v-after")
    run_id = "a0fb5319-1752-45eb-972f-0e7edee30e92"
    store.record_run(
        {
            "run_id": run_id,
            "agent_version_id": "main-v-before",
            "session_id": "sess-bbb",
            "message": "请说明当前 workspace 中有哪些 subagents 和 skills。",
            "answer_summary": "当前 workspace 中可用的 subagents 和 skills 如下。",
            "messages": [{"event": "AssistantMessage", "content": [{"text": "当前 workspace 中可用的 subagents 和 skills 如下。"}]}],
            "agent_activity": {"tool_names": [], "tool_calls": [], "tool_results": [], "skill_calls": []},
            "created_at": "2026-05-22T15:44:50+00:00",
            "completed_at": "2026-05-22T15:44:59+00:00",
            "errors": [],
        }
    )
    signal = store.create_signal(
        FeedbackSignalCreateRequest(
            run_id=run_id,
            session_id="sess-bbb",
            labels=["tool_data_incomplete"],
            comment="数据不全BBB",
        )
    )
    feedback_case = store.create_case(source_ids=[signal["signal_id"]], title="数据不全BBB")
    store.create_evidence_package(feedback_case["feedback_case_id"])
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    store.complete_attribution_job(
        attribution_job["job_id"],
        {
            "schema_version": "attribution-output/v1",
            "feedback_case_id": feedback_case["feedback_case_id"],
            "attribution_job_id": attribution_job["job_id"],
            "status": "completed",
            "problem_type": "tool_data_quality",
            "optimization_object_type": "main_agent_claude_md",
            "actionability": "direct_workspace_change",
            "confidence": "high",
            "human_review_required": False,
            "evidence_refs": [{"type": "evidence_file", "id": "tool_calls.json", "reason": "原回答没有工具调用"}],
            "responsibility_boundary": {"owner": "main_agent_workspace", "reason": "需要要求读取配置文件"},
            "rationale": "Agent 回答 workspace 能力清单时没有读取配置。",
            "recommended_next_step": "generate_proposal",
        },
    )
    proposal_job = store.create_proposal_job(feedback_case["feedback_case_id"])
    store.complete_proposal_job(
        proposal_job["job_id"],
        {
            "schema_version": "proposal-output/v1",
            "feedback_case_id": feedback_case["feedback_case_id"],
            "proposal_job_id": proposal_job["job_id"],
            "status": "completed",
            "proposals": [
                {
                    "proposal_id": "prop-bbb",
                    "title": "要求回答 workspace 能力清单前读取配置",
                    "actionability": "direct_workspace_change",
                    "target_type": "main_agent_claude_md",
                    "target_path": "CLAUDE.md",
                    "recommendation": "在 CLAUDE.md 增加 Read/Grep/Glob 核查配置的要求。",
                    "expected_effect": "回答更完整。",
                    "validation": "复测数据不全BBB 原始输入，并确认产生工具调用。",
                    "risk": "响应耗时增加。",
                    "requires_approval": True,
                }
            ],
            "external_guidance": [],
            "no_action_reason": None,
        },
    )
    proposal = store.list_proposals(feedback_case_id=feedback_case["feedback_case_id"])[0]
    store.review_proposal(proposal["proposal_id"], action="approve", comment="确认")
    task = store.create_task(proposal_id=proposal["proposal_id"])
    task = store.mark_task_applied(task["optimization_task_id"], agent_version={"agent_version_id": "main-v-after"})
    sync = store.sync_feedback_eval_cases(feedback_case_id=feedback_case["feedback_case_id"])
    eval_case = sync["eval_cases"][0]
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), store)
    seen: dict[str, object] = {}

    async def fake_query(*, prompt, options, transport=None):
        prompt_items = []
        async for item in prompt:
            prompt_items.append(item)
        seen["prompt"] = prompt_items[0]["message"]["content"]
        yield AssistantMessage(content=[TextBlock(text="我会先读取当前 workspace 配置后再回答。")], model="<synthetic>", session_id="sdk-eval-session")
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="sdk-eval-session",
            result="我会先读取当前 workspace 配置后再回答。",
        )

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    eval_run = asyncio.run(
        runtime.run_feedback_eval(
            eval_case_ids=[eval_case["eval_case_id"]],
            optimization_task_id=task["optimization_task_id"],
            source="manual_task_regression",
        )
    )
    updated_task = store.find_task(task["optimization_task_id"])
    regression_run = store.get_eval_run(eval_run["eval_run_id"])
    eval_agent_run = store.find_run(run_id=regression_run["items"][0]["agent_run_id"])

    assert sync["created"] == 1
    assert "subagents 和 skills" in eval_case["prompt"]
    assert eval_case["checks_json"]["requires_tool_use"] is True
    assert "subagents 和 skills" in str(seen["prompt"])
    assert eval_run["status"] == "completed"
    assert eval_run["result_status"] == "failed"
    assert regression_run["items"][0]["status"] == "failed"
    assert regression_run["items"][0]["check_results"]
    assert updated_task["status"] == "failed"
    assert updated_task["latest_regression_run_id"] == eval_run["eval_run_id"]
    assert eval_agent_run["metadata"]["source"] == "regression_eval"


def test_update_eval_case_directly_overwrites_content(tmp_path):
    store, _ = _store(tmp_path)
    eval_case, _ = _create_eval_case(store)

    updated = store.update_eval_case(
        eval_case["eval_case_id"],
        {
            "prompt": "复测：请列出当前 workspace 的 subagents 和 skills。",
            "expected_behavior": "必须读取配置文件后回答。",
            "checks_json": {"requires_non_empty_answer": True, "requires_tool_use": False},
            "labels": [" tool_data_incomplete ", "tool_data_incomplete", "manual"],
            "status": "archived",
        },
    )

    assert updated is not None
    assert updated["eval_case_id"] == eval_case["eval_case_id"]
    assert updated["prompt"] == "复测：请列出当前 workspace 的 subagents 和 skills。"
    assert updated["expected_behavior"] == "必须读取配置文件后回答。"
    assert updated["checks_json"]["requires_tool_use"] is False
    assert updated["labels"] == ["tool_data_incomplete", "manual"]
    assert updated["status"] == "archived"
    assert store.find_eval_case(eval_case["eval_case_id"])["prompt"] == updated["prompt"]


def test_update_eval_case_rejects_empty_prompt(tmp_path):
    store, _ = _store(tmp_path)
    eval_case, _ = _create_eval_case(store)

    try:
        store.update_eval_case(eval_case["eval_case_id"], {"prompt": "  "})
    except ValueError as exc:
        assert "prompt" in str(exc).lower()
    else:
        raise AssertionError("empty prompt should be rejected")


def test_archived_eval_case_is_not_selected_for_automatic_feedback_eval(tmp_path):
    settings = _settings(tmp_path)
    store = FeedbackStore(data_dir=settings.data_dir, agent_version_provider=lambda: "main-v-after")
    eval_case, _ = _create_eval_case(store)
    store.update_eval_case(eval_case["eval_case_id"], {"status": "archived"})
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), store)

    assert runtime._selected_eval_cases(None) == []  # noqa: SLF001 - regression coverage for active-only eval selection.


def test_runtime_feedback_jobs_use_offline_outputs_without_provider(tmp_path):
    settings = _settings(tmp_path)
    store = FeedbackStore(data_dir=settings.data_dir, agent_version_provider=lambda: "main-v-test")
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["evidence_gap"]))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), store)

    attribution_job = asyncio.run(runtime.run_attribution_job(feedback_case["feedback_case_id"]))
    reused_attribution_job = asyncio.run(runtime.run_attribution_job(feedback_case["feedback_case_id"]))
    proposal_job = asyncio.run(runtime.run_proposal_job(feedback_case["feedback_case_id"]))
    reused_proposal_job = asyncio.run(runtime.run_proposal_job(feedback_case["feedback_case_id"]))

    assert attribution_job["profile_name"] == "attribution-analyzer"
    assert attribution_job["status"] == "completed"
    assert reused_attribution_job["job_id"] == attribution_job["job_id"]
    assert reused_attribution_job["status"] == "completed"
    assert attribution_job["profile_version"]["profile_name"] == "attribution-analyzer"
    assert proposal_job["profile_name"] == "proposal-generator"
    assert proposal_job["status"] == "completed"
    assert reused_proposal_job["job_id"] == proposal_job["job_id"]
    assert reused_proposal_job["status"] == "completed"


def test_data_incomplete_bbb_case_calls_attribution_agent_and_generates_output(tmp_path, monkeypatch):
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
    import claude_agent_sdk

    settings = _settings(tmp_path)
    store = FeedbackStore(data_dir=settings.data_dir, agent_version_provider=lambda: "main-v-test")
    run_id = "a0fb5319-1752-45eb-972f-0e7edee30e92"
    session_id = "sess_74a6b45e-4883-45cd-9fae-0c5323ddbcd2"
    store.record_run(
        {
            "run_id": run_id,
            "agent_version_id": "agent-version-20260522T104329Z-628569dc",
            "session_id": session_id,
            "sdk_session_id": "38b2b5ae-5c40-42a7-9dcb-4ded2192f323",
            "message": "请说明当前 workspace 中有哪些 subagents 和 skills。",
            "answer_summary": "当前 workspace 中可用的 subagents 和 skills 如下。",
            "messages": [{"event": "AssistantMessage", "content": [{"text": "当前 workspace 中可用的 subagents 和 skills 如下。"}]}],
            "agent_activity": {
                "requested_skills": [],
                "skills_mode": "default",
                "allowed_tools": ["Read", "Grep", "Glob", "mcp__sec-ops-data__*"],
                "disallowed_tools": ["Bash", "WebFetch", "WebSearch"],
                "tool_names": [],
                "tool_calls": [],
                "tool_results": [],
                "skill_calls": [],
            },
            "langfuse_trace_id": "97eb6e0f1dd8b91a6956f4572f90b7f8",
            "langfuse_trace_url": "http://langfuse.local/project/traces/97eb6e0f1dd8b91a6956f4572f90b7f8",
            "created_at": "2026-05-22T15:44:50+00:00",
            "completed_at": "2026-05-22T15:44:59+00:00",
            "errors": [],
        }
    )
    signal = store.create_signal(
        FeedbackSignalCreateRequest(
            run_id=run_id,
            session_id=session_id,
            labels=["tool_data_incomplete"],
            comment="数据不全BBB",
            metadata={"analyst_action": "partially_accepted", "affected_tools": []},
        )
    )
    feedback_case = store.create_case(source_ids=[signal["signal_id"]], title="数据不全BBB")
    evidence = store.create_evidence_package(feedback_case["feedback_case_id"])
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), store)
    seen: dict[str, object] = {}

    async def fake_query(*, prompt, options, transport=None):
        prompt_items = []
        async for item in prompt:
            prompt_items.append(item)
        prompt_text = prompt_items[0]["message"]["content"]
        input_path = prompt_text.split("输入文件：", 1)[1].splitlines()[0]
        input_payload = json.loads(Path(input_path).read_text(encoding="utf-8"))
        output = {
            "schema_version": "attribution-output/v1",
            "feedback_case_id": input_payload["feedback_case_id"],
            "attribution_job_id": input_payload["job_id"],
            "status": "needs_human_review",
            "problem_type": "tool_usage_deficiency",
            "optimization_object_type": "agent_behavior",
            "actionability": "low",
            "confidence": "low",
            "human_review_required": True,
            "evidence_refs": input_payload["allowed_evidence_paths"],
            "responsibility_boundary": "agent",
            "rationale": "该 run 有 messages 和 trace summary，但 tool_calls.json 为空；归因为工具证据链不足。",
            "recommended_next_step": "Human reviewer should examine whether the agent should have used tools before answering capability queries.",
        }
        seen["prompt_text"] = prompt_text
        seen["input_path"] = input_path
        seen["cwd"] = options.cwd
        seen["max_turns"] = options.max_turns
        text = json.dumps(output, ensure_ascii=False)
        yield AssistantMessage(content=[TextBlock(text=text)], model="<synthetic>", session_id="sdk-attribution-session")
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=2,
            session_id="sdk-attribution-session",
            result=text,
        )

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    monkeypatch.setattr(runtime, "_provider_configured", lambda: True)

    attribution_job = asyncio.run(runtime.run_attribution_job(feedback_case["feedback_case_id"]))
    output = store.get_job_output(attribution_job["job_id"], "attribution")

    assert evidence["completeness"]["has_runs"] is True
    assert evidence["completeness"]["has_tool_calls"] is False
    assert store.get_evidence_package_file(evidence["evidence_package_id"], "tool_calls.json")["content"] == []
    assert "归因分析智能体" in str(seen["prompt_text"])
    assert seen["cwd"] == settings.attribution_analyzer_workspace_dir
    assert seen["max_turns"] == settings.max_turns
    assert attribution_job["status"] == "completed"
    assert output["schema_version"] == "attribution-output/v1"
    assert output["feedback_case_id"] == feedback_case["feedback_case_id"]
    assert output["problem_type"] == "tool_data_quality"
    assert output["optimization_object_type"] == "main_agent_claude_md"
    assert output["actionability"] == "needs_human_analysis"
    assert output["evidence_refs"][0]["type"] == "evidence_file"
    assert output["responsibility_boundary"]["owner"] == "agent"
    assert store.find_case(feedback_case["feedback_case_id"])["status"] == "pending_proposal"


def test_attribution_agent_fragment_output_is_formatted_before_validation(tmp_path, monkeypatch):
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
    import claude_agent_sdk

    from app.runtime.output_formatter import OutputFormatterResult

    settings = _settings(tmp_path)
    store = FeedbackStore(data_dir=settings.data_dir, agent_version_provider=lambda: "main-v-test")
    _record_run(store)
    signal = store.create_signal(
        FeedbackSignalCreateRequest(
            run_id="run-1",
            labels=["verdict_mismatch"],
            comment="告警结论错误，应该是误报",
        )
    )
    feedback_case = store.create_case(source_ids=[signal["signal_id"]], title="告警结论错误，应该是误报")
    store.create_evidence_package(feedback_case["feedback_case_id"])
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), store)
    seen: dict[str, object] = {}

    async def fake_query(*, prompt, options, transport=None):
        text = json.dumps(
            {
                "type": "evidence_file",
                "id": "feedback.json",
                "reason": "分析师明确反馈告警结论错误，应该是误报。",
            },
            ensure_ascii=False,
        )
        yield AssistantMessage(content=[TextBlock(text=text)], model="<synthetic>", session_id="sdk-attribution-session")
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="sdk-attribution-session",
            result=text,
        )

    class FakeFormatter:
        def format(self, *, job_type, raw_text, job_input, expected_schema_version):
            seen["job_type"] = job_type
            seen["raw_text"] = raw_text
            seen["job_input"] = job_input
            payload = {
                "schema_version": "attribution-output/v1",
                "feedback_case_id": job_input["feedback_case_id"],
                "attribution_job_id": job_input["job_id"],
                "status": "needs_human_review",
                "problem_type": "insufficient_information",
                "optimization_object_type": "not_actionable",
                "actionability": "needs_human_analysis",
                "confidence": "medium",
                "human_review_required": True,
                "evidence_refs": [{"type": "evidence_file", "id": "feedback.json", "reason": "反馈指出原告警结论错误。"}],
                "responsibility_boundary": {"owner": "needs_human_analysis", "reason": "原始输出只有证据片段，需要人工确认真实责任边界。"},
                "rationale": "归因分析智能体只输出了证据片段，格式化器保守转为需人工复核。",
                "recommended_next_step": "needs_human_review",
                "_formatter": {"name": "fake-dspy"},
            }
            return OutputFormatterResult(payload=payload, source="fake")

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    monkeypatch.setattr(runtime, "_provider_configured", lambda: True)
    runtime.output_formatter = FakeFormatter()

    attribution_job = asyncio.run(runtime.run_attribution_job(feedback_case["feedback_case_id"]))
    output = store.get_job_output(attribution_job["job_id"], "attribution")

    assert seen["job_type"] == "attribution"
    assert "feedback.json" in str(seen["raw_text"])
    assert attribution_job["status"] == "completed"
    assert attribution_job["raw_output_json"]["_formatter"]["name"] == "fake-dspy"
    assert output["schema_version"] == "attribution-output/v1"
    assert output["recommended_next_step"] == "needs_human_review"
    assert store.find_case(feedback_case["feedback_case_id"])["status"] == "pending_proposal"


def test_proposal_agent_ignores_intermediate_permissions_json(tmp_path, monkeypatch):
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
    import claude_agent_sdk

    settings = _settings(tmp_path)
    store = FeedbackStore(data_dir=settings.data_dir, agent_version_provider=lambda: "main-v-test")
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_incomplete"], comment="数据不全BBB"))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]], title="数据不全BBB")
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    store.complete_attribution_job(
        attribution_job["job_id"],
        {
            "schema_version": "attribution-output/v1",
            "feedback_case_id": feedback_case["feedback_case_id"],
            "attribution_job_id": attribution_job["job_id"],
            "status": "completed",
            "problem_type": "tool_misuse",
            "optimization_object_type": "main_agent_claude_md",
            "actionability": "direct_workspace_change",
            "confidence": "high",
            "human_review_required": False,
            "evidence_refs": [{"type": "evidence_file", "id": "agent_activity.json", "reason": "未调用工具"}],
            "responsibility_boundary": {"owner": "main_agent_workspace", "reason": "需要补充行为准则"},
            "rationale": "Agent 未验证 workspace 能力清单。",
            "recommended_next_step": "generate_proposal",
        },
    )
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), store)
    seen: dict[str, object] = {}

    async def fake_query(*, prompt, options, transport=None):
        prompt_items = []
        async for item in prompt:
            prompt_items.append(item)
        prompt_text = prompt_items[0]["message"]["content"]
        input_payload = json.loads(prompt_text.split("proposal_input_json:\n", 1)[1].split("\n\nattribution_output_json:", 1)[0])
        output = {
            "schema_version": "proposal-output/v1",
            "feedback_case_id": input_payload["feedback_case_id"],
            "proposal_job_id": input_payload["job_id"],
            "status": "completed",
            "proposals": [],
            "external_guidance": [],
            "no_action_reason": "当前归因需要先由人确认具体缺失项。",
        }
        text = '{"permissions":{"allow":["Bash(npm *)"]}}\n' + json.dumps(output, ensure_ascii=False)
        seen["prompt_text"] = prompt_text
        seen["allowed_tools"] = options.allowed_tools
        seen["disallowed_tools"] = options.disallowed_tools
        yield AssistantMessage(content=[TextBlock(text=text)], model="<synthetic>", session_id="sdk-proposal-session")
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=2,
            session_id="sdk-proposal-session",
            result=text,
        )

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    monkeypatch.setattr(runtime, "_provider_configured", lambda: True)

    proposal_job = asyncio.run(runtime.run_proposal_job(feedback_case["feedback_case_id"]))
    output = store.get_job_output(proposal_job["job_id"], "proposal")

    assert "proposal_input_json" in str(seen["prompt_text"])
    assert "attribution_output_json" in str(seen["prompt_text"])
    assert seen["allowed_tools"] == []
    assert set(seen["disallowed_tools"]) >= {"Read", "Grep", "Glob"}
    assert proposal_job["status"] == "completed"
    assert output["schema_version"] == "proposal-output/v1"
    assert proposal_job["raw_output_json"]["schema_version"] == "proposal-output/v1"


def test_sqlite_store_does_not_create_legacy_runtime_dirs(tmp_path):
    store, settings = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["evidence_gap"]))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    evidence = store.create_evidence_package(feedback_case["feedback_case_id"])
    job = store.create_attribution_job(feedback_case["feedback_case_id"])
    store.complete_attribution_job(job["job_id"], store.offline_attribution_output(job))

    assert settings.runtime_db_path.exists()
    assert store.get_evidence_package_file(evidence["evidence_package_id"], "feedback.json")["content"]
    assert not (settings.data_dir / "feedback-cases").exists()
    assert not (settings.data_dir / "feedback-analysis").exists()
    assert not (settings.data_dir / "evidence-packages").exists()
    assert not (settings.data_dir / ".runtime-tmp" / "jobs" / job["job_id"]).exists()
