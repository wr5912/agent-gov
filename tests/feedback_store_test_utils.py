from __future__ import annotations

import pytest
from app.runtime.runtime_db import TestDatasetCaseModel, TestDatasetModel, utc_now
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
    )
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


def _seed_test_dataset(
    store: FeedbackStore,
    *,
    agent_id: str,
    dataset_id: str,
    candidate_agent_version_id: str | None = None,
    source_improvement_id: str | None = None,
    source_execution_id: str | None = None,
) -> str:
    now = utc_now()
    with store.Session.begin() as db:
        db.add(
            TestDatasetModel(
                dataset_id=dataset_id,
                agent_id=agent_id,
                owner_kind="business_agent",
                owner_id=agent_id,
                source_improvement_id=source_improvement_id or f"fixture-{dataset_id}",
                name=f"Fixture {dataset_id}",
                description="",
                scope="test",
                revision=1,
                lifecycle_state="active",
                source_regression_assessment_id=f"reg-{dataset_id}",
                source_regression_assessment_updated_at=now,
                source_normalized_feedback_id=f"nf-{dataset_id}",
                source_normalized_feedback_updated_at=now,
                source_attribution_id=f"attr-{dataset_id}",
                source_attribution_updated_at=now,
                source_optimization_plan_id=f"opt-{dataset_id}",
                source_optimization_plan_updated_at=now,
                source_execution_id=source_execution_id or f"exec-{dataset_id}",
                source_execution_updated_at=now,
                candidate_agent_version_id=candidate_agent_version_id or f"candidate-{dataset_id}",
                source_feedback_ids_json=[],
                quality_tags_json=[],
                created_at=now,
                updated_at=now,
            )
        )
        db.flush()
        db.add(
            TestDatasetCaseModel(
                case_id=f"tdc-{dataset_id}",
                dataset_id=dataset_id,
                position=1,
                prompt="验证 typed dataset 执行路径",
                expected_behavior="返回非空且无运行错误的结果",
                checkpoints_json=["输出非空"],
            )
        )
    return dataset_id


__all__ = [
    "FeedbackSignalCreateRequest",
    "FeedbackStore",
    "SocEventIngestRequest",
    "_record_run",
    "_seed_test_dataset",
    "_settings",
    "_store",
    "pytest",
]
