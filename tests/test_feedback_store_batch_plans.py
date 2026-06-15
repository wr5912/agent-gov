from app.runtime.errors import BusinessRuleViolation, ConflictError
from app.runtime.feedback_schemas import FeedbackOptimizationPlanFormatterOutput
from app.runtime.records.batch_plan_records import FeedbackOptimizationPlanTaskRecord
from app.runtime.runtime_db import AgentJobModel, FeedbackOptimizationBatchModel
from pydantic import ValidationError
from sqlalchemy import text

from feedback_store_test_utils import (
    ClaudeRuntime,
    FeedbackSignalCreateRequest,
    LocalSessionStore,
    _batch_plan_output,
    _create_batch_with_completed_attribution,
    _eval_case_generation_output,
    _record_run,
    _store,
    asyncio,
    pytest,
    validate_feedback_optimization_plan_output,
)


def test_batch_plan_regeneration_records_instruction_and_replaces_plan(tmp_path):
    store, _ = _store(tmp_path)
    batch = _create_batch_with_completed_attribution(store)

    first = store.generate_batch_optimization_plan(
        batch["batch_id"],
        regeneration_instruction="优先修改 triage-alert skill 的使用说明",
    )
    second = store.generate_batch_optimization_plan(
        batch["batch_id"],
        regeneration_instruction="避免改动无关 MCP 配置",
    )

    first_plan = first["optimization_plan"]
    second_plan = second["optimization_plan"]
    assert first_plan["optimization_plan_id"] != second_plan["optimization_plan_id"]
    assert second_plan["status"] == "pending_execution"
    assert second_plan["regeneration_instruction"] == "避免改动无关 MCP 配置"
    assert "避免改动无关 MCP 配置" in second_plan["recommendation"]
    assert "避免改动无关 MCP 配置" in second_plan["rationale"]


def _batch_with_unapplied_pending_execution_task(store):
    batch = store.generate_batch_optimization_plan(_create_batch_with_completed_attribution(store)["batch_id"])
    plan_task = batch["optimization_plan"]["tasks"][0]
    prepared = store.prepare_batch_plan_task_execution(batch["batch_id"], plan_task["plan_task_id"])
    task_id = prepared["optimization_task"]["optimization_task_id"]
    updated = store.update_batch_plan_task(
        batch["batch_id"],
        plan_task["plan_task_id"],
        {"description": "人工修订后仍等待执行。"},
    )
    assert updated is not None
    batch = updated.batch
    assert batch["status"] == "pending_execution"
    assert store.find_task(task_id) is not None
    return batch, task_id


def _batch_with_failed_execution_task(store):
    batch, task_id = _batch_with_unapplied_pending_execution_task(store)
    job = store.create_execution_job(task_id, force=True)
    assert job is not None
    store.start_execution_job(job["execution_job_id"])
    failed = store.fail_execution_job(
        job["execution_job_id"],
        error_code="AGENT_RUNTIME_ERROR",
        message="execution optimizer failed",
    )
    batch = store.find_optimization_batch(batch["batch_id"])
    assert failed is not None
    assert batch["status"] == "execution_failed"
    return batch, task_id, job


def _batch_with_external_plan_task(store):
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["external_mcp_service"], comment="alert-0002 事件数据不全"))
    batch = store.create_optimization_batch([{"source_kind": "signal", "source_id": signal["signal_id"]}], title="外部通知失败批次")
    attribution_job = store.create_attribution_job(batch["feedback_case_ids"][0])
    completed = store.complete_attribution_job(
        attribution_job["job_id"],
        {
            "feedback_case_id": batch["feedback_case_ids"][0],
            "attribution_job_id": attribution_job["job_id"],
            "status": "completed",
            "problem_type": "tool_data_quality",
            "optimization_object_type": "external_mcp_service",
            "actionability": "external_guidance",
            "confidence": "high",
            "human_review_required": False,
            "evidence_refs": [{"type": "evidence_file", "id": "tool_calls.json", "reason": "list_events 查询 alert-0002 时 event_time 与告警窗口不匹配"}],
            "responsibility_boundary": {"owner": "sec-ops-data", "reason": "sec-ops-data 的 list_events 接口需要支持按告警上下文返回事件"},
            "rationale": (
                "sec-ops-data MCP 工具 mcp__sec-ops-data__local_api__list_events_api_v1_events_get 查询 alert-0002 时，"
                "返回事件的 event_time 全部是 2026-02-11，无法支撑 2026-05-25 的告警研判。"
                "需要修复 list_events 接口或底层数据源，按 alert_id 和告警时间窗口返回完整事件。"
            ),
            "recommended_next_step": "generate_proposal",
        },
    )
    batch = store.record_batch_attribution_jobs(batch["batch_id"], [completed])
    batch = store.generate_batch_optimization_plan(batch["batch_id"])
    plan_task = batch["optimization_plan"]["tasks"][0]
    return batch, plan_task


def _batch_with_failed_external_notification(store, settings):
    batch, plan_task = _batch_with_external_plan_task(store)
    settings.data_dir.joinpath("external-governance-webhooks.yaml").write_text(
        "webhooks:\n  - alias: kb\n    name: 知识库\n    url: http://example.invalid/kb\n",
        encoding="utf-8",
    )
    failed = store.notify_batch_plan_task_external(
        batch["batch_id"],
        plan_task["plan_task_id"],
        webhook_alias="kb",
        sender=lambda webhook, payload: {"http_status": 500, "response_body": "failed"},
    )
    batch = failed["batch"]
    assert batch["status"] == "pending_execution"
    assert failed["plan_task"]["status"] == "notification_failed"
    assert failed["external_item"]["status"] == "notification_failed"
    return batch, failed["external_item"]


def test_batch_plan_store_does_not_expose_reject_review_action(tmp_path):
    store, _ = _store(tmp_path)
    assert not hasattr(store, "approve_batch_optimization_plan")
    assert not hasattr(store, "reject_batch_optimization_plan")


def test_regenerate_unapplied_pending_execution_batch_plan_requeues_and_cleans_draft_task(tmp_path):
    store, _ = _store(tmp_path)
    batch, task_id = _batch_with_unapplied_pending_execution_task(store)

    job = store.create_batch_plan_job(batch["batch_id"], force=True, regeneration_instruction="重新聚焦 MCP 配置")
    updated = store.find_optimization_batch(batch["batch_id"])

    assert job["job_type"] == "batch_plan"
    assert updated["status"] == "optimization_plan_queued"
    assert updated["optimization_plan"] is None
    assert updated["optimization_task_id"] is None
    assert updated["optimization_task_ids"] == []
    assert store.find_task(task_id) is None


def test_reset_attribution_from_active_batch_plan_job_discards_stale_plan_job(tmp_path):
    store, _ = _store(tmp_path)
    batch = _create_batch_with_completed_attribution(store)
    plan_job = store.create_batch_plan_job(batch["batch_id"], force=True)
    queued = store.find_optimization_batch(batch["batch_id"])

    reset = store.reset_batch_attribution(batch["batch_id"])

    assert queued["status"] == "optimization_plan_queued"
    assert reset["status"] == "draft"
    assert reset["optimization_plan"] is None
    assert reset["optimization_plan_job_id"] is None
    assert store.get_job(plan_job["job_id"]) is None


