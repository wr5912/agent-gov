from pathlib import Path
import json

from feedback_store_test_utils import (
    AgentJobResponse,
    FeedbackProposalRegenerateRequest,
    FeedbackSignalCreateRequest,
    FeedbackStore,
    SocEventIngestRequest,
    ValidationError,
    _attribution_output,
    _create_approved_task_for_target,
    _create_batch_with_completed_attribution,
    _proposal_output,
    _record_run,
    _settings,
    _store,
    pytest,
)
from sqlalchemy import select, text

from app.runtime.errors import BusinessRuleViolation, ConfigurationError
from app.runtime.records.external_governance_records import ExternalGovernanceItemRecord
from app.runtime.runtime_db import (
    AgentJobModel,
    EvidenceFileModel,
    EvidencePackageModel,
    ExternalNotificationModel,
    FeedbackOptimizationBatchModel,
    OptimizationProposalModel,
)


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


def test_pending_correlation_resolve_is_single_record_update(tmp_path):
    store, _ = _store(tmp_path)
    pending = store.ingest_soc_event(
        SocEventIngestRequest(
            event_id="evt-pending",
            source_system="sec-ops-ui",
            event_type="case.verdict_changed",
            timestamp="2026-05-20T00:02:00+00:00",
            case_id="case-before",
        )
    )["pending_correlation"]

    resolved = store.resolve_pending(
        "evt-pending",
        run_id="run-after",
        session_id="session-after",
        case_id="case-after",
        comment="人工匹配",
    )
    repeated = store.resolve_pending(pending["pending_id"], run_id="run-after-2")

    assert resolved["pending_id"] == pending["pending_id"]
    assert resolved["status"] == "resolved"
    assert resolved["resolved_run_id"] == "run-after"
    assert resolved["session_id"] == "session-after"
    assert resolved["case_id"] == "case-after"
    assert resolved["comment"] == "人工匹配"
    assert repeated["pending_id"] == pending["pending_id"]
    assert repeated["resolved_run_id"] == "run-after-2"
    assert store.find_pending(pending["pending_id"])["status"] == "resolved"
    assert store.resolve_pending("missing-pending") is None


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
    completed = store.complete_attribution_job(
        attribution_job["job_id"],
        _attribution_output(
            attribution_job,
            actionability="needs_human_analysis",
            recommended_next_step="needs_human_review",
        ),
    )
    output = store.get_job_output(attribution_job["job_id"], "attribution")

    assert completed["status"] == "completed"
    assert store.create_attribution_job(feedback_case["feedback_case_id"])["job_id"] == attribution_job["job_id"]
    assert output["schema_version"] == "attribution-output/v1"
    assert output["actionability"] == "needs_human_analysis"

    proposal_job = store.create_proposal_job(feedback_case["feedback_case_id"])
    store.start_job(proposal_job["job_id"])
    completed_proposal = store.complete_proposal_job(proposal_job["job_id"], _proposal_output(proposal_job))
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
    store.complete_attribution_job(attribution_job["job_id"], _attribution_output(attribution_job))

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
    store.complete_attribution_job(attribution_job["job_id"], _attribution_output(attribution_job))
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
        with store.Session() as db:
            rows = db.scalars(
                select(ExternalNotificationModel).where(
                    ExternalNotificationModel.external_item_id == items[0]["external_item_id"]
                )
            ).all()
        seen["pre_send_notification_statuses"] = [row.status for row in rows]
        return {"http_status": 201, "response_body": "created"}

    updated = store.notify_external_governance_item(items[0]["external_item_id"], webhook_alias="knowledge-base", sender=fake_sender)

    assert store.list_external_webhooks()[0]["alias"] == "knowledge-base"
    assert seen["webhook"]["token"] == "dev-token"
    assert seen["payload"]["schema_version"] == "external-governance-notification/v1"
    assert seen["payload"]["webhook_alias"] == "knowledge-base"
    assert seen["pre_send_notification_statuses"] == ["sending"]
    assert updated["status"] == "notified"
    assert updated["latest_notification"]["status"] == "sent"
    assert updated["latest_notification"]["http_status"] == 201


def test_external_governance_notification_failure_updates_item_status(tmp_path):
    store, settings = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_quality"]))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    store.complete_attribution_job(attribution_job["job_id"], _attribution_output(attribution_job))
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
                    "recommendation": "补充知识库条目。",
                    "reason": "外部知识库缺失。",
                }
            ],
            "no_action_reason": None,
        },
    )
    item = store.list_external_governance_items(feedback_case_id=feedback_case["feedback_case_id"])[0]
    (settings.data_dir / "external-governance-webhooks.yaml").write_text(
        "webhooks:\n  - alias: knowledge-base\n    name: 知识库\n    url: http://example.invalid/kb\n",
        encoding="utf-8",
    )

    updated = store.notify_external_governance_item(
        item["external_item_id"],
        webhook_alias="knowledge-base",
        sender=lambda webhook, payload: {"http_status": 500, "response_body": "failed"},
    )

    assert updated["status"] == "notification_failed"
    assert updated["latest_notification"]["status"] == "failed"
    assert updated["latest_notification"]["http_status"] == 500


