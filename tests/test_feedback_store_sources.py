from feedback_store_test_utils import (
    FeedbackOptimizationBatchPlanGenerateRequest,
    FeedbackSignalCreateRequest,
    SocEventIngestRequest,
    ValidationError,
    _complete_eval_case_generation_job,
    _create_batch_with_completed_attribution,
    _eval_case_generation_output,
    _record_run,
    _store,
    asyncio,
    pytest,
)
from app.runtime.errors import BusinessRuleViolation
from app.runtime.runtime_db import (
    AgentRunModel,
    FeedbackSignalModel,
    FeedbackSourceAnnotationModel,
    PendingCorrelationModel,
    SocEventModel,
)
from app.services.agent_job_worker import AgentJobWorker


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


def test_pending_correlation_projection_rejects_invalid_persisted_status(tmp_path):
    store, _ = _store(tmp_path)
    pending = store.ingest_soc_event(
        SocEventIngestRequest(
            event_id="event-invalid-pending",
            source_system="soc",
            event_type="tool.manual_query_after_agent",
            timestamp="2026-05-20T00:03:00+00:00",
            session_id="unmatched-session",
        )
    )["pending_correlation"]

    with store.Session.begin() as db:
        row = db.get(PendingCorrelationModel, pending["pending_id"])
        payload = dict(row.payload_json or {})
        payload["status"] = "unknown_status"
        row.status = "unknown_status"
        row.payload_json = payload

    with pytest.raises(ValidationError):
        store.find_pending(pending["pending_id"])


def test_agent_run_projection_rejects_invalid_persisted_created_at(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)

    with store.Session.begin() as db:
        row = db.get(AgentRunModel, "run-1")
        payload = dict(row.payload_json or {})
        payload["created_at"] = ""
        row.created_at = ""
        row.payload_json = payload

    with pytest.raises(ValidationError):
        store.find_run(run_id="run-1")


def test_feedback_signal_projection_rejects_invalid_persisted_source_type(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["evidence_gap"]))

    with store.Session.begin() as db:
        row = db.get(FeedbackSignalModel, signal["signal_id"])
        payload = dict(row.payload_json or {})
        payload["source_type"] = "legacy_feedback"
        row.source_type = "legacy_feedback"
        row.payload_json = payload

    with pytest.raises(ValidationError):
        store.find_signal(signal["signal_id"])


def test_feedback_source_annotation_projection_rejects_invalid_persisted_status(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["evidence_gap"]))
    store.update_feedback_source_annotation("signal", signal["signal_id"], {"status": "triaged"})

    with store.Session.begin() as db:
        row = db.get(FeedbackSourceAnnotationModel, f"signal:{signal['signal_id']}")
        payload = dict(row.payload_json or {})
        payload["status"] = "legacy_status"
        row.status = "legacy_status"
        row.payload_json = payload

    with pytest.raises(ValidationError):
        store.find_feedback_source("signal", signal["signal_id"])


def test_soc_event_projection_rejects_invalid_persisted_event_type(tmp_path):
    store, _ = _store(tmp_path)
    event = store.ingest_soc_event(
        SocEventIngestRequest(
            event_id="event-invalid-type",
            source_system="soc",
            event_type="tool.manual_query_after_agent",
            timestamp="2026-05-20T00:03:00+00:00",
            session_id="unmatched-session",
        )
    )["event"]

    with store.Session.begin() as db:
        row = db.get(SocEventModel, event["event_id"])
        payload = dict(row.payload_json or {})
        payload["event_type"] = "legacy.event"
        row.event_type = "legacy.event"
        row.payload_json = payload

    with pytest.raises(ValidationError):
        store.find_event(event["event_id"])