def test_regenerate_failed_execution_batch_plan_requeues_and_cleans_draft_task(tmp_path):
    store, _ = _store(tmp_path)
    batch, task_id, execution_job = _batch_with_failed_execution_task(store)

    job = store.create_batch_plan_job(batch["batch_id"], force=True, regeneration_instruction="执行失败后重新生成")
    updated = store.find_optimization_batch(batch["batch_id"])

    assert job["job_type"] == "batch_plan"
    assert updated["status"] == "optimization_plan_queued"
    assert updated["optimization_plan"] is None
    assert updated["optimization_task_id"] is None
    assert updated["optimization_task_ids"] == []
    assert store.find_task(task_id) is None
    assert store.get_execution_job(execution_job["execution_job_id"]) is None


def test_reset_attribution_from_failed_execution_cleans_downstream_drafts(tmp_path):
    store, _ = _store(tmp_path)
    batch, task_id, execution_job = _batch_with_failed_execution_task(store)

    reset = store.reset_batch_attribution(batch["batch_id"])

    assert reset["status"] == "draft"
    assert reset["attribution_job_ids"] == []
    assert reset["optimization_plan"] is None
    assert reset["optimization_task_id"] is None
    assert reset["optimization_task_ids"] == []
    assert store.find_task(task_id) is None
    assert store.get_execution_job(execution_job["execution_job_id"]) is None


def test_regenerate_batch_plan_after_failed_external_notification_cleans_external_item(tmp_path):
    store, settings = _store(tmp_path)
    batch, external_item = _batch_with_failed_external_notification(store, settings)

    job = store.create_batch_plan_job(batch["batch_id"], force=True, regeneration_instruction="通知失败后重新生成")
    updated = store.find_optimization_batch(batch["batch_id"])

    assert job["job_type"] == "batch_plan"
    assert updated["status"] == "optimization_plan_queued"
    assert updated["optimization_plan"] is None
    assert store.find_external_governance_item(external_item["external_item_id"]) is None


def test_regenerate_batch_plan_blocks_sent_external_notification_when_batch_projection_failed(tmp_path, monkeypatch):
    store, settings = _store(tmp_path)
    batch, plan_task = _batch_with_external_plan_task(store)
    settings.data_dir.joinpath("external-governance-webhooks.yaml").write_text(
        "webhooks:\n  - alias: kb\n    name: 知识库\n    url: http://example.invalid/kb\n",
        encoding="utf-8",
    )

    def fail_plan_task_projection(*args, **kwargs):
        raise RuntimeError("batch plan task projection failed")

    monkeypatch.setattr(store, "_update_batch_plan_task", fail_plan_task_projection)
    with pytest.raises(RuntimeError, match="batch plan task projection failed"):
        store.notify_batch_plan_task_external(
            batch["batch_id"],
            plan_task["plan_task_id"],
            webhook_alias="kb",
            sender=lambda webhook, payload: {"http_status": 202, "response_body": "accepted"},
        )

    notified_items = store.list_external_governance_items(status="notified")
    unchanged = store.find_optimization_batch(batch["batch_id"])
    with pytest.raises(ConflictError, match="已有外部通知结果"):
        store.create_batch_plan_job(batch["batch_id"], force=True, regeneration_instruction="不应覆盖已通知任务")

    assert len(notified_items) == 1
    assert unchanged["optimization_plan"]["tasks"][0]["status"] == "pending_notification"
    assert store.find_external_governance_item(notified_items[0]["external_item_id"]) is not None


def test_reset_attribution_from_unapplied_pending_execution_cleans_downstream_drafts(tmp_path):
    store, _ = _store(tmp_path)
    batch, task_id = _batch_with_unapplied_pending_execution_task(store)

    reset = store.reset_batch_attribution(batch["batch_id"])

    assert reset["status"] == "draft"
    assert reset["attribution_job_ids"] == []
    assert reset["optimization_plan"] is None
    assert reset["optimization_plan_job_id"] is None
    assert reset["optimization_task_id"] is None
    assert reset["optimization_task_ids"] == []
    assert reset["latest_execution_run"] is None
    assert store.find_task(task_id) is None


def test_batch_plan_running_job_is_reused_and_failed_job_is_replaced_on_retry(tmp_path):
    store, _ = _store(tmp_path)
    batch = _create_batch_with_completed_attribution(store)

    first = store.create_batch_plan_job(batch["batch_id"], force=True)
    repeated = store.create_batch_plan_job(batch["batch_id"], force=True)
    store.start_job(first["job_id"])
    running_repeated = store.create_batch_plan_job(batch["batch_id"], force=True, regeneration_instruction="重复点击")
    store.fail_job(first["job_id"], error_code="AGENT_RUNTIME_ERROR", message="formatter failed")
    retry = store.create_batch_plan_job(batch["batch_id"], force=True)

    assert repeated["job_id"] == first["job_id"]
    assert running_repeated["job_id"] == first["job_id"]
    assert retry["job_id"] != first["job_id"]
    assert store.get_job(first["job_id"]) is None
    assert [job["job_id"] for job in store.list_agent_jobs(job_type="batch_plan", scope_kind="optimization_batch", scope_id=batch["batch_id"])] == [
        retry["job_id"]
    ]


def test_batch_plan_failure_preserves_raw_output_diagnostics(tmp_path):
    store, _ = _store(tmp_path)
    batch = _create_batch_with_completed_attribution(store)
    job = store.create_batch_plan_job(batch["batch_id"], force=True)
    raw_output = {
        "_formatter": {
            "name": "dspy",
            "status": "failed",
            "candidate_count": 0,
        },
        "raw_text": "优化方案生成智能体输出了自然语言方案。",
    }

    failed = store.fail_job(
        job["job_id"],
        error_code="AGENT_RUNTIME_ERROR",
        message="OutputFormatterError: formatter failed",
        raw_output_json=raw_output,
    )
    updated = store.find_optimization_batch(batch["batch_id"])

    assert failed["raw_output_json"] == raw_output
    assert updated["optimization_plan"] is None
    assert updated["optimization_plan_error"]["error_code"] == "AGENT_RUNTIME_ERROR"
    assert updated["optimization_plan_job"]["raw_output_json"] == raw_output


def test_stale_batch_plan_timeout_projects_to_batch_error(tmp_path):
    store, _ = _store(tmp_path)
    batch = _create_batch_with_completed_attribution(store)
    job = store.create_batch_plan_job(batch["batch_id"], force=True)
    claimed = store.claim_next_agent_job(job_types=["batch_plan"])
    assert claimed is not None
    assert claimed["job_id"] == job["job_id"]
    with store.Session.begin() as db:
        row = db.get(AgentJobModel, job["job_id"])
        assert row is not None
        row.started_at = "2026-01-01T00:00:00+00:00"
        row.timeout_seconds = 1

    timed_out = store._timeout_stale_agent_jobs()
    updated = store.find_optimization_batch(batch["batch_id"])

    assert [item["job_id"] for item in timed_out] == [job["job_id"]]
    assert updated["status"] == "needs_human_review"
    assert updated["optimization_plan"] is None
    assert updated["optimization_plan_job"]["status"] == "timeout"
    assert updated["optimization_plan_error"]["error_code"] == "AGENT_TIMEOUT"