def test_external_governance_item_rejects_invalid_status():
    with pytest.raises(ValidationError):
        ExternalGovernanceItemRecord(
            external_item_id="egi-invalid",
            created_at="2026-05-29T00:00:00+00:00",
            updated_at="2026-05-29T00:00:00+00:00",
            status="unknown",
            feedback_case_id="fbc-invalid",
            proposal_job_id="fbp-invalid",
            owner="knowledge-base",
            actionability="external_guidance",
        )


def test_external_governance_notify_requires_known_webhook_alias(tmp_path):
    store, settings = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_quality"]))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    store.complete_attribution_job(attribution_job["job_id"], _attribution_output(attribution_job))
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

    with pytest.raises(ConfigurationError, match="External governance webhook config not found"):
        store.notify_external_governance_item(item["external_item_id"], webhook_alias="missing")

    (settings.data_dir / "external-governance-webhooks.yaml").write_text(
        "webhooks:\n  - alias: other\n    name: Other\n    url: http://example.invalid/other\n",
        encoding="utf-8",
    )
    with pytest.raises(BusinessRuleViolation, match="Unknown external governance webhook alias"):
        store.notify_external_governance_item(item["external_item_id"], webhook_alias="missing")


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


def test_case_projection_rejects_invalid_persisted_status(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_quality"]))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    with store.Session.begin() as db:
        db.execute(
            text("UPDATE feedback_cases SET status = 'unknown_status' WHERE feedback_case_id = :feedback_case_id"),
            {"feedback_case_id": feedback_case["feedback_case_id"]},
        )

    with pytest.raises(ValidationError):
        store.find_case(feedback_case["feedback_case_id"])


def test_create_evidence_package_rolls_back_when_case_attach_fails(tmp_path, monkeypatch):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["evidence_gap"], comment="证据不足"))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])

    def fail_case_update(*args, **kwargs):
        raise RuntimeError("case attach failed")

    monkeypatch.setattr(store, "_append_case_update_row", fail_case_update)

    with pytest.raises(RuntimeError, match="case attach failed"):
        store.create_evidence_package(feedback_case["feedback_case_id"])

    with store.Session() as db:
        assert db.scalars(select(EvidencePackageModel)).all() == []
        assert db.scalars(select(EvidenceFileModel)).all() == []
    unchanged_case = store.find_case(feedback_case["feedback_case_id"])
    assert unchanged_case["status"] == "pending_evidence"
    assert unchanged_case["evidence_package_ids"] == []


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


def test_failed_agent_jobs_can_retry_without_duplicating_active_jobs(tmp_path):
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
    assert AgentJobResponse(**failed_attribution).error_json.error_code == "AGENT_RUNTIME_ERROR"
    assert failed_case["status"] == "pending_attribution"
    assert retried_attribution["job_id"] != attribution_job["job_id"]
    store.complete_attribution_job(retried_attribution["job_id"], _attribution_output(retried_attribution))

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
    store.complete_attribution_job(attribution_job["job_id"], _attribution_output(attribution_job))
    proposal_job = store.create_proposal_job(feedback_case["feedback_case_id"])
    store.complete_proposal_job(proposal_job["job_id"], _proposal_output(proposal_job))

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


def test_create_attribution_job_cleans_record_and_tmp_when_case_attach_fails(tmp_path, monkeypatch):
    store, settings = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_incomplete"], comment="数据不全"))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    store.create_evidence_package(feedback_case["feedback_case_id"])

    def fail_case_update(*args, **kwargs):
        raise RuntimeError("case attach failed")

    monkeypatch.setattr(store, "_append_case_update", fail_case_update)

    with pytest.raises(RuntimeError, match="case attach failed"):
        store.create_attribution_job(feedback_case["feedback_case_id"])

    with store.Session() as db:
        assert db.scalars(select(AgentJobModel)).all() == []
    assert list((settings.data_dir / ".runtime-tmp" / "jobs").iterdir()) == []
    assert store.find_case(feedback_case["feedback_case_id"])["attribution_job_ids"] == []


