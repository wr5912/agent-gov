import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.feedback_analysis_response_schemas import FeedbackAnalysisJobResponse
from app.runtime.feedback_schemas import validate_execution_plan_output, validate_feedback_optimization_plan_output
from app.runtime.feedback_store import FeedbackStore
from app.runtime.schemas import (
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

__all__ = [name for name in globals() if not name.startswith('__')]