def test_batch_plan_projection_refreshes_stale_job_snapshot(tmp_path):
    store, _ = _store(tmp_path)
    batch = _create_batch_with_completed_attribution(store)
    job = store.create_batch_plan_job(batch["batch_id"], force=True)
    error_payload = {
        "error_code": "AGENT_TIMEOUT",
        "message": "Agent job exceeded timeout_seconds=300",
        "created_at": "2026-06-06T08:48:12+00:00",
        "job_id": job["job_id"],
    }
    with store.Session.begin() as db:
        row = db.get(AgentJobModel, job["job_id"])
        assert row is not None
        row.status = "timeout"
        row.started_at = "2026-06-06T08:43:11+00:00"
        row.completed_at = "2026-06-06T08:48:12+00:00"
        row.error_json = error_payload
        batch_row = db.get(FeedbackOptimizationBatchModel, batch["batch_id"])
        assert batch_row is not None
        stale_payload = dict(batch_row.payload_json or {})
        assert stale_payload["optimization_plan_job"]["status"] == "queued"
        assert stale_payload["optimization_plan_error"] is None

    refreshed = store.find_optimization_batch(batch["batch_id"])

    assert refreshed["status"] == "needs_human_review"
    assert refreshed["optimization_plan"] is None
    assert refreshed["optimization_plan_job"]["status"] == "timeout"
    assert refreshed["optimization_plan_error"]["error_code"] == "AGENT_TIMEOUT"


def test_complete_batch_plan_job_sanitizes_attribution_summary_extras(tmp_path):
    store, _ = _store(tmp_path)
    batch = _create_batch_with_completed_attribution(store)
    job = store.create_batch_plan_job(batch["batch_id"], force=True)
    raw_output = _batch_plan_output(
        job,
        attribution_summaries=[
            {
                "job_id": "fba-attribution-1",
                "owner": "mcp_config",
                "feedback_case_id": batch["feedback_case_ids"][0],
                "problem_type": "tool_data_quality",
                "optimization_object_type": "mcp_description",
                "actionability": "external_guidance",
                "confidence": "high",
                "rationale": "外部 MCP 数据源返回不完整。",
                "summary": "归因指向外部 MCP 数据源。",
            }
        ],
    )

    completed = store.complete_batch_plan_job(job["job_id"], raw_output)
    updated = store.find_optimization_batch(batch["batch_id"])

    assert completed["status"] == "completed"
    summary = updated["optimization_plan"]["attribution_summaries"][0]
    assert summary["attribution_job_id"] == batch["attribution_job_ids"][0]
    assert summary["feedback_case_id"] == batch["feedback_case_ids"][0]
    assert summary["problem_type"] == "tool_misuse"
    assert summary["optimization_object_type"] == "main_agent_claude_md"
    assert summary["actionability"] == "direct_workspace_change"
    assert summary["confidence"] == "high"
    assert summary["rationale"] == "Agent 回答工作区能力问题时未读取当前配置文件。"
    assert "owner" not in summary
    assert "summary" not in summary


def test_batch_plan_output_context_fields_are_backend_authoritative(tmp_path):
    store, _ = _store(tmp_path)
    batch = _create_batch_with_completed_attribution(store)
    job = store.create_batch_plan_job(batch["batch_id"], force=True)
    raw_output = _batch_plan_output(
        job,
        batch_id="fob-agent-wrong",
        optimization_plan_id="fop-agent-wrong",
        created_at="2026-01-01T00:00:00+00:00",
        source_refs=[{"source_kind": "agent", "source_id": "wrong"}],
        feedback_case_ids=["fbc-agent-wrong"],
        eval_case_ids=["evc-agent-wrong"],
        attribution_job_ids=["fba-agent-wrong"],
        status="pending_execution",
        actionability="direct_workspace_change",
        target_type="main_agent_claude_md",
        target_path="CLAUDE.md",
        tasks=[
            {
                "plan_task_id": "fopt-agent-wrong",
                "execution_kind": "workspace_execution",
                "status": "completed",
                "title": "补充主智能体约束",
                "description": "补充读取配置前的约束。",
                "objective": "降低同类反馈。",
                "target_type": "main_agent_claude_md",
                "target_path": "CLAUDE.md",
                "actionability": "direct_workspace_change",
                "recommendation": "补充读取配置前必须核查 workspace。",
                "expected_effect": "减少同类反馈。",
                "validation": "复测原反馈。",
                "risk": "可能增加一次文件读取。",
                "feedback_case_ids": ["fbc-agent-wrong"],
                "eval_case_ids": ["evc-agent-wrong"],
                "attribution_job_ids": ["fba-agent-wrong"],
                "task_context": {"target_file": "CLAUDE.md"},
            }
        ],
        blocked_items=[],
    )

    completed = store.complete_batch_plan_job(job["job_id"], raw_output)
    plan = completed["validated_output_json"]
    task = plan["tasks"][0]

    assert plan["batch_id"] == batch["batch_id"]
    assert plan["optimization_plan_id"] != "fop-agent-wrong"
    assert plan["created_at"] != "2026-01-01T00:00:00+00:00"
    assert plan["source_refs"] == batch["source_refs"]
    assert plan["feedback_case_ids"] == batch["feedback_case_ids"]
    assert plan["eval_case_ids"] == batch["eval_case_ids"]
    assert plan["attribution_job_ids"] == batch["attribution_job_ids"]
    assert task["plan_task_id"] != "fopt-agent-wrong"
    assert task["status"] == "pending_execution"
    assert task["feedback_case_ids"] == batch["feedback_case_ids"]
    assert task["eval_case_ids"] == batch["eval_case_ids"]
    assert task["attribution_job_ids"] == batch["attribution_job_ids"]


def test_batch_projection_rejects_invalid_persisted_status(tmp_path):
    store, _ = _store(tmp_path)
    batch = _create_batch_with_completed_attribution(store)
    with store.Session.begin() as db:
        db.execute(text("UPDATE feedback_optimization_batches SET status = 'unknown_status' WHERE batch_id = :batch_id"), {"batch_id": batch["batch_id"]})

    with pytest.raises(ValidationError):
        store.find_optimization_batch(batch["batch_id"])


def test_feedback_optimization_plan_output_requires_actionable_external_context():
    validated, error = validate_feedback_optimization_plan_output(
        {
            "batch_id": "fob-test",
            "status": "pending_execution",
            "title": "外部服务优化方案",
            "summary": "统筹归因结果生成任务。",
            "confidence": "high",
            "actionability": "external_guidance",
            "target_type": "external_mcp_service",
            "target_path": None,
            "recommendation": "修复外部 MCP 服务返回数据不完整问题。",
            "expected_effect": "Agent 可获得完整数据。",
            "validation": "回归用例通过。",
            "risk": "外部系统需要变更。",
            "tasks": [
                {
                    "execution_kind": "external_webhook",
                    "title": "补齐外部数据",
                    "description": "任务缺少具体接口和问题对象。",
                    "objective": "提高数据完整性。",
                    "target_type": "external_mcp_service",
                    "owner": "sec-ops-data",
                    "actionability": "external_guidance",
                    "recommendation": "修复数据返回。",
                    "expected_effect": "数据完整。",
                    "validation": "回归通过。",
                    "risk": "需外部系统配合。",
                    "task_context": {"mcp_server": "sec-ops-data"},
                }
            ],
            "blocked_items": [],
        }
    )

    assert error is None
    assert validated is not None
    assert validated["status"] == "needs_human_review"
    assert validated["tasks"] == []
    assert validated["blocked_items"][0]["reason"] == "任务缺少明确的外部对象、接口或问题 ID，不能派发到外部系统。"