def test_complete_attribution_job_rolls_back_when_case_update_fails(tmp_path, monkeypatch):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_incomplete"], comment="数据不全"))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])

    def fail_case_update(*args, **kwargs):
        raise RuntimeError("case status update failed")

    monkeypatch.setattr(store, "_append_case_update_row", fail_case_update)

    with pytest.raises(RuntimeError, match="case status update failed"):
        store.complete_attribution_job(attribution_job["job_id"], _attribution_output(attribution_job))

    unchanged_job = store.get_job(attribution_job["job_id"])
    unchanged_case = store.find_case(feedback_case["feedback_case_id"])
    assert unchanged_job["status"] == "queued"
    assert unchanged_job["raw_output_json"] is None
    assert unchanged_job["validated_output_json"] is None
    assert unchanged_job["completed_at"] is None
    assert unchanged_case["status"] == "attribution_queued"


def test_complete_proposal_job_rolls_back_rows_when_case_update_fails(tmp_path, monkeypatch):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_incomplete"], comment="数据不全"))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    store.complete_attribution_job(attribution_job["job_id"], _attribution_output(attribution_job))
    proposal_job = store.create_proposal_job(feedback_case["feedback_case_id"])
    raw_output = {
        "schema_version": "proposal-output/v1",
        "feedback_case_id": feedback_case["feedback_case_id"],
        "proposal_job_id": proposal_job["job_id"],
        "status": "completed",
        "proposals": [
            {
                "proposal_id": "prop-rollback",
                "title": "补充配置核查要求",
                "actionability": "direct_workspace_change",
                "target_type": "main_agent_claude_md",
                "target_path": "CLAUDE.md",
                "recommendation": "回答工作区配置问题前读取配置文件。",
                "expected_effect": "提高回答完整性。",
                "validation": "复测原始反馈输入。",
                "risk": "回答耗时可能增加。",
                "requires_approval": True,
            }
        ],
        "external_guidance": [],
        "no_action_reason": None,
    }

    def fail_case_update(*args, **kwargs):
        raise RuntimeError("case status update failed")

    monkeypatch.setattr(store, "_append_case_update_row", fail_case_update)

    with pytest.raises(RuntimeError, match="case status update failed"):
        store.complete_proposal_job(proposal_job["job_id"], raw_output)

    unchanged_job = store.get_job(proposal_job["job_id"])
    unchanged_case = store.find_case(feedback_case["feedback_case_id"])
    with store.Session() as db:
        assert db.scalars(select(OptimizationProposalModel)).all() == []
    assert unchanged_job["status"] == "queued"
    assert unchanged_job["raw_output_json"] is None
    assert unchanged_job["validated_output_json"] is None
    assert unchanged_job["completed_at"] is None
    assert unchanged_case["status"] == "proposal_queued"


def test_fail_job_rolls_back_when_case_update_fails(tmp_path, monkeypatch):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_incomplete"], comment="数据不全"))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])

    def fail_case_update(*args, **kwargs):
        raise RuntimeError("case status update failed")

    monkeypatch.setattr(store, "_append_case_update_row", fail_case_update)

    with pytest.raises(RuntimeError, match="case status update failed"):
        store.fail_job(attribution_job["job_id"], error_code="AGENT_RUNTIME_ERROR", message="failed")

    unchanged_job = store.get_job(attribution_job["job_id"])
    unchanged_case = store.find_case(feedback_case["feedback_case_id"])
    assert unchanged_job["status"] == "queued"
    assert unchanged_job["error_json"] is None
    assert unchanged_job["completed_at"] is None
    assert unchanged_case["status"] == "attribution_queued"


