from feedback_store_test_utils import *
from app.runtime.errors import BusinessRuleViolation


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


def test_feedback_signal_requires_source_locator(tmp_path):
    store, _ = _store(tmp_path)

    with pytest.raises(BusinessRuleViolation, match="run_id, session_id, alert_id, or case_id"):
        store.create_signal(FeedbackSignalCreateRequest(labels=["tool_data_incomplete"]))


def test_source_list_filters_do_not_use_in_memory_filter(tmp_path, monkeypatch):
    store, _ = _store(tmp_path)
    _record_run(store)
    store.record_run(
        {
            "run_id": "run-2",
            "session_id": "session-2",
            "alert_id": "alert-2",
            "case_id": "case-2",
            "message": "第二个运行",
            "created_at": "2026-05-20T00:01:00+00:00",
        }
    )
    store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_incomplete"]))
    store.create_signal(FeedbackSignalCreateRequest(session_id="session-direct", labels=["manual_review"]))
    store.ingest_soc_event(
        SocEventIngestRequest(
            event_id="event-1",
            source_system="soc",
            event_type="tool.manual_query_after_agent",
            timestamp="2026-05-20T00:02:00+00:00",
            run_id="run-1",
        )
    )
    store.ingest_soc_event(
        SocEventIngestRequest(
            event_id="event-2",
            source_system="soc",
            event_type="tool.manual_query_after_agent",
            timestamp="2026-05-20T00:03:00+00:00",
            session_id="unmatched-session",
        )
    )

    def fail_filter(*args, **kwargs):
        raise AssertionError("list queries should push exact filters down to SQLite")

    monkeypatch.setattr(store, "_filter_records", fail_filter)

    assert [item["run_id"] for item in store.list_runs(session_id="session-1")] == ["run-1"]
    assert [item["signal_id"] for item in store.list_signals(run_id="run-1")]
    assert [item["event_id"] for item in store.list_events(run_id="run-1")] == ["event-1"]
    assert [item["event_id"] for item in store.list_pending(status="pending")] == ["event-2"]


def test_generate_eval_cases_rolls_back_case_when_eval_write_fails(tmp_path, monkeypatch):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_incomplete"], comment="数据不全"))

    def fail_add_eval_case(db, payload):
        raise RuntimeError("eval case insert failed")

    monkeypatch.setattr(store, "_add_eval_case_row", fail_add_eval_case)

    with pytest.raises(RuntimeError, match="eval case insert failed"):
        store.generate_eval_cases_for_sources([{"source_kind": "signal", "source_id": signal["signal_id"]}])

    source = store.find_feedback_source("signal", signal["signal_id"])
    assert store.list_cases() == []
    assert store.list_eval_cases() == []
    assert source["feedback_case_id"] is None


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