def test_feedback_optimization_plan_output_promotes_actionable_blocked_external_item():
    validated, error = validate_feedback_optimization_plan_output(
        {
            "batch_id": "fob-test",
            "status": "pending_execution",
            "title": "漏洞数据源优化方案",
            "summary": "统筹归因结果生成任务。",
            "confidence": "high",
            "actionability": "external_guidance",
            "target_type": "external_mcp_service",
            "target_path": None,
            "recommendation": "通知外部 MCP 工具提供方修复漏洞数据源。",
            "expected_effect": "Agent 可查询到完整漏洞数据。",
            "validation": "回归用例通过。",
            "risk": "外部系统需要变更。",
            "tasks": [],
            "blocked_items": [
                {
                    "title": "确认并上报漏洞数据源 2026 年数据缺失问题",
                    "target_type": "external_mcp_service",
                    "owner": "sec-ops-data",
                    "actionability": "external_guidance",
                    "problem_type": "tool_data_quality",
                    "reason": ("归因显示 sec-ops-data MCP 工具 list_vulnerabilities 查询漏洞数据时，无法获得 2026 年 CVE 数据。"),
                    "recommendation": (
                        "请 sec-ops-data 工具提供方核查 list_vulnerabilities_api_v1_vulnerabilities_get 的数据源覆盖范围，确认 2026 年漏洞数据是否缺失。"
                    ),
                    "feedback_case_ids": ["fbc-2026"],
                    "attribution_job_ids": ["fba-2026"],
                }
            ],
        }
    )

    assert error is None
    assert validated is not None
    assert validated["blocked_items"] == []
    assert len(validated["tasks"]) == 1
    task = validated["tasks"][0]
    assert task["execution_kind"] == "external_webhook"
    assert task["status"] == "pending_notification"
    assert task["actionability"] == "external_guidance"
    assert task["target_summary"] == "external:sec-ops-data"
    assert task["task_context"]["mcp_server"] == "sec-ops-data"
    assert task["task_context"]["tool_name"] == "list_vulnerabilities_api_v1_vulnerabilities_get"
    assert "2026" in task["task_context"]["observed_issue"]
    assert "year" in task["task_context"]["affected_fields"]
    assert "cve_coverage" in task["task_context"]["affected_fields"]
    assert task["feedback_case_ids"] == ["fbc-2026"]
    assert task["attribution_job_ids"] == ["fba-2026"]


def test_feedback_optimization_plan_output_promotes_blocked_item_with_plan_context():
    validated, error = validate_feedback_optimization_plan_output(
        {
            "batch_id": "fob-test",
            "status": "pending_execution",
            "title": "漏洞数据查询优化方案",
            "summary": ("归因确认 sec-ops-data MCP 的 list_vulnerabilities_api_v1_vulnerabilities_get 返回的漏洞记录缺少 2026 年 CVE 数据。"),
            "confidence": "medium",
            "actionability": "external_guidance",
            "target_type": "mcp_description",
            "target_path": None,
            "recommendation": "统筹处理漏洞数据覆盖和工具描述问题。",
            "expected_effect": "Agent 能查询到 2026 年漏洞数据。",
            "validation": "回归用例通过。",
            "risk": "外部数据源可能需要修复。",
            "tasks": [],
            "blocked_items": [
                {
                    "title": "确认并上报漏洞数据源 2026 年数据缺失问题",
                    "reason": "当前方案正文已定位到数据源缺失，但该项被智能体放入 blocked_items。",
                    "recommendation": "联系 sec-ops-data 数据维护团队确认 2026 年 CVE 数据覆盖情况。",
                }
            ],
        }
    )

    assert error is None
    assert validated is not None
    assert validated["blocked_items"] == []
    task = validated["tasks"][0]
    assert task["execution_kind"] == "external_webhook"
    assert task["target_summary"] == "external:sec-ops-data"
    assert task["task_context"]["mcp_server"] == "sec-ops-data"
    assert task["task_context"]["tool_name"] == "list_vulnerabilities_api_v1_vulnerabilities_get"
    assert "2026" in task["task_context"]["observed_issue"]


def test_feedback_optimization_plan_output_keeps_generic_blocked_external_item():
    validated, error = validate_feedback_optimization_plan_output(
        {
            "batch_id": "fob-test",
            "status": "pending_execution",
            "title": "外部问题优化方案",
            "summary": "统筹归因结果生成任务。",
            "confidence": "medium",
            "actionability": "external_guidance",
            "target_type": "external_mcp_service",
            "target_path": None,
            "recommendation": "通知外部系统修复。",
            "expected_effect": "Agent 可获得完整数据。",
            "validation": "回归用例通过。",
            "risk": "外部系统需要变更。",
            "tasks": [],
            "blocked_items": [
                {
                    "title": "确认外部数据缺失问题",
                    "target_type": "external_mcp_service",
                    "actionability": "external_guidance",
                    "reason": "数据不全，但没有定位到具体系统、工具或接口。",
                    "recommendation": "补充外部系统信息后重新生成优化方案。",
                }
            ],
        }
    )

    assert error is None
    assert validated is not None
    assert validated["tasks"] == []
    assert len(validated["blocked_items"]) == 1
    assert validated["blocked_items"][0]["title"] == "确认外部数据缺失问题"


def test_batch_plan_task_projection_normalizes_evidence_refs(tmp_path):
    store, _ = _store(tmp_path)
    batch = {"batch_id": "fob-test", "feedback_case_ids": ["fbc-1"], "eval_case_ids": []}
    plan = {"attribution_job_ids": ["fbaj-1"], "rationale": "回答未读取配置。"}

    task = store._normalize_plan_task(
        batch,
        plan,
        {
            "execution_kind": "workspace_execution",
            "title": "补充配置核查约束",
            "target_type": "main_agent_claude_md",
            "target_path": "CLAUDE.md",
            "recommendation": "补充回答前读取配置的约束。",
            "task_context": {"target_file": "CLAUDE.md"},
            "evidence_refs": [
                {"path": "messages.json", "description": "回答来自记忆。", "agent_note": {"source": "planner"}},
                {"type": "evidence_file"},
                "skip-me",
            ],
        },
    )

    record = FeedbackOptimizationPlanTaskRecord.model_validate(task)
    assert record.task_context.target_file == "CLAUDE.md"
    assert len(record.evidence_refs) == 1
    assert record.evidence_refs[0].id == "messages.json"
    assert record.evidence_refs[0].reason == "回答来自记忆。"
    assert "agent_note" not in task["evidence_refs"][0]


