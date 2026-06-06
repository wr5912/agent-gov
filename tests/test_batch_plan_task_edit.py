import pytest
from app.runtime.errors import ConflictError
from app.runtime.feedback_schemas import ExecutionPlanFormatterOutput
from fastapi.testclient import TestClient

from feedback_store_test_utils import _create_batch_with_completed_attribution, _store
from test_api_execution_optimizer import _load_app
from test_external_governance_store_records import _external_plan_task as _external_plan_task_fixture


def _plan_task(batch: dict, plan_task_id: str | None = None) -> dict:
    tasks = batch["optimization_plan"]["tasks"]
    if plan_task_id is None:
        return tasks[0]
    for task in tasks:
        if task["plan_task_id"] == plan_task_id:
            return task
    raise AssertionError(f"plan task not found: {plan_task_id}")


def test_edit_plan_task_updates_future_optimization_task_snapshot(tmp_path):
    store, _settings = _store(tmp_path)
    batch = store.generate_batch_optimization_plan(_create_batch_with_completed_attribution(store)["batch_id"])
    plan_task = _plan_task(batch)

    updated = store.update_batch_plan_task(
        batch["batch_id"],
        plan_task["plan_task_id"],
        {
            "title": "人工修订任务",
            "description": "优先修改 MCP 配置说明。",
            "target_path": ".mcp.json",
            "recommended_actions": ["读取现有 MCP 配置", "补充缺失约束"],
            "acceptance_criteria": ["执行方案只修改 .mcp.json"],
            "task_context": {"target_file": ".mcp.json", "config_section": "mcpServers"},
            "edit_note": "人工确认目标文件应为 MCP 配置。",
        },
    )
    assert updated is not None
    edited_task = updated.plan_task
    prepared = store.prepare_batch_plan_task_execution(batch["batch_id"], plan_task["plan_task_id"])
    optimization_task = prepared["optimization_task"]

    assert edited_task["title"] == "人工修订任务"
    assert edited_task["target_path"] == ".mcp.json"
    assert edited_task["edit_note"] == "人工确认目标文件应为 MCP 配置。"
    assert optimization_task["target_paths"] == [".mcp.json"]
    assert optimization_task["proposal"]["description"] == "优先修改 MCP 配置说明。"
    assert optimization_task["proposal"]["recommended_actions"] == ["读取现有 MCP 配置", "补充缺失约束"]
    assert optimization_task["proposal"]["acceptance_criteria"] == ["执行方案只修改 .mcp.json"]
    assert optimization_task["proposal"]["task_context"]["target_file"] == ".mcp.json"


def test_edit_existing_task_invalidates_stale_execution_job(tmp_path):
    store, _settings = _store(tmp_path)
    batch = store.generate_batch_optimization_plan(_create_batch_with_completed_attribution(store)["batch_id"])
    plan_task = _plan_task(batch)
    task = store.prepare_batch_plan_task_execution(batch["batch_id"], plan_task["plan_task_id"])["optimization_task"]
    job = store.create_execution_job(task["optimization_task_id"])
    store.start_execution_job(job["execution_job_id"])
    store.complete_execution_job(
        job["execution_job_id"],
        ExecutionPlanFormatterOutput.model_validate(
            {
                "status": "ready",
                "summary": "追加说明。",
                "operations": [{"operation": "append_text", "path": "CLAUDE.md", "append_text": "\n补充约束。\n", "rationale": "测试"}],
                "validation": "复测。",
                "risk": "低。",
                "human_review_required": True,
            }
        ),
    )

    updated = store.update_batch_plan_task(
        batch["batch_id"],
        plan_task["plan_task_id"],
        {"description": "人工修订后需要重新生成执行方案。"},
    )
    assert updated is not None
    edited_task = updated.optimization_task
    assert edited_task is not None
    edited_batch_task = _plan_task(updated.batch, plan_task["plan_task_id"])

    assert updated.invalidated_execution_job_ids == [job["execution_job_id"]]
    assert store.get_execution_job(job["execution_job_id"]) is None
    assert edited_task["status"] == "pending_execution"
    assert edited_task["latest_execution_job_id"] is None
    assert edited_task["latest_execution_job"] is None
    assert edited_task["proposal"]["description"] == "人工修订后需要重新生成执行方案。"
    assert edited_batch_task.get("execution_job_id") is None
    assert edited_batch_task["status"] == "pending_execution"