def test_batch_attribution_uses_current_jobs_and_resets_downstream_plan(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_incomplete"], comment="数据不全"))
    batch = store.create_optimization_batch([{"source_kind": "signal", "source_id": signal["signal_id"]}])
    feedback_case_id = batch["feedback_case_ids"][0]
    attribution_job = store.create_attribution_job(feedback_case_id)
    completed = store.complete_attribution_job(attribution_job["job_id"], _attribution_output(attribution_job))
    batch = store.record_batch_attribution_jobs(batch["batch_id"], [completed])
    batch = store.generate_batch_optimization_plan(batch["batch_id"])

    reset = store.reset_batch_attribution(batch["batch_id"])

    assert batch["optimization_plan"]
    assert reset["status"] == "draft"
    assert reset["attribution_job_ids"] == []
    assert reset["optimization_plan"] is None
    assert store.get_job(attribution_job["job_id"]) is None
    assert not (store.tmp_jobs_dir / attribution_job["job_id"]).exists()


def test_reset_batch_attribution_rolls_back_db_and_keeps_tmp_when_batch_update_fails(tmp_path, monkeypatch):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_incomplete"], comment="数据不全"))
    batch = store.create_optimization_batch([{"source_kind": "signal", "source_id": signal["signal_id"]}])
    feedback_case_id = batch["feedback_case_ids"][0]
    attribution_job = store.create_attribution_job(feedback_case_id)
    completed = store.complete_attribution_job(attribution_job["job_id"], _attribution_output(attribution_job))
    batch = store.record_batch_attribution_jobs(batch["batch_id"], [completed])
    batch = store.generate_batch_optimization_plan(batch["batch_id"])
    job_tmp_dir = store.tmp_jobs_dir / attribution_job["job_id"]
    job_tmp_dir.mkdir(parents=True, exist_ok=True)
    job_tmp_dir.joinpath("marker.txt").write_text("must survive rollback", encoding="utf-8")

    def fail_batch_update(*args, **kwargs):
        raise RuntimeError("batch reset failed")

    monkeypatch.setattr(store, "_update_batch_row", fail_batch_update)

    with pytest.raises(RuntimeError, match="batch reset failed"):
        store.reset_batch_attribution(batch["batch_id"])

    assert store.get_job(attribution_job["job_id"]) is not None
    assert job_tmp_dir.exists()
    assert store.find_optimization_batch(batch["batch_id"])["status"] == batch["status"]


def test_create_optimization_batch_rolls_back_partial_writes_on_batch_failure(tmp_path, monkeypatch):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_incomplete"], comment="数据不全"))

    def fail_batch_model(payload):
        raise RuntimeError("batch insert failed")

    monkeypatch.setattr(store, "_batch_model_from_payload", fail_batch_model)

    with pytest.raises(RuntimeError, match="batch insert failed"):
        store.create_optimization_batch([{"source_kind": "signal", "source_id": signal["signal_id"]}])

    source = store.find_feedback_source("signal", signal["signal_id"])
    assert store.list_optimization_batches() == []
    assert store.list_cases() == []
    assert store.list_eval_cases() == []
    assert source["status"] == "collected"
    assert source["feedback_case_id"] is None


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
    completed = store.complete_attribution_job(first_job["job_id"], _attribution_output(first_job))
    running = store.start_job(second_job["job_id"])

    running_batch = store.record_batch_attribution_jobs(batch["batch_id"], [completed, running])
    failed = store.fail_job(second_job["job_id"], error_code="AGENT_RUNTIME_ERROR", message="failed")
    failed_batch = store.record_batch_attribution_jobs(batch["batch_id"], [completed, failed])

    assert running_batch["status"] == "attribution_running"
    assert running_batch["attribution_summary"]["running"] == 1
    assert failed_batch["status"] == "needs_human_review"
    assert failed_batch["attribution_summary"]["needs_review_or_failed"] == 1


def test_batch_projection_refreshes_failed_attribution_job_snapshot(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(
        FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_incomplete"], comment="归因失败")
    )
    batch = store.create_optimization_batch([{"source_kind": "signal", "source_id": signal["signal_id"]}])
    attribution_job = store.create_attribution_job(batch["feedback_case_ids"][0])
    assert attribution_job is not None
    store.record_batch_attribution_jobs(batch["batch_id"], [attribution_job])

    store.fail_job(attribution_job["job_id"], error_code="AGENT_RUNTIME_ERROR", message="failed")
    refreshed = store.find_optimization_batch(batch["batch_id"])

    assert refreshed is not None
    assert refreshed["status"] == "needs_human_review"
    assert refreshed["attribution_jobs"][0]["status"] == "failed"
    assert refreshed["attribution_jobs"][0]["error_json"]["error_code"] == "AGENT_RUNTIME_ERROR"
    assert refreshed["attribution_summary"]["running"] == 0
    assert refreshed["attribution_summary"]["needs_review_or_failed"] == 1


def test_failed_projected_attribution_syncs_batch_snapshot(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(
        FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_incomplete"], comment="归因失败")
    )
    batch = store.create_optimization_batch([{"source_kind": "signal", "source_id": signal["signal_id"]}])
    attribution_job = store.create_attribution_job(batch["feedback_case_ids"][0])
    assert attribution_job is not None
    store.record_batch_attribution_jobs(batch["batch_id"], [attribution_job])

    failed = store.fail_projected_agent_job(
        attribution_job,
        error_code="AGENT_RUNTIME_ERROR",
        message="failed",
    )

    with store.Session() as db:
        row = db.get(FeedbackOptimizationBatchModel, batch["batch_id"])
        assert row is not None
        row_status = row.status
        payload = row.payload_json

    assert failed is not None
    assert failed["status"] == "failed"
    assert row_status == "needs_human_review"
    assert payload["attribution_jobs"][0]["status"] == "failed"
    assert payload["attribution_summary"]["needs_review_or_failed"] == 1


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
    store.complete_attribution_job(valid_attribution["job_id"], _attribution_output(valid_attribution))
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