def test_feedback_optimization_plan_output_normalizes_string_attribution_summaries():
    validated, error = validate_feedback_optimization_plan_output(
        {
            "batch_id": "fob-test",
            "status": "ready_for_execution",
            "title": "漏洞数据查询优化方案",
            "summary": "统筹归因结果生成任务。",
            "problem_types": ["tool_data_quality"],
            "confidence": "medium",
            "actionability": "needs_human_analysis",
            "target_type": "mcp_description",
            "target_path": "",
            "recommendation": "核查漏洞查询工具是否支持年份筛选。",
            "expected_effect": "减少同类数据缺失反馈。",
            "validation": "运行反馈对应回归用例。",
            "risk": "外部数据源可能仍不完整。",
            "attribution_summaries": ["归因确认漏洞查询缺少 2026 年数据。"],
            "tasks": [
                {
                    "execution_kind": "workspace_execution",
                    "title": "核查漏洞查询工具描述",
                    "description": "确认工具是否支持年份筛选。",
                    "objective": "让 Agent 能查询指定年份漏洞。",
                    "target_type": "mcp_description",
                    "target_path": "",
                    "actionability": "direct_workspace_change",
                    "recommendation": "补充年份筛选说明。",
                    "expected_effect": "查询结果覆盖指定年份。",
                    "validation": "回归用例通过。",
                    "risk": "底层数据可能缺失。",
                }
            ],
            "blocked_items": [],
        }
    )

    assert error is None
    assert validated is not None
    assert validated["status"] == "needs_human_review"
    assert validated["attribution_summaries"] == [{"summary": "归因确认漏洞查询缺少 2026 年数据。"}]
    assert validated["tasks"] == []
    assert validated["blocked_items"][0]["reason"] == "任务缺少 target_path，不能交给 execution-optimizer 执行。"


def test_batch_plan_generation_uses_proposal_generator_agent_output(tmp_path, monkeypatch):
    store, settings = _store(tmp_path)
    batch = _create_batch_with_completed_attribution(store)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir), feedback_store=store)
    seen = {}

    async def fake_run_profile_json(**kwargs):
        seen.update(kwargs)
        return FeedbackOptimizationPlanFormatterOutput.model_validate(
            {
                "status": "pending_execution",
                "title": "补强工作区配置核查",
                "summary": "根据归因结果生成一个 workspace 优化任务。",
                "problem_types": ["tool_misuse"],
                "confidence": "high",
                "actionability": "direct_workspace_change",
                "target_type": "main_agent_claude_md",
                "target_path": "CLAUDE.md",
                "recommendation": "在 CLAUDE.md 中补充回答工作区配置问题前必须读取配置文件的要求。",
                "expected_effect": "Agent 回答同类问题时先读取当前配置。",
                "validation": "使用批次回归用例验证是否读取配置并回答完整。",
                "risk": "可能增加少量工具调用。",
                "rationale": "归因结果显示 Agent 未读取当前工作区配置。",
                "evidence_refs": [{"type": "evidence_file", "id": "messages.json", "reason": "回答未核查当前配置。"}],
                "tasks": [
                    {
                        "execution_kind": "workspace_execution",
                        "status": "pending_execution",
                        "title": "补充工作区配置核查指令",
                        "description": "在主智能体指令中要求回答工作区配置问题前读取当前配置文件。",
                        "objective": "让 Agent 对配置枚举类问题使用当前文件内容作答。",
                        "target_summary": "workspace:CLAUDE.md",
                        "target_type": "main_agent_claude_md",
                        "target_path": "CLAUDE.md",
                        "owner": "main_agent_workspace",
                        "actionability": "direct_workspace_change",
                        "confidence": "high",
                        "problem_type": "tool_misuse",
                        "recommendation": "追加配置核查要求。",
                        "recommended_actions": ["由 execution-optimizer 生成 CLAUDE.md 的受控追加方案。"],
                        "acceptance_criteria": ["回归用例通过，且回答前读取当前配置文件。"],
                        "expected_effect": "同类反馈不再复现。",
                        "validation": "运行批次回归测试。",
                        "risk": "回答耗时略增。",
                        "analysis_summary": "Agent 未读取当前配置。",
                        "evidence_summary": "messages.json 显示回答来自记忆。",
                        "evidence_refs": [{"type": "evidence_file", "id": "messages.json", "reason": "回答未核查当前配置。"}],
                        "task_context": {"target_file": "CLAUDE.md", "config_section": "workspace-capability-answering"},
                    }
                ],
                "blocked_items": [],
            }
        )

    monkeypatch.setattr(runtime, "_run_profile_json", fake_run_profile_json)

    updated = asyncio.run(runtime.run_batch_optimization_plan(batch["batch_id"], regeneration_instruction="优先保持指令简洁"))
    assert updated is not None
    plan = updated.optimization_plan
    assert plan is not None
    plan_task = plan.tasks[0]
    job = store.get_job(updated.optimization_plan_job_id)

    assert seen["profile_name"] == "governor"
    assert seen["job_type"] == "batch_plan"
    assert "optimization_plan_prompt_context" in seen["prompt"]
    assert "optimization_plan_input_json" not in seen["prompt"]
    assert "schema_version" not in seen["prompt"]
    assert "job_id" not in seen["prompt"]
    assert seen["job_input"]["regeneration_instruction"] == "优先保持指令简洁"
    assert job["job_type"] == "batch_plan"
    assert job["profile_name"] == "governor"
    assert job["status"] == "completed"
    assert plan.generated_by == "governor"
    assert plan.status == "pending_execution"
    assert plan_task.title == "补充工作区配置核查指令"
    assert plan_task.target_path == "CLAUDE.md"
    assert plan_task.task_context.target_file == "CLAUDE.md"
    assert plan_task.task_context.config_section == "workspace-capability-answering"
    assert plan_task.feedback_case_ids == seen["job_input"]["feedback_case_ids"]
    assert plan_task.attribution_job_ids == seen["job_input"]["attribution_job_ids"]


def test_complete_batch_plan_job_projects_incomplete_formatter_task_as_blocked_item(tmp_path):
    store, _ = _store(tmp_path)
    batch = _create_batch_with_completed_attribution(store)
    job = store.create_batch_plan_job(batch["batch_id"])
    formatter_output = FeedbackOptimizationPlanFormatterOutput.model_validate(
        {
            "status": "pending_execution",
            "title": "统筹优化 sec-ops 数据源问题",
            "tasks": [
                {
                    "title": "修复 sec-ops 数据源 2026 年数据缺失",
                    "description": "在运行时环境中补充数据源覆盖说明。",
                }
            ],
        }
    )

    completed = store.complete_batch_plan_job(job["job_id"], formatter_output)
    updated_batch = store.find_optimization_batch(batch["batch_id"])
    plan = updated_batch["optimization_plan"]

    assert completed["status"] == "completed"
    assert updated_batch["status"] == "needs_human_review"
    assert updated_batch["optimization_plan_error"] is None
    assert plan["tasks"] == []
    assert plan["blocked_items"][0]["title"] == "修复 sec-ops 数据源 2026 年数据缺失"
    assert "Agent 输出的优化任务缺少可执行字段" in plan["blocked_items"][0]["reason"]
    assert "execution_kind" in plan["blocked_items"][0]["reason"]


