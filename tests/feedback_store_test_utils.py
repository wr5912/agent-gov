from __future__ import annotations

import json

import pytest
from app.runtime.business_agent_workspace import seed_business_agent_workspace
from app.runtime.schemas import FeedbackSignalCreateRequest, SocEventIngestRequest
from app.runtime.settings import AppSettings
from app.runtime.stores.feedback_store import FeedbackStore


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
        RUNTIME_VOLUME_MODE="local-debug",
    )
    workspace = settings.main_workspace_dir
    workspace.mkdir(parents=True, exist_ok=True)
    (settings.main_claude_root / ".claude").mkdir(parents=True, exist_ok=True)
    seed_business_agent_workspace(workspace, agent_id="main-agent", name="Test Agent")
    (workspace / "CLAUDE.md").write_text("# Test Agent\n", encoding="utf-8")
    (workspace / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "sec-ops-data": {
                        "type": "http",
                        "url": "http://localhost:58001/mcp",
                    }
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
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


def _eval_case_generation_output(job: dict, feedback_case: dict, **overrides):
    input_json = job.get("input_json") if isinstance(job.get("input_json"), dict) else {}
    source_run = {}
    for item in input_json.get("feedback_cases") or []:
        if item.get("feedback_case", {}).get("feedback_case_id") == feedback_case["feedback_case_id"]:
            source_run = item.get("source_run") or {}
            break
    output = {
        "job_id": job["job_id"],
        "scope_kind": job.get("scope_kind"),
        "scope_id": job.get("scope_id"),
        "status": "completed",
        "eval_cases": [
            {
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
                "prompt": source_run.get("message") or "复测原始反馈场景。",
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


__all__ = [
    "FeedbackSignalCreateRequest",
    "FeedbackStore",
    "SocEventIngestRequest",
    "_complete_eval_case_generation_job",
    "_eval_case_generation_output",
    "_record_run",
    "_settings",
    "_store",
    "pytest",
]