def test_generate_eval_cases_projection_failure_fails_agent_job_without_eval_case(tmp_path, monkeypatch):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_incomplete"], comment="数据不全"))
    job = store.generate_eval_cases_for_sources([{"source_kind": "signal", "source_id": signal["signal_id"]}])
    feedback_case = job["input_json"]["feedback_cases"][0]["feedback_case"]

    def fail_add_eval_case(db, payload):
        raise RuntimeError("eval case insert failed")

    monkeypatch.setattr(store, "_add_eval_case_row", fail_add_eval_case)

    async def fake_run_profile_json(**kwargs):
        agent_job = {
            "job_id": kwargs["job_input"]["job_id"],
            "scope_kind": kwargs["job_input"]["scope_kind"],
            "scope_id": kwargs["job_input"]["scope_id"],
            "input_json": kwargs["job_input"],
        }
        return _eval_case_generation_output(agent_job, feedback_case)

    worker = AgentJobWorker(feedback_store=store, run_profile_json=fake_run_profile_json)
    failed = asyncio.run(worker.run_once())

    source = store.find_feedback_source("signal", signal["signal_id"])
    assert failed.status == "failed"
    assert failed.error_json is not None
    assert failed.error_json.error_code == "AGENT_RUNTIME_ERROR"
    assert store.list_eval_cases() == []
    assert source["feedback_case_id"] == feedback_case["feedback_case_id"]


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
    generated_job = store.generate_eval_cases_for_sources(
        [{"source_kind": "signal", "source_id": signal["signal_id"]}],
    )
    feedback_case = generated_job["input_json"]["feedback_cases"][0]["feedback_case"]
    generated_eval_case = _complete_eval_case_generation_job(store, generated_job, feedback_case=feedback_case)
    batch = store.create_optimization_batch(
        [{"source_kind": "signal", "source_id": signal["signal_id"]}],
        title="数据不全批次",
        priority="high",
    )
    batch_eval_job = store.get_agent_job(batch["eval_case_generation_job_id"])
    _complete_eval_case_generation_job(store, batch_eval_job, feedback_case=feedback_case)
    batch = store.find_optimization_batch(batch["batch_id"])

    assert source["comment"] == "数据不全，需要复测"
    assert generated_job["job_type"] == "eval_case_generation"
    assert batch["status"] == "draft"
    assert batch["eval_case_ids"] == [generated_eval_case["eval_case_id"]]

    feedback_case_id = batch["feedback_case_ids"][0]
    attribution_job = store.create_attribution_job(feedback_case_id)
    completed_job = store.complete_attribution_job(
        attribution_job["job_id"],
        {
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
    plan_task = batch["optimization_plan"]["tasks"][0]
    prepared = store.prepare_batch_plan_task_execution(batch["batch_id"], plan_task["plan_task_id"], comment="同意执行")

    assert batch["status"] == "pending_execution"
    assert batch["optimization_plan"]["target_path"] == "CLAUDE.md"
    assert prepared["optimization_task"]["source"] == "feedback_optimization_batch"
    assert prepared["optimization_task"]["source_batch_id"] == batch["batch_id"]
    assert prepared["optimization_task"]["eval_case_ids"] == batch["eval_case_ids"]
    assert prepared["batch"]["internal_proposal_id"] is None
    assert prepared["optimization_task"]["proposal_id"] is None
    assert store.list_proposals(feedback_case_id=feedback_case_id) == []


def test_batch_eval_case_create_update_archive_and_remove_are_scoped_to_batch(tmp_path):
    store, _ = _store(tmp_path)
    batch = _create_batch_with_completed_attribution(store)

    manual = store.create_batch_eval_case(
        batch["batch_id"],
        {
            "prompt": "复测：读取当前 workspace 配置后回答。",
            "expected_behavior": "必须读取 CLAUDE.md。",
            "checks_json": {"requires_tool_use": True},
            "labels": ["manual", "manual"],
        },
    )
    updated_batch = store.find_optimization_batch(batch["batch_id"])

    assert manual is not None
    assert manual["source"] == "optimization_batch_manual"
    assert manual["eval_case_id"] in updated_batch["eval_case_ids"]
    assert manual["labels"] == ["manual", "feedback_optimization", "optimization_batch"]
    assert store.list_batch_eval_cases(batch["batch_id"])[-1]["eval_case_id"] == manual["eval_case_id"]

    edited = store.update_batch_eval_case(
        batch["batch_id"],
        manual["eval_case_id"],
        {"prompt": "复测：确认回答包含 evidence refs。", "status": "archived"},
    )

    assert edited["prompt"] == "复测：确认回答包含 evidence refs。"
    assert edited["status"] == "archived"
    assert store.update_batch_eval_case("fob-missing", manual["eval_case_id"], {"status": "active"}) is None
    assert store.update_batch_eval_case(batch["batch_id"], "evc-not-linked", {"status": "active"}) is None

    removed = store.remove_batch_eval_case(batch["batch_id"], manual["eval_case_id"])

    assert manual["eval_case_id"] not in removed["eval_case_ids"]
    assert store.find_eval_case(manual["eval_case_id"])["status"] == "archived"


def test_batch_eval_case_rejects_invalid_manual_payload(tmp_path):
    store, _ = _store(tmp_path)
    batch = _create_batch_with_completed_attribution(store)

    with pytest.raises(BusinessRuleViolation, match="prompt"):
        store.create_batch_eval_case(batch["batch_id"], {"prompt": "   "})
    with pytest.raises(BusinessRuleViolation, match="checks_json"):
        store.create_batch_eval_case(batch["batch_id"], {"prompt": "有效 prompt", "checks_json": []})


def test_create_signal_records_business_agent_attribution(tmp_path):
    """AGV-024 反馈归属可查：信号 agent_id 后端派生为活跃业务 Agent 并暴露于记录。"""
    store, _ = _store(tmp_path)

    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_incomplete"]))

    # 反馈不进无归属全局问题池：归属可从信号记录直接查到。
    assert signal["agent_id"] == "main-agent"
    # 数据层归属：信号行 agent_id = 活跃业务 Agent。
    with store.Session.begin() as db:
        row = db.get(FeedbackSignalModel, signal["signal_id"])
        assert row.agent_id == "main-agent"


def test_create_signal_attributes_to_run_business_agent(tmp_path):
    """AGV-024：业务 Agent 的 run 产生的反馈归属到该业务 Agent，沿 run.agent_id 链路传播。"""
    store, _ = _store(tmp_path)
    # 业务 Agent 与 main agent 各记录一次运行（agent_id 经 run payload 持久化）。
    store.record_run({"run_id": "run-biz", "agent_id": "soc-ops", "created_at": "2026-06-12T00:00:00Z"})
    store.record_run({"run_id": "run-main", "agent_id": "main-agent", "created_at": "2026-06-12T00:00:00Z"})

    biz_signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-biz", labels=["tool_data_incomplete"]))
    main_signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-main", labels=["tool_data_incomplete"]))
    orphan_signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-none", labels=["tool_data_incomplete"]))

    # 业务 Agent 的反馈归属到该业务 Agent，不串扰、不进无归属全局池。
    assert biz_signal["agent_id"] == "soc-ops"
    assert main_signal["agent_id"] == "main-agent"
    # 无匹配 run 时回退 main（不破坏无 run 历史反馈）。
    assert orphan_signal["agent_id"] == "main-agent"


def test_list_signals_filters_by_agent_dimension(tmp_path):
    """AGV-017/025：反馈可按 Agent 维度过滤——业务 Agent 间反馈互不串扰。"""
    store, _ = _store(tmp_path)
    store.record_run({"run_id": "run-a", "agent_id": "agent-a", "created_at": "2026-06-12T00:00:00Z"})
    store.record_run({"run_id": "run-b", "agent_id": "agent-b", "created_at": "2026-06-12T00:00:00Z"})
    store.create_signal(FeedbackSignalCreateRequest(run_id="run-a", labels=["tool_data_incomplete"]))
    store.create_signal(FeedbackSignalCreateRequest(run_id="run-b", labels=["tool_data_incomplete"]))

    a_signals = store.list_signals(agent_id="agent-a")
    b_signals = store.list_signals(agent_id="agent-b")
    # 每个 Agent 视图只含自身反馈，不被另一个 Agent 的反馈污染。
    assert {s["agent_id"] for s in a_signals} == {"agent-a"}
    assert {s["run_id"] for s in a_signals} == {"run-a"}
    assert {s["agent_id"] for s in b_signals} == {"agent-b"}
    assert {s["run_id"] for s in b_signals} == {"run-b"}


def test_list_runs_filters_by_agent_dimension(tmp_path):
    """AGV-017：运行记录可按 Agent 维度过滤——business Agent 间运行互不串扰。"""
    store, _ = _store(tmp_path)
    store.record_run({"run_id": "run-a", "agent_id": "agent-a", "created_at": "2026-06-12T00:00:00Z"})
    store.record_run({"run_id": "run-b", "agent_id": "agent-b", "created_at": "2026-06-12T00:00:01Z"})

    assert [r["run_id"] for r in store.list_runs(agent_id="agent-a")] == ["run-a"]
    assert [r["run_id"] for r in store.list_runs(agent_id="agent-b")] == ["run-b"]
    # 不带 agent_id 时返回全部（向后兼容）。
    assert {r["run_id"] for r in store.list_runs()} == {"run-a", "run-b"}


def test_create_optimization_batch_rejects_cross_agent_misroute(tmp_path):
    """AGV-025 criterion 1：跨 Agent 反馈混入同一优化批次被拒（误路由防护，不污染他 Agent）。"""
    store, _ = _store(tmp_path)
    store.record_run({"run_id": "run-a", "agent_id": "agent-a", "created_at": "2026-06-12T00:00:00Z"})
    store.record_run({"run_id": "run-b", "agent_id": "agent-b", "created_at": "2026-06-12T00:00:01Z"})
    sig_a = store.create_signal(FeedbackSignalCreateRequest(run_id="run-a", labels=["tool_data_incomplete"]))
    sig_b = store.create_signal(FeedbackSignalCreateRequest(run_id="run-b", labels=["tool_data_incomplete"]))

    # 跨 Agent 混入 -> 显式拒绝。
    with pytest.raises(BusinessRuleViolation, match="Cross-agent"):
        store.create_optimization_batch(
            [
                {"source_kind": "signal", "source_id": sig_a["signal_id"]},
                {"source_kind": "signal", "source_id": sig_b["signal_id"]},
            ]
        )
    # 同一 Agent -> 正常创建（不误伤单 Agent 批次）。
    batch = store.create_optimization_batch([{"source_kind": "signal", "source_id": sig_a["signal_id"]}])
    assert batch is not None