def test_complete_batch_plan_job_rolls_back_when_batch_update_fails(tmp_path, monkeypatch):
    store, _ = _store(tmp_path)
    batch = _create_batch_with_completed_attribution(store)
    job = store.create_batch_plan_job(batch["batch_id"])

    def fail_batch_update(*args, **kwargs):
        raise RuntimeError("batch update failed")

    monkeypatch.setattr(store, "_update_batch_row", fail_batch_update)

    with pytest.raises(RuntimeError, match="batch update failed"):
        store.complete_batch_plan_job(job["job_id"], _batch_plan_output(job))

    unchanged_job = store.get_job(job["job_id"])
    unchanged_batch = store.find_optimization_batch(batch["batch_id"])
    assert unchanged_job["status"] == "queued"
    assert unchanged_job["raw_output_json"] is None
    assert unchanged_job["validated_output_json"] is None
    assert unchanged_job["completed_at"] is None
    assert unchanged_batch["status"] == "optimization_plan_queued"
    assert unchanged_batch["optimization_plan_error"] is None


def test_fail_batch_plan_job_rolls_back_when_batch_update_fails(tmp_path, monkeypatch):
    store, _ = _store(tmp_path)
    batch = _create_batch_with_completed_attribution(store)
    job = store.create_batch_plan_job(batch["batch_id"])

    def fail_batch_update(*args, **kwargs):
        raise RuntimeError("batch update failed")

    monkeypatch.setattr(store, "_update_batch_row", fail_batch_update)

    with pytest.raises(RuntimeError, match="batch update failed"):
        store.fail_job(job["job_id"], error_code="AGENT_RUNTIME_ERROR", message="failed")

    unchanged_job = store.get_job(job["job_id"])
    unchanged_batch = store.find_optimization_batch(batch["batch_id"])
    assert unchanged_job["status"] == "queued"
    assert unchanged_job["error_json"] is None
    assert unchanged_job["completed_at"] is None
    assert unchanged_batch["status"] == "optimization_plan_queued"
    assert unchanged_batch["optimization_plan_error"] is None


def test_batch_plan_lists_workspace_tasks_and_prepares_task_execution(tmp_path):
    store, _ = _store(tmp_path)
    batch = _create_batch_with_completed_attribution(store)

    batch = store.generate_batch_optimization_plan(batch["batch_id"])
    plan_task = batch["optimization_plan"]["tasks"][0]
    prepared = store.prepare_batch_plan_task_execution(batch["batch_id"], plan_task["plan_task_id"], comment="执行任务")

    assert plan_task["schema_version"] == "feedback-optimization-plan-task/v3"
    assert plan_task["execution_kind"] == "workspace_execution"
    assert plan_task["target_path"] == "CLAUDE.md"
    assert plan_task["description"]
    assert plan_task["objective"]
    assert plan_task["target_summary"] == "workspace:CLAUDE.md"
    assert "main_agent_claude_md" not in plan_task["title"]
    assert plan_task["recommended_actions"]
    assert plan_task["acceptance_criteria"]
    assert any("回归测试用例通过" in item for item in plan_task["acceptance_criteria"])
    assert all("execution-optimizer" not in item for item in plan_task["acceptance_criteria"])
    assert all("版本快照" not in item for item in plan_task["acceptance_criteria"])
    assert "归因依据" not in plan_task["recommendation"]
    assert "未读取当前配置文件" in plan_task["analysis_summary"]
    assert prepared["optimization_task"]["source"] == "feedback_optimization_batch"
    assert prepared["optimization_task"]["source_plan_task_id"] == plan_task["plan_task_id"]
    assert prepared["optimization_task"]["proposal"]["description"] == plan_task["description"]
    assert prepared["optimization_task"]["proposal"]["recommended_actions"] == plan_task["recommended_actions"]
    assert prepared["optimization_task"]["proposal"]["acceptance_criteria"] == plan_task["acceptance_criteria"]
    assert prepared["plan_task"]["optimization_task_id"] == prepared["optimization_task"]["optimization_task_id"]


def test_batch_plan_external_task_notifies_selected_webhook(tmp_path):
    store, settings = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["external_mcp_service"], comment="alert-0002 事件数据不全"))
    batch = store.create_optimization_batch([{"source_kind": "signal", "source_id": signal["signal_id"]}], title="外部 MCP 优化")
    attribution_job = store.create_attribution_job(batch["feedback_case_ids"][0])
    completed = store.complete_attribution_job(
        attribution_job["job_id"],
        {
            "feedback_case_id": batch["feedback_case_ids"][0],
            "attribution_job_id": attribution_job["job_id"],
            "status": "completed",
            "problem_type": "tool_data_quality",
            "optimization_object_type": "external_mcp_service",
            "actionability": "external_guidance",
            "confidence": "high",
            "human_review_required": False,
            "evidence_refs": [{"type": "evidence_file", "id": "tool_calls.json", "reason": "list_events 查询 alert-0002 时 event_time 与告警窗口不匹配"}],
            "responsibility_boundary": {"owner": "sec-ops-data", "reason": "sec-ops-data 的 list_events 接口需要支持按告警上下文返回事件"},
            "rationale": (
                "sec-ops-data MCP 工具 mcp__sec-ops-data__local_api__list_events_api_v1_events_get 查询 alert-0002 时，"
                "返回事件的 event_time 全部是 2026-02-11，无法支撑 2026-05-25 的告警研判。"
                "需要修复 list_events 接口或底层数据源，按 alert_id 和告警时间窗口返回完整事件。"
            ),
            "recommended_next_step": "generate_proposal",
        },
    )
    batch = store.record_batch_attribution_jobs(batch["batch_id"], [completed])
    batch = store.generate_batch_optimization_plan(batch["batch_id"])
    plan_task = batch["optimization_plan"]["tasks"][0]
    (settings.data_dir / "external-governance-webhooks.yaml").write_text(
        "webhooks:\n  - alias: kb\n    name: 知识库\n    url: http://example.invalid/kb\n",
        encoding="utf-8",
    )
    seen = {}

    def fake_sender(webhook, payload):
        seen["webhook"] = webhook
        seen["payload"] = payload
        return {"http_status": 202, "response_body": "accepted"}

    result = store.notify_batch_plan_task_external(batch["batch_id"], plan_task["plan_task_id"], webhook_alias="kb", sender=fake_sender)

    assert plan_task["execution_kind"] == "external_webhook"
    assert plan_task["schema_version"] == "feedback-optimization-plan-task/v3"
    assert plan_task["target_summary"] == "external:sec-ops-data"
    assert "external_mcp_service" not in plan_task["title"]
    assert "sec-ops-data" in plan_task["title"]
    assert "alert-0002" in plan_task["description"]
    assert "event_time" in plan_task["description"]
    assert plan_task["task_context"]["mcp_server"] == "sec-ops-data"
    assert plan_task["task_context"]["tool_name"] == "mcp__sec-ops-data__local_api__list_events_api_v1_events_get"
    assert plan_task["task_context"]["endpoint"] == "GET /api/v1/events"
    assert "alert-0002" in plan_task["task_context"]["query_ids"]
    assert "event_time" in plan_task["task_context"]["affected_fields"]
    assert plan_task["recommended_actions"]
    assert plan_task["acceptance_criteria"]
    assert any("mcp__sec-ops-data__local_api__list_events_api_v1_events_get" in item for item in plan_task["acceptance_criteria"])
    assert any("alert-0002" in item for item in plan_task["acceptance_criteria"])
    assert all("时 时" not in item for item in plan_task["acceptance_criteria"])
    assert all("Webhook" not in item for item in plan_task["acceptance_criteria"])
    assert all("2xx" not in item for item in plan_task["acceptance_criteria"])
    assert all("payload" not in item for item in plan_task["acceptance_criteria"])
    assert "归因依据" not in plan_task["recommendation"]
    assert seen["payload"]["batch_id"] == batch["batch_id"]
    assert seen["payload"]["plan_task_id"] == plan_task["plan_task_id"]
    assert seen["payload"]["title"] == plan_task["title"]
    assert seen["payload"]["description"] == plan_task["description"]
    assert seen["payload"]["objective"] == plan_task["objective"]
    assert seen["payload"]["target_summary"] == "external:sec-ops-data"
    assert seen["payload"]["task_context"]["mcp_server"] == "sec-ops-data"
    assert seen["payload"]["task_context"]["endpoint"] == "GET /api/v1/events"
    assert seen["payload"]["recommended_actions"] == plan_task["recommended_actions"]
    assert seen["payload"]["acceptance_criteria"] == plan_task["acceptance_criteria"]
    assert "alert-0002" in seen["payload"]["analysis_summary"]
    assert seen["payload"]["source_attribution_job_ids"] == [attribution_job["job_id"]]
    assert result["external_item"]["status"] == "notified"
    assert result["plan_task"]["external_item_id"] == result["external_item"]["external_item_id"]
    assert result["plan_task"]["latest_webhook_alias"] == "kb"
    with pytest.raises(ConflictError, match="已有外部通知结果"):
        store.create_batch_plan_job(batch["batch_id"], force=True, regeneration_instruction="重新生成外部任务")


