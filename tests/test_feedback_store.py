import asyncio

from app.runtime.claude_runtime import ClaudeRuntime, ensure_langfuse_otel_compat
from app.runtime.feedback_store import FeedbackStore
from app.runtime.schemas import ChatRequest, FeedbackCreateRequest, FeedbackEventIngestRequest
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import AppSettings


def _settings(tmp_path):
    workspace = tmp_path / "docker" / "volume" / "workspace"
    data = tmp_path / "docker" / "volume" / "data"
    claude_root = tmp_path / "docker" / "volume" / "claude-root"
    claude_home = claude_root / ".claude"
    workspace.mkdir(parents=True, exist_ok=True)
    claude_home.mkdir(parents=True, exist_ok=True)
    return AppSettings(
        _env_file=None,
        WORKSPACE_DIR=workspace,
        DATA_DIR=data,
        CLAUDE_ROOT=claude_root,
        CLAUDE_HOME=claude_home,
        ENABLE_POLICY_HOOKS=True,
    )


def _store(settings):
    return FeedbackStore(settings.feedback_dir, settings.optimization_proposals_dir)


def test_feedback_store_creates_attribution_and_proposal(tmp_path):
    settings = _settings(tmp_path)
    store = _store(settings)
    store.record_run(
        {
            "run_id": "run-1",
            "session_id": "session-1",
            "alert_id": "alert-1",
            "case_id": None,
            "answer_summary": "告警研判摘要",
            "agent_activity": {"tool_calls": [{"name": "mcp__sec-ops-data__asset"}]},
            "created_at": "2026-05-20T00:00:00+00:00",
            "completed_at": "2026-05-20T00:00:01+00:00",
        }
    )

    result = store.create_feedback(
        FeedbackCreateRequest(
            run_id="run-1",
            session_id="session-1",
            alert_id="alert-1",
            feedback_source="explicit",
            analyst_action="rejected",
            labels=["tool_false_positive", "evidence_insufficient"],
            affected_tools=["mcp__sec-ops-data__asset"],
        )
    )

    assert result["attribution"]["attribution_type"] == "tool_quality_gap"
    assert result["proposal"]["status"] == "pending_review"
    assert "tool-registry" in result["proposal"]["target_path"]
    queried = store.query(run_id="run-1")
    assert len(queried["feedback"]) == 1
    assert len(queried["attributions"]) == 1


def test_feedback_without_alert_or_case_does_not_create_proposal(tmp_path):
    settings = _settings(tmp_path)
    store = _store(settings)
    store.record_run(
        {
            "run_id": "run-2",
            "session_id": "session-2",
            "answer_summary": "无告警上下文",
            "agent_activity": {},
            "created_at": "2026-05-20T00:00:00+00:00",
            "completed_at": "2026-05-20T00:00:01+00:00",
        }
    )

    result = store.create_feedback(
        FeedbackCreateRequest(
            run_id="run-2",
            session_id="session-2",
            feedback_source="explicit",
            analyst_action="rejected",
            labels=["evidence_insufficient"],
        )
    )

    assert result["attribution"]["attribution_type"] == "evidence_gap"
    assert result["proposal"] is None
    assert store.list_proposals() == []


def test_feedback_event_matching_pending_and_idempotency(tmp_path):
    settings = _settings(tmp_path)
    store = _store(settings)
    store.record_run(
        {
            "run_id": "run-3",
            "session_id": "session-3",
            "alert_id": "alert-3",
            "answer_summary": "风险等级高危",
            "agent_activity": {},
            "created_at": "2026-05-20T00:00:00+00:00",
            "completed_at": "2026-05-20T00:00:01+00:00",
        }
    )

    event = FeedbackEventIngestRequest(
        event_id="event-1",
        source_system="soc-ui",
        event_type="case.severity_changed",
        timestamp="2026-05-20T00:01:00+00:00",
        alert_id="alert-3",
        after={"severity": "medium"},
    )
    first = store.ingest_event(event)
    duplicate = store.ingest_event(event)
    pending = store.ingest_event(
        FeedbackEventIngestRequest(
            event_id="event-2",
            source_system="soc-ui",
            event_type="recommendation.rejected",
            timestamp="2026-05-20T00:02:00+00:00",
            alert_id="missing-alert",
        )
    )

    assert first["correlation_status"] == "matched"
    assert first["matched_run_id"] == "run-3"
    assert first["attribution"]["attribution_type"] == "verdict_calibration_gap"
    assert duplicate["correlation_status"] == "duplicate"
    assert pending["correlation_status"] == "pending_correlation"
    assert len(store.query(alert_id="missing-alert")["pending_correlations"]) == 1


def test_runtime_records_feedback_run(tmp_path, monkeypatch):
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    async def fake_query(*, prompt, options, transport=None):
        async for _ in prompt:
            pass
        yield AssistantMessage(
            content=[TextBlock(text="研判结果")],
            model="<synthetic>",
            session_id="sdk-session",
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="sdk-session",
            result="研判结果",
            usage={"input_tokens": 1, "output_tokens": 2},
            stop_reason="end_turn",
        )

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    settings = _settings(tmp_path)
    store = _store(settings)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), store)

    result = asyncio.run(runtime.run(ChatRequest(message="研判告警", alert_id="alert-runtime")))
    run = store.find_run(run_id=result["run_id"])

    assert result["run_id"]
    assert run is not None
    assert run["session_id"] == result["session_id"]
    assert run["alert_id"] == "alert-runtime"
    assert run["answer_summary"] == "研判结果"


def test_langfuse_otel_compat_backfills_internal_metrics_constant(monkeypatch):
    import opentelemetry.sdk.environment_variables as otel_env

    monkeypatch.delattr(otel_env, "OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED", raising=False)

    ensure_langfuse_otel_compat()

    assert (
        otel_env.OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED
        == "OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED"
    )
