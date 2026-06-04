from feedback_store_test_utils import (
    FeedbackSignalCreateRequest,
    ValidationError,
    _attribution_output,
    _batch_plan_output,
    _record_run,
    _store,
    pytest,
)
from sqlalchemy import select

from app.runtime.records.external_governance_records import ExternalGovernancePlanTaskDetailRecord
from app.runtime.runtime_db import ExternalGovernanceItemModel, ExternalNotificationModel


def _external_plan_task(store):
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_quality"]))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]])
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    store.complete_attribution_job(attribution_job["job_id"], _attribution_output(attribution_job))
    batch = store.ensure_single_case_optimization_batch(feedback_case["feedback_case_id"])
    plan_job = store.create_batch_plan_job(batch["batch_id"])
    store.complete_batch_plan_job(
        plan_job["job_id"],
        _batch_plan_output(
            plan_job,
            status="pending_approval",
            actionability="external_guidance",
            target_type="external_mcp_service",
            target_path=None,
            tasks=[
                {
                    "execution_kind": "external_webhook",
                    "status": "pending_notification",
                    "title": "通知外部系统修正字段",
                    "description": "外部系统字段缺失。",
                    "objective": "补齐反馈场景需要的数据。",
                    "target_summary": "external:sec-ops-data",
                    "target_type": "external_mcp_service",
                    "target_path": None,
                    "owner": "  sec-ops-data  ",
                    "actionability": "external_guidance",
                    "recommendation": "补齐告警时间戳。",
                    "recommended_actions": ["补齐告警时间戳。"],
                    "acceptance_criteria": ["真实数据通过回归验证。"],
                    "expected_effect": "Agent 可获取完整告警时间。",
                    "validation": "复测反馈场景。",
                    "risk": "外部系统字段变更需确认兼容。",
                    "task_context": {
                        "mcp_server": "sec-ops-data",
                        "tool_name": "mcp__sec-ops-data__list_events",
                        "observed_issue": "反馈场景缺少告警时间戳。",
                        "affected_fields": ["event_time"],
                    },
                    "evidence_refs": [{"type": "evidence_file", "id": "evidence.json", "reason": "归因证据"}, "skip-me"],
                    "feedback_case_ids": [feedback_case["feedback_case_id"]],
                    "eval_case_ids": ["fec-1"],
                    "attribution_job_ids": [attribution_job["job_id"]],
                    "agent_note": {"source": "proposal-governor"},
                }
            ],
            blocked_items=[],
        ),
    )
    batch = store.find_optimization_batch(batch["batch_id"])
    plan = batch["optimization_plan"]
    return batch, plan, plan["tasks"][0]


def test_external_plan_task_upsert_uses_projection_record_and_drops_extra_payload(tmp_path):
    store, _ = _store(tmp_path)
    batch, plan, plan_task = _external_plan_task(store)

    item = store._upsert_external_governance_item_for_plan_task(batch, plan, plan_task)

    assert item["owner"] == "sec-ops-data"
    assert "agent_note" not in item
    assert item["source"] == "feedback_optimization_batch"
    assert item["plan_task_id"] == plan_task["plan_task_id"]
    assert item["task_context"]["mcp_server"] == "sec-ops-data"
    assert item["task_context"]["tool_name"] == "mcp__sec-ops-data__list_events"
    assert item["task_context"]["affected_fields"] == ["event_time"]
    assert item["task_context"]["observed_issue"] == "反馈场景缺少告警时间戳。"


def test_external_plan_task_detail_projection_has_stable_payload_shape(tmp_path):
    store, _ = _store(tmp_path)

    detail = store._plan_task_external_detail(
        {"batch_id": "fob-test", "feedback_case_ids": ["fbc-1"], "eval_case_ids": ["fec-1"]},
        {"optimization_plan_id": "fop-test"},
        {
            "plan_task_id": "fopt-test",
            "title": "通知外部系统修正字段",
            "target_type": "external_mcp_service",
            "target_path": None,
            "task_context": {"mcp_server": "sec-ops-data"},
            "recommended_actions": ["补齐告警时间戳。"],
            "acceptance_criteria": ["真实数据通过回归验证。"],
            "evidence_refs": [{"type": "evidence_file", "id": "evidence.json", "reason": "归因证据"}, "skip-me"],
            "attribution_job_ids": ["fbaj-1"],
        },
    )

    record = ExternalGovernancePlanTaskDetailRecord.model_validate(detail)
    assert record.source == "feedback_optimization_batch"
    assert detail["task_context"] == {"mcp_server": "sec-ops-data"}
    assert detail["evidence_refs"] == [{"type": "evidence_file", "id": "evidence.json", "reason": "归因证据"}]
    assert detail["feedback_case_ids"] == ["fbc-1"]
    assert detail["eval_case_ids"] == ["fec-1"]
    assert detail["source_attribution_job_ids"] == ["fbaj-1"]


def test_external_guidance_upsert_rejects_invalid_persisted_payload(tmp_path):
    store, _ = _store(tmp_path)
    batch, plan, plan_task = _external_plan_task(store)
    item = store._upsert_external_governance_item_for_plan_task(batch, plan, plan_task)

    with store.Session.begin() as db:
        row = db.scalars(select(ExternalGovernanceItemModel)).one()
        row.payload_json = {**row.payload_json, "source_index": {"bad": "index"}}

    with pytest.raises(ValidationError):
        store.find_external_governance_item(item["external_item_id"])


def test_external_governance_item_rejects_invalid_latest_notification_payload(tmp_path):
    store, settings = _store(tmp_path)
    batch, _, plan_task = _external_plan_task(store)
    (settings.data_dir / "external-governance-webhooks.yaml").write_text(
        "webhooks:\n  - alias: sec-ops-data\n    name: SecOps Data\n    url: http://example.invalid/sec-ops-data\n",
        encoding="utf-8",
    )
    result = store.notify_batch_plan_task_external(
        batch["batch_id"],
        plan_task["plan_task_id"],
        webhook_alias="sec-ops-data",
        sender=lambda webhook, payload: {"http_status": 201, "response_body": "created"},
    )

    with store.Session.begin() as db:
        row = db.scalars(select(ExternalNotificationModel)).one()
        row.payload_json = {**row.payload_json, "request_json": ["not", "an", "object"]}

    with pytest.raises(ValidationError):
        store.find_external_governance_item(result["external_item"]["external_item_id"])
