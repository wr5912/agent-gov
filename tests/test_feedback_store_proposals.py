from feedback_store_test_utils import (
    FeedbackSignalCreateRequest,
    _attribution_output,
    _create_eval_case,
    _record_run,
    _store,
    pytest,
)

from app.runtime.errors import ConflictError


def test_proposal_output_normalizes_compact_agent_proposal(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_incomplete"]))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    store.complete_attribution_job(attribution_job["job_id"], _attribution_output(attribution_job))
    proposal_job = store.create_proposal_job(feedback_case["feedback_case_id"])

    completed = store.complete_proposal_job(
        proposal_job["job_id"],
        {
            "schema_version": "proposal-output/v1",
            "feedback_case_id": feedback_case["feedback_case_id"],
            "proposal_job_id": proposal_job["job_id"],
            "status": "completed",
            "proposals": [
                {
                    "id": "prop-001",
                    "target_path": "CLAUDE.md",
                    "actionability": "direct_workspace_change",
                    "rationale": "Agent 未验证 workspace 能力清单。",
                    "recommendation": "Add a Workspace Discovery section to CLAUDE.md.",
                }
            ],
            "external_guidance": [],
            "no_action_reason": None,
        },
    )
    output = store.get_job_output(proposal_job["job_id"], "proposal")
    proposals = store.list_proposals(feedback_case_id=feedback_case["feedback_case_id"])

    assert completed["status"] == "completed"
    assert output["proposals"][0]["proposal_id"] == "prop-001"
    assert output["proposals"][0]["target_type"] == "main_agent_claude_md"
    assert output["proposals"][0]["title"] == "Add a Workspace Discovery section to CLAUDE.md."
    assert output["proposals"][0]["expected_effect"]
    assert proposals[0]["target_path"] == "CLAUDE.md"


def test_proposal_output_normalizes_external_guidance_aliases(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_quality"]))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    store.complete_attribution_job(attribution_job["job_id"], _attribution_output(attribution_job))
    proposal_job = store.create_proposal_job(feedback_case["feedback_case_id"])

    completed = store.complete_proposal_job(
        proposal_job["job_id"],
        {
            "schema_version": "proposal-output/v1",
            "feedback_case_id": feedback_case["feedback_case_id"],
            "proposal_job_id": proposal_job["job_id"],
            "status": "completed",
            "proposals": [
                {
                    "id": "prop-001",
                    "target_path": "CLAUDE.md",
                    "actionability": "direct_workspace_change",
                    "recommendation": "说明实时数据限制。",
                }
            ],
            "external_guidance": [
                {
                    "target": "sec-ops-data MCP service provider",
                    "actionability": "external_guidance",
                    "recommendation": "接入真实告警数据源。",
                    "rationale": "当前工具返回模拟时间戳。",
                }
            ],
            "no_action_reason": None,
        },
    )
    output = store.get_job_output(proposal_job["job_id"], "proposal")
    items = store.list_external_governance_items(feedback_case_id=feedback_case["feedback_case_id"])

    assert completed["status"] == "completed"
    assert len(store.list_proposals(feedback_case_id=feedback_case["feedback_case_id"])) == 1
    assert output["external_guidance"][0]["owner"] == "sec-ops-data MCP service provider"
    assert output["external_guidance"][0]["reason"] == "当前工具返回模拟时间戳。"
    assert items[0]["owner"] == "sec-ops-data MCP service provider"


def test_external_governance_item_filters_are_applied_before_materialization(tmp_path, monkeypatch):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_quality"]))
    first_case = store.create_case(source_ids=[signal["signal_id"]])
    first_attribution = store.create_attribution_job(first_case["feedback_case_id"])
    store.complete_attribution_job(first_attribution["job_id"], _attribution_output(first_attribution))
    first_job = store.create_proposal_job(first_case["feedback_case_id"])
    store.complete_proposal_job(
        first_job["job_id"],
        {
            "schema_version": "proposal-output/v1",
            "feedback_case_id": first_case["feedback_case_id"],
            "proposal_job_id": first_job["job_id"],
            "status": "completed",
            "proposals": [],
            "external_guidance": [{"owner": "knowledge-base", "actionability": "external_guidance", "recommendation": "补充知识库。"}],
            "no_action_reason": None,
        },
    )

    second_signal = store.create_signal(FeedbackSignalCreateRequest(session_id="second-session", labels=["tool_data_quality"]))
    second_case = store.create_case(source_ids=[second_signal["signal_id"]])
    second_attribution = store.create_attribution_job(second_case["feedback_case_id"])
    store.complete_attribution_job(second_attribution["job_id"], _attribution_output(second_attribution))
    second_job = store.create_proposal_job(second_case["feedback_case_id"])
    store.complete_proposal_job(
        second_job["job_id"],
        {
            "schema_version": "proposal-output/v1",
            "feedback_case_id": second_case["feedback_case_id"],
            "proposal_job_id": second_job["job_id"],
            "status": "completed",
            "proposals": [],
            "external_guidance": [{"owner": "sec-ops-data", "actionability": "external_guidance", "recommendation": "补齐 MCP 数据。"}],
            "no_action_reason": None,
        },
    )

    original_item_to_dict = store.external_governance.item_to_dict

    def assert_filtered_row(row):
        assert row.feedback_case_id == first_case["feedback_case_id"]
        assert row.proposal_job_id == first_job["job_id"]
        return original_item_to_dict(row)

    monkeypatch.setattr(store.external_governance, "item_to_dict", assert_filtered_row)

    items = store.list_external_governance_items(
        feedback_case_id=first_case["feedback_case_id"],
        proposal_job_id=first_job["job_id"],
    )

    assert [item["proposal_job_id"] for item in items] == [first_job["job_id"]]


def test_workflow_list_filters_do_not_use_in_memory_filter(tmp_path, monkeypatch):
    store, _ = _store(tmp_path)
    eval_case, feedback_case = _create_eval_case(store)
    eval_case = store.promote_eval_case(eval_case["eval_case_id"], {"operator": "tester", "reason": "filter coverage"})
    proposal = store.list_proposals(feedback_case_id=feedback_case["feedback_case_id"])[0]
    store.review_proposal(proposal["proposal_id"], action="approve", comment="确认")
    task = store.create_task(proposal_id=proposal["proposal_id"])
    batch = store.create_optimization_batch(
        [{"source_kind": "signal", "source_id": feedback_case["source_ids"][0]}],
        title="过滤下推测试批次",
    )
    eval_run = store.create_eval_run(
        eval_case_ids=[eval_case["eval_case_id"]],
        agent_version_id="main-v-test",
        optimization_task_id=task["optimization_task_id"],
        source="manual_task_regression",
    )

    def fail_filter(*args, **kwargs):
        raise AssertionError("workflow list queries should push exact filters down to SQLite")

    monkeypatch.setattr(store, "_filter_records", fail_filter)

    assert store.list_eval_cases(status="active", source_feedback_case_id=feedback_case["feedback_case_id"])[0]["eval_case_id"] == eval_case["eval_case_id"]
    assert store.list_eval_runs(status="running", agent_version_id="main-v-test", optimization_task_id=task["optimization_task_id"])[0]["eval_run_id"] == eval_run["eval_run_id"]
    assert store.list_proposals(feedback_case_id=feedback_case["feedback_case_id"], status="approved")[0]["proposal_id"] == proposal["proposal_id"]
    assert store.list_tasks(feedback_case_id=feedback_case["feedback_case_id"], status="pending_execution")[0]["optimization_task_id"] == task["optimization_task_id"]
    assert store.list_optimization_batches(status="draft")[0]["batch_id"] == batch["batch_id"]


def test_revalidate_proposal_job_raw_output_persists_legacy_suggestions(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_quality"]))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    store.complete_attribution_job(attribution_job["job_id"], _attribution_output(attribution_job))
    proposal_job = store.create_proposal_job(feedback_case["feedback_case_id"])
    raw_output = {
        "schema_version": "proposal-output/v1",
        "feedback_case_id": feedback_case["feedback_case_id"],
        "proposal_job_id": proposal_job["job_id"],
        "status": "needs_human_review",
        "proposals": [
            {
                "id": "prop-001",
                "target_path": "CLAUDE.md",
                "actionability": "direct_workspace_change",
                "recommendation": "说明 MCP 数据限制。",
            }
        ],
        "external_guidance": [
            {
                "target": "sec-ops-data MCP service provider",
                "actionability": "external_guidance",
                "recommendation": "接入真实告警数据源。",
                "rationale": "历史 Agent 使用 target/rationale 字段。",
            }
        ],
        "no_action_reason": None,
    }
    store._set_job_json(
        proposal_job["job_id"],
        raw_output_json=raw_output,
        error_json={"error_code": "SCHEMA_VALIDATION_FAILED", "message": "legacy validation failed"},
    )
    store._append_job_update(proposal_job["job_id"], status="needs_human_review")

    revalidated = store.revalidate_proposal_job(proposal_job["job_id"])
    output = store.get_job_output(proposal_job["job_id"], "proposal")
    proposals = store.list_proposals(feedback_case_id=feedback_case["feedback_case_id"])
    items = store.list_external_governance_items(feedback_case_id=feedback_case["feedback_case_id"])

    assert revalidated["status"] == "completed"
    assert revalidated["error_json"] is None
    assert store.find_case(feedback_case["feedback_case_id"])["status"] == "pending_review"
    assert len(proposals) == 1
    assert proposals[0]["proposal_id"] == "prop-001"
    assert output["external_guidance"][0]["owner"] == "sec-ops-data MCP service provider"
    assert items[0]["owner"] == "sec-ops-data MCP service provider"


def test_force_regenerate_supersedes_unused_existing_proposals(tmp_path):
    store, _ = _store(tmp_path)
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
            "proposals": [
                {
                    "id": "prop-001",
                    "target_path": "CLAUDE.md",
                    "actionability": "direct_workspace_change",
                    "recommendation": "说明 MCP 数据限制。",
                }
            ],
            "external_guidance": [
                {
                    "owner": "knowledge-base",
                    "actionability": "external_guidance",
                    "recommendation": "补充知识库条目。",
                    "reason": "知识库缺少对应说明。",
                }
            ],
            "no_action_reason": None,
        },
    )

    regenerated = store.create_proposal_job(feedback_case["feedback_case_id"], force=True)
    active_proposals = store.list_proposals(feedback_case_id=feedback_case["feedback_case_id"])
    superseded_proposals = store.list_proposals(feedback_case_id=feedback_case["feedback_case_id"], status="superseded")
    active_external_items = store.list_external_governance_items(feedback_case_id=feedback_case["feedback_case_id"])
    superseded_external_items = store.list_external_governance_items(feedback_case_id=feedback_case["feedback_case_id"], status="superseded")

    assert regenerated["job_id"] != proposal_job["job_id"]
    assert regenerated["status"] == "queued"
    assert active_proposals == []
    assert superseded_proposals[0]["proposal_id"] == "prop-001"
    assert superseded_proposals[0]["superseded_by_job_id"] == regenerated["job_id"]
    assert active_external_items == []
    assert superseded_external_items[0]["owner"] == "knowledge-base"


def test_superseded_external_governance_item_cannot_be_notified(tmp_path):
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
                    "reason": "知识库缺少对应说明。",
                }
            ],
            "no_action_reason": None,
        },
    )
    item = store.list_external_governance_items(feedback_case_id=feedback_case["feedback_case_id"])[0]
    store.create_proposal_job(feedback_case["feedback_case_id"], force=True)
    (settings.data_dir / "external-governance-webhooks.yaml").write_text(
        "webhooks:\n  - alias: knowledge-base\n    name: 知识库\n    url: http://example.invalid/kb\n",
        encoding="utf-8",
    )

    with pytest.raises(ConflictError, match="superseded"):
        store.notify_external_governance_item(
            item["external_item_id"],
            webhook_alias="knowledge-base",
            sender=lambda webhook, payload: {"http_status": 200, "response_body": "ok"},
        )
