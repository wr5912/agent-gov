from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import uuid4

import pytest
from app.runtime.improvement_db import ExecutionRecordModel
from app.runtime.protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from app.runtime.runtime_db import utc_now
from app.runtime.schemas import FeedbackSignalCreateRequest, SocEventIngestRequest
from app.runtime.settings import AppSettings
from app.runtime.stores.feedback_store import FeedbackStore
from app.runtime.stores.improvement_store import advance_improvement_stage_in_transaction

from business_agent_test_utils import create_test_business_agent_workspace


def _settings(tmp_path):
    governor_workspace = tmp_path / "docker" / "volume" / "governor-workspace"
    data = tmp_path / "docker" / "volume" / "data"
    governor_workspace.mkdir(parents=True, exist_ok=True)
    settings = AppSettings(
        _env_file=None,
        GOVERNOR_WORKSPACE_DIR=governor_workspace,
        DATA_DIR=data,
        RUNTIME_VOLUME_MODE="local-debug",
    )
    workspace = settings.default_workspace_dir
    workspace.mkdir(parents=True, exist_ok=True)
    create_test_business_agent_workspace(
        workspace,
        agent_id=DEFAULT_BUSINESS_AGENT_ID,
        name="Security Operations Expert",
    )
    (workspace / "AGENT.md").write_text("# Test Agent\n", encoding="utf-8")
    mcp_dir = workspace / "mcp"
    mcp_dir.mkdir(parents=True, exist_ok=True)
    (mcp_dir / "sec-ops.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "sec-ops",
                "credential_refs": [
                    {"env": "SEC_OPS_MCP_URL", "path": "mcp_config.url"},
                ],
                "mcp_config": {
                    "type": "http_mcp",
                    "url": "${SEC_OPS_MCP_URL}",
                },
                "enable_tools": [],
                "enable_resources": [],
                "enable_resource_templates": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return settings


def _store(tmp_path):
    settings = _settings(tmp_path)
    return FeedbackStore(data_dir=settings.data_dir), settings


def _run_payload(
    *,
    run_id: str = "run-1",
    agent_id: str = DEFAULT_BUSINESS_AGENT_ID,
    session_id: str | None = None,
    created_at: str = "2026-05-20T00:00:00+00:00",
    **overrides: Any,
) -> dict[str, Any]:
    """Build one complete AgentScope-era AgentGov run projection."""

    trace_id = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:32]
    payload: dict[str, Any] = {
        "run_id": run_id,
        "agent_id": agent_id,
        "session_id": session_id or f"session-{run_id}",
        "agent_version_id": f"version-{agent_id}",
        "runtime_agent_id": f"runtime-{agent_id}",
        "harness_digest": "a" * 64,
        "status": "succeeded",
        "reply_ids": [f"reply-{run_id}"],
        "trace_id": trace_id,
        "trace_url": f"http://langfuse.local/project/traces/{trace_id}",
        "trace_status": "complete",
        "metadata": {},
        "created_at": created_at,
        "started_at": created_at,
        "updated_at": created_at,
        "completed_at": created_at,
    }
    payload.update(overrides)
    return payload


def _record_run(store: FeedbackStore):
    return store.record_run(
        _run_payload(
            session_id="session-1",
            alert_id="alert-1",
            case_id="case-1",
            completed_at="2026-05-20T00:00:01+00:00",
            updated_at="2026-05-20T00:00:01+00:00",
        )
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
    "_run_payload",
    "_seed_execution_record",
    "_settings",
    "_store",
    "pytest",
]
