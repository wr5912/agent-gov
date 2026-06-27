import asyncio
import json
from pathlib import Path

import pytest
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.feedback_schemas import (
    validate_execution_plan_output,
    validate_feedback_optimization_plan_output,
)
from app.runtime.response_schemas.agent_job_response_schemas import AgentJobResponse
from app.runtime.schemas import (
    FeedbackOptimizationBatchPlanGenerateRequest,
    FeedbackSignalCreateRequest,
    SocEventIngestRequest,
)
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import AppSettings
from app.runtime.stores.feedback_store import FeedbackStore
from pydantic import ValidationError


def _settings(tmp_path):
    governor_workspace = tmp_path / "docker" / "volume" / "governor-workspace"
    data = tmp_path / "docker" / "volume" / "data"
    governor_root = tmp_path / "docker" / "volume" / "claude-roots" / "governor"
    for path in (governor_workspace, governor_root / ".claude"):
        path.mkdir(parents=True, exist_ok=True)
    settings = AppSettings(
        _env_file=None,
        GOVERNOR_WORKSPACE_DIR=governor_workspace,
        DATA_DIR=data,
        GOVERNOR_CLAUDE_ROOT=governor_root,
        MODEL_PROVIDER_API_KEY="sk-test-provider",
    )
    # main 已并入业务模型：在派生的 main-agent workspace（/data 下）写入起始受管文件，
    # 执行/证据测试针对这些文件。
    workspace = settings.main_workspace_dir
    workspace.mkdir(parents=True, exist_ok=True)
    (settings.main_claude_root / ".claude").mkdir(parents=True, exist_ok=True)
    (workspace / "CLAUDE.md").write_text("# Test Agent\n", encoding="utf-8")
    (workspace / ".mcp.json").write_text("{}\n", encoding="utf-8")
    return settings


def _store(tmp_path):
    settings = _settings(tmp_path)
    return FeedbackStore(data_dir=settings.data_dir, agent_version_provider=lambda _aid=None: "main-v-test"), settings


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
    job = store.sync_feedback_eval_cases(feedback_case_id=feedback_case["feedback_case_id"])
    eval_case = _complete_eval_case_generation_job(store, job, feedback_case=feedback_case)
    return eval_case, feedback_case


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


def _attribution_output(job: dict, **overrides):
    output = {
        "feedback_case_id": job["feedback_case_id"],
        "attribution_job_id": job["job_id"],
        "status": "completed",
        "problem_type": "tool_data_quality",
        "optimization_object_type": "main_agent_claude_md",
        "actionability": "direct_workspace_change",
        "confidence": "high",
        "human_review_required": False,
        "evidence_refs": [{"type": "evidence_file", "id": "feedback.json", "reason": "测试反馈指出需要优化。"}],
        "responsibility_boundary": {"owner": "main_agent_workspace", "reason": "测试归因指向主智能体 workspace。"},
        "rationale": "测试用结构化归因输出。",
        "recommended_next_step": "generate_proposal",
    }
    output.update(overrides)
    return output


def _batch_plan_output(job: dict, **overrides):
    input_json = job.get("input_json") if isinstance(job.get("input_json"), dict) else {}
    output = {
        "batch_id": input_json.get("batch_id") or "",
        "status": "needs_human_review",
        "title": "测试批次优化方案",
        "summary": "测试用批次优化方案。",
        "problem_types": [],
        "confidence": "medium",
        "actionability": "needs_human_analysis",
        "target_type": "not_actionable",
        "target_path": None,
        "recommendation": "测试场景不生成可执行任务。",
        "expected_effect": "用于验证批次 plan 持久化逻辑。",
        "validation": "测试断言通过。",
        "risk": "无生产风险。",
        "source_refs": input_json.get("source_refs") or [],
        "feedback_case_ids": input_json.get("feedback_case_ids") or [],
        "eval_case_ids": input_json.get("eval_case_ids") or [],
        "attribution_job_ids": input_json.get("attribution_job_ids") or [],
        "attribution_summaries": [],
        "rationale": "测试用结构化批次方案输出。",
        "evidence_refs": [],
        "tasks": [],
        "blocked_items": [
            {
                "title": "测试阻断项",
                "target_type": "not_actionable",
                "actionability": "needs_human_analysis",
                "reason": "测试场景不生成可执行任务。",
                "feedback_case_ids": input_json.get("feedback_case_ids") or [],
                "eval_case_ids": input_json.get("eval_case_ids") or [],
                "attribution_job_ids": input_json.get("attribution_job_ids") or [],
            }
        ],
    }
    output.update(overrides)
    return output