def test_batch_plan_internal_action_promotes_batch_eval_cases(tmp_path):
    store, _ = _store(tmp_path)
    batch = _create_batch_with_completed_attribution(store)
    feedback_case = store.find_case(batch["feedback_case_ids"][0])
    eval_job = store.queue_feedback_eval_case_generation_agent_job(batch_id=batch["batch_id"], force=True)
    eval_result = store.complete_projected_agent_job(eval_job, _eval_case_generation_output(eval_job, feedback_case))
    eval_case = eval_result["validated_output_json"]["eval_cases"][0]
    batch = store.find_optimization_batch(batch["batch_id"])
    plan_job = store.create_batch_plan_job(batch["batch_id"], force=True)

    completed = store.complete_batch_plan_job(
        plan_job["job_id"],
        _batch_plan_output(
            plan_job,
            status="pending_execution",
            actionability="regression_asset_governance",
            target_type="eval_case",
            recommendation="晋级本批次候选评估用例。",
            expected_effect="后续回归计划可复用该反馈场景。",
            validation="检查评估用例状态和审计记录。",
            risk="误晋级会污染长期回归资产。",
            tasks=[
                {
                    "plan_task_id": "fopt-agent-wrong",
                    "execution_kind": "internal_action",
                    "internal_action": "promote_eval_cases",
                    "status": "completed",
                    "title": "将评估用例提升为活跃回归用例",
                    "description": "把候选用例纳入长期回归资产。",
                    "objective": "让同类反馈进入稳定回归验证。",
                    "target_type": "eval_case",
                    "actionability": "regression_asset_governance",
                    "recommendation": "晋级关联评估用例。",
                    "expected_effect": "后续版本回归可覆盖该反馈场景。",
                    "validation": "检查用例状态和审计记录。",
                    "risk": "用例质量不足时可能降低回归信号。",
                    "feedback_case_ids": ["fbc-agent-wrong"],
                    "eval_case_ids": ["evc-agent-wrong", eval_case["eval_case_id"]],
                    "attribution_job_ids": ["fba-agent-wrong"],
                }
            ],
            blocked_items=[],
        ),
    )
    plan = completed["validated_output_json"]
    plan_task = plan["tasks"][0]

    assert eval_case["status"] == "draft"
    assert eval_case["promotion_status"] == "candidate"
    assert plan_task["plan_task_id"] != "fopt-agent-wrong"
    assert plan_task["schema_version"] == "feedback-optimization-plan-task/v3"
    assert plan_task["execution_kind"] == "internal_action"
    assert plan_task["internal_action"] == "promote_eval_cases"
    assert plan_task["status"] == "pending_execution"
    assert plan_task["owner"] == "feedback_optimizer"
    assert plan_task["target_summary"] == "internal:promote_eval_cases"
    assert plan_task["eval_case_ids"] == batch["eval_case_ids"]
    assert plan["task_summary"]["internal_action"] == 1

    result = store._execute_batch_plan_task_internal_action(  # noqa: SLF001
        batch["batch_id"],
        plan_task["plan_task_id"],
        reason="批次内部动作测试",
    )
    promoted = store.find_eval_case(eval_case["eval_case_id"])
    revisions = store.list_eval_case_revisions(eval_case["eval_case_id"])
    events = store.list_eval_case_governance_events(eval_case["eval_case_id"])

    assert result["plan_task"]["status"] == "completed"
    assert result["plan_task"]["internal_action_result"]["operator"] == "feedback_optimizer"
    assert result["plan_task"]["internal_action_result"]["role"] == "system"
    assert result["plan_task"]["internal_action_result"]["updated_eval_case_ids"] == [eval_case["eval_case_id"]]
    assert promoted["status"] == "active"
    assert promoted["asset_layer"] == "core_regression"
    assert promoted["promotion_status"] == "approved"
    assert promoted["blocking_policy"] == "blocking_if_relevant"
    assert revisions[0]["created_by"] == "feedback_optimizer"
    assert revisions[0]["reason"] == "批次内部动作测试"
    assert events[0]["action"] == "promote"
    assert events[0]["operator"] == "feedback_optimizer"
    assert events[0]["role"] == "system"
    assert events[0]["before"]["status"] == "draft"
    assert events[0]["after"]["status"] == "active"
    with pytest.raises(ConflictError, match="已有内部执行结果"):
        store.create_batch_plan_job(batch["batch_id"], force=True, regeneration_instruction="重新生成内部任务")


