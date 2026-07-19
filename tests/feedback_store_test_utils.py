from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest
from app.runtime.improvement_db import ExecutionRecordModel
from app.runtime.runtime_db import utc_now
from app.runtime.schemas import FeedbackSignalCreateRequest, SocEventIngestRequest
from app.runtime.settings import AppSettings
from app.runtime.stores.feedback_store import FeedbackStore
from app.runtime.stores.improvement_store import advance_improvement_stage_in_transaction

from business_agent_test_utils import create_test_business_agent_workspace


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
    workspace = settings.default_workspace_dir
    workspace.mkdir(parents=True, exist_ok=True)
    (settings.default_claude_root / ".claude").mkdir(parents=True, exist_ok=True)
    create_test_business_agent_workspace(workspace, agent_id="main-agent", name="Test Agent")
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
            "agent_id": "main-agent",
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


def _seed_execution_record(
    content: Any,
    improvement_id: str,
    *,
    summary: str,
    changes_applied: list[str] | None = None,
    agent_version: str = "",
    generated_by: str = "heuristic",
    change_set_id: str = "",
    applied_agent_version_id: str = "",
    applied_diff: dict | None = None,
    risk_level: str = "",
    rollback_strategy: str = "",
    rollback_instructions: list[str] | None = None,
    generation_trace_id: str = "",
    generation_trace_url: str = "",
    advance_to_stage: str | None = None,
) -> Any:
    """Seed an execution artifact without exposing a production bypass API."""

    now = utc_now()
    with content._session_factory.begin() as db:
        row = db.query(ExecutionRecordModel).filter_by(improvement_id=improvement_id).one_or_none()
        if row is None:
            row = ExecutionRecordModel(
                execution_id=f"exec-test-{uuid4().hex[:12]}",
                improvement_id=improvement_id,
                created_at=now,
            )
            db.add(row)
        row.summary = summary
        row.changes_applied_json = list(changes_applied or [])
        row.agent_version = agent_version
        row.status = "draft"
        row.generated_by = generated_by
        row.change_set_id = change_set_id
        row.applied_agent_version_id = applied_agent_version_id
        row.applied_diff_json = dict(applied_diff or {})
        row.risk_level = risk_level
        row.rollback_strategy = rollback_strategy
        row.rollback_instructions_json = list(rollback_instructions or [])
        row.generation_trace_id = generation_trace_id
        row.generation_trace_url = generation_trace_url
        row.base_commit_sha = ""
        row.source_optimization_plan_id = ""
        row.source_optimization_plan_updated_at = ""
        row.source_attribution_id = ""
        row.source_attribution_updated_at = ""
        row.claim_token = ""
        row.claim_expires_at = ""
        row.updated_at = now
        db.flush()
        if advance_to_stage:
            advance_improvement_stage_in_transaction(db, improvement_id, stage=advance_to_stage)
    record = content.get_execution(improvement_id)
    assert record is not None
    return record


__all__ = [
    "FeedbackSignalCreateRequest",
    "FeedbackStore",
    "SocEventIngestRequest",
    "_record_run",
    "_seed_execution_record",
    "_settings",
    "_store",
    "pytest",
]