def _eval_case_generation_output(job: dict, feedback_case: dict, **overrides):
    input_json = job.get("input_json") if isinstance(job.get("input_json"), dict) else {}
    source_run = {}
    for item in input_json.get("feedback_cases") or []:
        if item.get("feedback_case", {}).get("feedback_case_id") == feedback_case["feedback_case_id"]:
            source_run = item.get("source_run") or {}
            break
    prompt = source_run.get("message") or "复测原始反馈场景。"
    output = {
        "job_id": job["job_id"],
        "scope_kind": job.get("scope_kind"),
        "scope_id": job.get("scope_id"),
        "status": "completed",
        "eval_cases": [
            {
                "schema_version": "feedback-eval-case/v1",
                "status": "draft",
                "source": "eval_case_governor",
                "source_feedback_case_id": feedback_case["feedback_case_id"],
                "source_run_id": source_run.get("run_id"),
                "source_kind": "feedback_case",
                "source_id": feedback_case["feedback_case_id"],
                "source_refs": [{"source_kind": "feedback_case", "source_id": feedback_case["feedback_case_id"]}],
                "asset_layer": "candidate",
                "promotion_status": "candidate",
                "blocking_policy": "non_blocking",
                "flaky_status": "stable",
                "variant_role": "original_reproduction",
                "prompt": prompt,
                "expected_behavior": "回答前读取当前 workspace 配置，并基于最新配置给出完整结论。",
                "checks_json": {
                    "requires_non_empty_answer": True,
                    "requires_no_runtime_errors": True,
                    "requires_tool_use": True,
                },
                "labels": ["feedback_optimization", "tool_data_incomplete"],
            }
        ],
        "results": [],
    }
    output.update(overrides)
    return output


def _complete_eval_case_generation_job(store: FeedbackStore, job: dict, *, feedback_case: dict, **overrides):
    completed = store.complete_projected_agent_job(job, _eval_case_generation_output(job, feedback_case, **overrides))
    return completed["validated_output_json"]["eval_cases"][0]


def _create_approved_task_for_target(
    store: FeedbackStore,
    target_path: str,
    *,
    target_type: str = "workspace_file",
    title: str | None = None,
    recommendation: str | None = None,
):
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_incomplete"]))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    store.complete_attribution_job(attribution_job["job_id"], _attribution_output(attribution_job))
    batch = store.ensure_single_case_optimization_batch(feedback_case["feedback_case_id"])
    plan_job = store.create_batch_plan_job(batch["batch_id"])
    completed = store.complete_batch_plan_job(
        plan_job["job_id"],
        _batch_plan_output(
            plan_job,
            status="pending_execution",
            actionability="direct_workspace_change",
            target_type=target_type,
            target_path=target_path,
            recommendation=recommendation or f"按反馈调整 {target_path}。",
            expected_effect="提高反馈场景表现。",
            validation="复测反馈场景。",
            risk="需确认文件内容变更符合预期。",
            tasks=[
                {
                    "execution_kind": "workspace_execution",
                    "status": "pending_execution",
                    "title": title or f"修改 {target_path}",
                    "description": recommendation or f"按反馈调整 {target_path}。",
                    "objective": "提高反馈场景表现。",
                    "target_summary": target_path,
                    "target_type": target_type,
                    "target_path": target_path,
                    "owner": "main_agent_workspace",
                    "actionability": "direct_workspace_change",
                    "recommendation": recommendation or f"按反馈调整 {target_path}。",
                    "recommended_actions": [f"修改 {target_path}"],
                    "acceptance_criteria": ["复测反馈场景通过"],
                    "expected_effect": "提高反馈场景表现。",
                    "validation": "复测反馈场景。",
                    "risk": "需确认文件内容变更符合预期。",
                    "feedback_case_ids": [feedback_case["feedback_case_id"]],
                    "eval_case_ids": [],
                    "attribution_job_ids": [attribution_job["job_id"]],
                    "task_context": {"target_file": target_path},
                }
            ],
            blocked_items=[],
        ),
    )
    plan = completed["validated_output_json"]
    plan_task = plan["tasks"][0]
    prepared = store.prepare_batch_plan_task_execution(batch["batch_id"], plan_task["plan_task_id"])
    return prepared["optimization_task"]


__all__ = [
    "asyncio",
    "json",
    "Path",
    "pytest",
    "ValidationError",
    "ClaudeRuntime",
    "AgentJobResponse",
    "FeedbackOptimizationBatchPlanGenerateRequest",
    "FeedbackSignalCreateRequest",
    "FeedbackStore",
    "LocalSessionStore",
    "SocEventIngestRequest",
    "validate_execution_plan_output",
    "validate_feedback_optimization_plan_output",
    "_create_approved_task_for_target",
    "_attribution_output",
    "_batch_plan_output",
    "_complete_eval_case_generation_job",
    "_create_batch_with_completed_attribution",
    "_create_eval_case",
    "_eval_case_generation_output",
    "_record_run",
    "_settings",
    "_store",
]