def test_batch_plan_internal_action_rejects_eval_cases_outside_batch(tmp_path):
    store, _ = _store(tmp_path)
    batch = _create_batch_with_completed_attribution(store)
    feedback_case = store.find_case(batch["feedback_case_ids"][0])
    eval_job = store.queue_feedback_eval_case_generation_agent_job(batch_id=batch["batch_id"], force=True)
    eval_result = store.complete_projected_agent_job(eval_job, _eval_case_generation_output(eval_job, feedback_case))
    eval_case = eval_result["validated_output_json"]["eval_cases"][0]
    batch = store.find_optimization_batch(batch["batch_id"])
    plan = {
        "schema_version": "feedback-optimization-plan/v1",
        "optimization_plan_id": "fop-invalid-internal-action",
        "batch_id": batch["batch_id"],
        "created_at": batch["created_at"],
        "status": "pending_execution",
        "title": "非法内部动作方案",
        "actionability": "regression_asset_governance",
        "target_type": "eval_case",
        "recommendation": "尝试晋级非本批次用例。",
        "expected_effect": "不应执行。",
        "validation": "必须整体拒绝。",
        "risk": "跨批次污染回归资产。",
        "tasks": [
            {
                "plan_task_id": "fopt-invalid-internal-action",
                "execution_kind": "internal_action",
                "internal_action": "promote_eval_cases",
                "status": "pending_execution",
                "title": "晋级非法用例",
                "description": "混入非本批次 eval_case_id。",
                "objective": "验证内部动作边界。",
                "target_type": "eval_case",
                "actionability": "regression_asset_governance",
                "recommendation": "不应执行。",
                "expected_effect": "不应变更任何用例。",
                "validation": "检查用例仍为 draft/candidate。",
                "risk": "跨批次污染。",
                "eval_case_ids": [eval_case["eval_case_id"], "evc-outside-batch"],
            }
        ],
        "blocked_items": [],
    }
    store._update_batch(batch["batch_id"], status="pending_execution", fields={"optimization_plan": plan})  # noqa: SLF001

    with pytest.raises(BusinessRuleViolation, match="must belong"):
        store._execute_batch_plan_task_internal_action(batch["batch_id"], "fopt-invalid-internal-action")  # noqa: SLF001

    unchanged = store.find_eval_case(eval_case["eval_case_id"])
    assert unchanged["status"] == "draft"
    assert unchanged["promotion_status"] == "candidate"
    assert store.list_eval_case_governance_events(eval_case["eval_case_id"]) == []


def test_batch_plan_blocks_external_task_without_specific_object_context(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["external_mcp_service"], comment="MCP 数据不全"))
    batch = store.create_optimization_batch([{"source_kind": "signal", "source_id": signal["signal_id"]}], title="外部 MCP 优化")
    attribution_job = store.create_attribution_job(batch["feedback_case_ids"][0])
    completed = store.complete_attribution_job(
        attribution_job["job_id"],
        {
            "feedback_case_id": batch["feedback_case_ids"][0],
            "attribution_job_id": attribution_job["job_id"],
            "status": "completed",
            "problem_type": "tool_data_quality",
            "optimization_object_type": "external_mcp_service",
            "actionability": "external_guidance",
            "confidence": "medium",
            "human_review_required": False,
            "evidence_refs": [{"type": "evidence_file", "id": "tool_calls.json", "reason": "MCP 返回字段缺失"}],
            "responsibility_boundary": {"owner": "knowledge-base-mcp", "reason": "外部 MCP 服务需要补齐字段"},
            "rationale": "知识库 MCP 返回的数据字段不足，需要外部服务侧处理。",
            "recommended_next_step": "generate_proposal",
        },
    )
    batch = store.record_batch_attribution_jobs(batch["batch_id"], [completed])
    batch = store.generate_batch_optimization_plan(batch["batch_id"])
    plan = batch["optimization_plan"]

    assert plan["tasks"] == []
    assert plan["task_summary"]["total"] == 0
    assert len(plan["blocked_items"]) == 1
    assert "证据不足以定位具体外部对象" in plan["blocked_items"][0]["reason"]


def test_batch_plan_puts_non_executable_results_in_blocked_items(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["not_actionable"], comment="无法优化"))
    batch = store.create_optimization_batch([{"source_kind": "signal", "source_id": signal["signal_id"]}], title="不可执行批次")
    attribution_job = store.create_attribution_job(batch["feedback_case_ids"][0])
    completed = store.complete_attribution_job(
        attribution_job["job_id"],
        {
            "feedback_case_id": batch["feedback_case_ids"][0],
            "attribution_job_id": attribution_job["job_id"],
            "status": "needs_human_review",
            "problem_type": "insufficient_information",
            "optimization_object_type": "not_actionable",
            "actionability": "needs_human_analysis",
            "confidence": "low",
            "human_review_required": True,
            "evidence_refs": [{"type": "evidence_file", "id": "feedback.json", "reason": "反馈信息不足"}],
            "responsibility_boundary": {"owner": "developer", "reason": "缺少可落地优化目标"},
            "rationale": "当前反馈没有足够上下文，不能形成 workspace 或外部系统优化任务。",
            "recommended_next_step": "needs_human_review",
        },
    )
    batch = store.record_batch_attribution_jobs(batch["batch_id"], [completed])
    batch = store.generate_batch_optimization_plan(batch["batch_id"])
    plan = batch["optimization_plan"]

    assert batch["status"] == "needs_human_review"
    assert plan["tasks"] == []
    assert plan["task_summary"]["total"] == 0
    assert len(plan["blocked_items"]) == 1
    assert plan["blocked_items"][0]["blocked_item_id"].startswith("fobi-")
    assert plan["blocked_items"][0]["attribution_job_ids"] == [attribution_job["job_id"]]
    assert "未指向可由当前 workspace" in plan["blocked_items"][0]["reason"]


def test_legacy_manual_plan_is_exposed_as_blocked_item_not_task(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["not_actionable"], comment="历史数据"))
    batch = store.create_optimization_batch([{"source_kind": "signal", "source_id": signal["signal_id"]}], title="历史批次")
    legacy_plan = {
        "schema_version": "feedback-optimization-plan/v1",
        "optimization_plan_id": "fop-legacy-manual",
        "batch_id": batch["batch_id"],
        "created_at": "2026-05-20T00:00:00+00:00",
        "status": "needs_human_review",
        "title": "历史不可执行方案",
        "actionability": "needs_human_analysis",
        "target_type": "not_actionable",
        "target_path": None,
        "recommendation": "历史方案没有可执行任务。",
        "no_action_reason": "历史方案需要人工分析。",
    }

    updated = store._update_batch(batch["batch_id"], status="needs_human_review", fields={"optimization_plan": legacy_plan})  # noqa: SLF001
    plan = updated["optimization_plan"]

    assert plan["tasks"] == []
    assert plan["task_summary"]["total"] == 0
    assert len(plan["blocked_items"]) == 1
    assert plan["blocked_items"][0]["blocked_item_id"] == "fopt-legacy-fop-legacy-manual"
    assert plan["blocked_items"][0]["reason"] == "历史方案需要人工分析。"


def test_batch_plan_regeneration_allows_legacy_approved_unapplied_plan(tmp_path):
    store, _ = _store(tmp_path)
    batch, task_id = _batch_with_unapplied_pending_execution_task(store)
    legacy_plan = {**batch["optimization_plan"], "status": "approved"}
    with store.Session.begin() as db:
        row = db.get(FeedbackOptimizationBatchModel, batch["batch_id"])
        assert row is not None
        row.status = "approved"
        row.payload_json = {**batch, "status": "approved", "optimization_plan": legacy_plan}

    job = store.create_batch_plan_job(batch["batch_id"], force=True, regeneration_instruction="重新改写目标")
    updated = store.find_optimization_batch(batch["batch_id"])

    assert job["job_type"] == "batch_plan"
    assert updated["status"] == "optimization_plan_queued"
    assert updated["optimization_plan"] is None
    assert store.find_task(task_id) is None