def test_edit_rejects_running_execution_job(tmp_path):
    store, _settings = _store(tmp_path)
    batch = store.generate_batch_optimization_plan(_create_batch_with_completed_attribution(store)["batch_id"])
    plan_task = _plan_task(batch)
    task = store.prepare_batch_plan_task_execution(batch["batch_id"], plan_task["plan_task_id"])["optimization_task"]
    store.create_execution_job(task["optimization_task_id"])

    with pytest.raises(ConflictError, match="still running"):
        store.update_batch_plan_task(batch["batch_id"], plan_task["plan_task_id"], {"description": "运行中不能编辑。"})


def test_edit_rejects_applied_optimization_task(tmp_path):
    store, _settings = _store(tmp_path)
    batch = store.generate_batch_optimization_plan(_create_batch_with_completed_attribution(store)["batch_id"])
    plan_task = _plan_task(batch)
    task = store.prepare_batch_plan_task_execution(batch["batch_id"], plan_task["plan_task_id"])["optimization_task"]
    store.mark_task_applied(task["optimization_task_id"], agent_version={"agent_version_id": "main-v-applied"})

    with pytest.raises(ConflictError, match="already been applied"):
        store.update_batch_plan_task(batch["batch_id"], plan_task["plan_task_id"], {"description": "已应用不能编辑。"})


def test_edit_failed_external_task_resets_notification_payload(tmp_path):
    store, settings = _store(tmp_path)
    batch, _plan, plan_task = _external_plan_task_fixture(store)
    (settings.data_dir / "external-governance-webhooks.yaml").write_text(
        "webhooks:\n  - alias: sec-ops-data\n    name: SecOps Data\n    url: http://example.invalid/sec-ops-data\n",
        encoding="utf-8",
    )
    failed = store.notify_batch_plan_task_external(
        batch["batch_id"],
        plan_task["plan_task_id"],
        webhook_alias="sec-ops-data",
        sender=lambda webhook, payload: {"http_status": 500, "response_body": "failed"},
    )
    failed_task = failed["plan_task"]

    updated = store.update_batch_plan_task(
        batch["batch_id"],
        failed_task["plan_task_id"],
        {
            "owner": "knowledge-base",
            "recommendation": "改为由知识库补齐字段说明。",
            "task_context": {"external_system": "knowledge-base", "affected_fields": ["event_time"]},
        },
    )
    assert updated is not None
    external_item = updated.external_item
    assert external_item is not None

    assert updated.plan_task["status"] == "pending_notification"
    assert updated.plan_task["owner"] == "knowledge-base"
    assert external_item["status"] == "pending_notification"
    assert external_item["owner"] == "knowledge-base"
    assert external_item["recommendation"] == "改为由知识库补齐字段说明。"
    assert external_item["latest_notification_id"] is None
    assert external_item["latest_notification"] is None


def test_edit_plan_task_api_returns_updated_batch(monkeypatch, tmp_path):
    module = _load_app(monkeypatch, tmp_path)
    batch = module.feedback_store.generate_batch_optimization_plan(_create_batch_with_completed_attribution(module.feedback_store)["batch_id"])
    plan_task = _plan_task(batch)

    with TestClient(module.app) as client:
        response = client.patch(
            f"/api/feedback-optimization-batches/{batch['batch_id']}/optimization-plan/tasks/{plan_task['plan_task_id']}",
            json={
                "title": "API 人工修订任务",
                "description": "API 保存后的任务描述。",
                "acceptance_criteria": ["API 返回更新后的任务"],
            },
        )

    assert response.status_code == 200, response.json()
    payload = response.json()
    assert payload["plan_task"]["title"] == "API 人工修订任务"
    assert payload["batch"]["optimization_plan"]["tasks"][0]["description"] == "API 保存后的任务描述。"
    assert payload["invalidated_execution_job_ids"] == []
