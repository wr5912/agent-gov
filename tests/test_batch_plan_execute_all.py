from __future__ import annotations

import asyncio

import pytest
from app.runtime.agent_git_store import GitAgentVersionStore
from app.runtime.errors import BusinessRuleViolation, ConflictError, MainWorkspaceDirtyError
from app.runtime.feedback_batch_execution_request_schemas import (
    FeedbackOptimizationBatchExecuteAllRequest,
    FeedbackOptimizationBatchExecutionRollbackRequest,
)
from app.runtime.stores.feedback_store import FeedbackStore
from app.services.agent_governance import AgentGovernanceService
from app.services.batch_optimization_execution import BatchOptimizationExecutionService
from app.services.execution_application import ExecutionApplicationService

from feedback_store_test_utils import FeedbackSignalCreateRequest, _attribution_output, _batch_plan_output, _record_run, _settings


class RuntimeStub:
    def __init__(self, store: FeedbackStore) -> None:
        self.store = store

    async def run_execution_job(self, optimization_task_id: str, *, force: bool = False):
        task = self.store.find_task(optimization_task_id)
        assert task
        target_path = task["target_paths"][0]
        job = self.store.create_execution_job(optimization_task_id, force=force)
        assert job
        self.store.start_execution_job(job["execution_job_id"])
        return self.store.complete_execution_job(
            job["execution_job_id"],
            {
                "optimization_task_id": optimization_task_id,
                "execution_job_id": job["execution_job_id"],
                "status": "ready",
                "baseline_agent_version_id": task["baseline_agent_version_id"],
                "summary": f"追加 {target_path} 优化要求。",
                "operations": [
                    {
                        "operation": "append_text",
                        "path": target_path,
                        "append_text": f"\n# one-click execution: {target_path}\n",
                        "rationale": "测试一键执行批次级应用。",
                    }
                ],
                "validation": "检查候选 diff。",
                "risk": "测试风险。",
                "human_review_required": True,
            },
        )


class RuntimeShouldNotRun:
    async def run_execution_job(self, optimization_task_id: str, *, force: bool = False):  # noqa: ARG002
        raise AssertionError("runtime should not run when main workspace is dirty")


def _service(tmp_path):
    settings = _settings(tmp_path)
    agent_store = GitAgentVersionStore(
        repository_dir=settings.main_workspace_dir,
        worktrees_dir=settings.agent_git_worktrees_dir,
        releases_dir=settings.agent_release_archives_dir,
    )
    agent_store.ensure_bootstrap()
    store = FeedbackStore(
        data_dir=settings.data_dir,
        workspace_dir=settings.main_workspace_dir,
        agent_version_provider=agent_store.current_version_id,
    )
    governance = AgentGovernanceService(feedback_store=store, agent_version_store=agent_store)
    application = ExecutionApplicationService(
        settings=settings,
        feedback_store=store,
        agent_version_store=agent_store,
        agent_governance=governance,
    )
    return (
        BatchOptimizationExecutionService(
            feedback_store=store,
            runtime=RuntimeStub(store),  # type: ignore[arg-type]
            execution_application=application,
        ),
        store,
        agent_store,
    )


def _batch_with_plan_tasks(store: FeedbackStore, tasks: list[dict]):
    _record_run(store)
    signal = store.create_signal(
        FeedbackSignalCreateRequest(
            run_id="run-1",
            labels=["tool_data_incomplete"],
            comment="批次一键执行测试",
        )
    )
    feedback_case = store.create_case(source_ids=[signal["signal_id"]], title="批次一键执行测试")
    attribution_job = store.create_attribution_job(feedback_case["feedback_case_id"])
    completed_attribution = store.complete_attribution_job(
        attribution_job["job_id"],
        _attribution_output(attribution_job),
    )
    batch = store.ensure_single_case_optimization_batch(feedback_case["feedback_case_id"])
    if batch.get("eval_case_generation_job_id"):
        store._discard_job(batch["eval_case_generation_job_id"])  # noqa: SLF001 - keep this service test focused.
    store.record_batch_attribution_jobs(batch["batch_id"], [completed_attribution])
    plan_job = store.create_batch_plan_job(batch["batch_id"])
    completed = store.complete_batch_plan_job(
        plan_job["job_id"],
        _batch_plan_output(
            plan_job,
            status="pending_execution",
            actionability="direct_workspace_change",
            target_type="main_agent_claude_md",
            target_path="CLAUDE.md",
            tasks=tasks,
            blocked_items=[],
        ),
    )
    return store.find_optimization_batch(completed["scope_id"] if completed and completed.get("scope_id") else batch["batch_id"])


def _workspace_plan_task(target_path: str) -> dict:
    return {
        "execution_kind": "workspace_execution",
        "status": "pending_execution",
        "title": f"修改 {target_path}",
        "description": f"按反馈调整 {target_path}。",
        "objective": "提高反馈场景表现。",
        "target_summary": target_path,
        "target_type": "main_agent_claude_md",
        "target_path": target_path,
        "owner": "main_agent_workspace",
        "actionability": "direct_workspace_change",
        "recommendation": f"按反馈调整 {target_path}。",
        "recommended_actions": [f"修改 {target_path}"],
        "acceptance_criteria": ["复测反馈场景通过"],
        "expected_effect": "提高反馈场景表现。",
        "validation": "复测反馈场景。",
        "risk": "需确认文件内容变更符合预期。",
        "task_context": {"target_file": target_path},
    }


def test_execute_all_workspace_tasks_creates_one_agent_version(tmp_path):
    service, store, _agent_store = _service(tmp_path)
    batch = _batch_with_plan_tasks(store, [_workspace_plan_task("CLAUDE.md"), _workspace_plan_task(".mcp.json")])

    response = asyncio.run(
        service.execute_all(
            batch["batch_id"],
            FeedbackOptimizationBatchExecuteAllRequest(force=True),
        )
    )

    run = response.execution_run
    updated = store.find_optimization_batch(batch["batch_id"])
    version_ids = {result.applied_agent_version_id for result in run.task_results if result.execution_kind == "workspace_execution"}
    plan_tasks = updated["optimization_plan"]["tasks"]
    task_ids = [item["optimization_task_id"] for item in plan_tasks]

    assert run.status == "completed"
    assert run.applied_agent_version_id
    assert len(version_ids) == 1
    assert version_ids == {run.applied_agent_version_id}
    assert {item["status"] for item in plan_tasks} == {"applied_pending_regression"}
    assert {store.find_task(task_id)["applied_agent_version_id"] for task_id in task_ids} == {run.applied_agent_version_id}
    assert updated["latest_execution_run"]["execution_run_id"] == run.execution_run_id
    assert run.applied_diff is not None
    assert len(run.applied_diff.modified) == 2


def test_execute_all_rejects_invalid_task_before_creating_run(tmp_path):
    service, store, _agent_store = _service(tmp_path)
    task = {
        **_workspace_plan_task("main-workspace 根目录下的 .mcp.json 文件"),
        "actionability": "runtime_fix",
        "task_context": {"target_file": ".mcp.json"},
    }
    batch = _batch_with_plan_tasks(store, [task])

    with pytest.raises(ConflictError, match="actionability"):
        asyncio.run(service.execute_all(batch["batch_id"], FeedbackOptimizationBatchExecuteAllRequest(force=True)))

    assert store.latest_batch_execution_run(batch["batch_id"]) is None
    assert not store.list_tasks()


def test_execute_all_rejects_dirty_workspace_before_creating_run(tmp_path):
    service, store, agent_store = _service(tmp_path)
    service.runtime = RuntimeShouldNotRun()  # type: ignore[assignment]
    batch = _batch_with_plan_tasks(store, [_workspace_plan_task("CLAUDE.md")])
    agent_store.repository_dir.joinpath("CLAUDE.md").write_text("# manual edit\n", encoding="utf-8")

    with pytest.raises(MainWorkspaceDirtyError) as exc_info:
        asyncio.run(service.execute_all(batch["batch_id"], FeedbackOptimizationBatchExecuteAllRequest(force=True)))

    assert "uncommitted changes" in str(exc_info.value)
    assert exc_info.value.error_code == "MAIN_WORKSPACE_DIRTY"
    assert exc_info.value.error_details
    assert exc_info.value.error_details["changed_files"][0]["path"] == "CLAUDE.md"
    assert store.latest_batch_execution_run(batch["batch_id"]) is None
    assert not store.list_tasks()


def test_execute_all_requires_webhook_alias_for_external_tasks(tmp_path):
    service, store, _agent_store = _service(tmp_path)
    external_task = {
        **_workspace_plan_task("CLAUDE.md"),
        "execution_kind": "external_webhook",
        "target_type": "external_mcp_service",
        "target_path": None,
        "owner": "mcp_config",
        "actionability": "external_guidance",
        "task_context": {"mcp_server": "sec-ops", "tool_name": "query_alert", "query_ids": ["alert-1"]},
    }
    batch = _batch_with_plan_tasks(store, [external_task])

    with pytest.raises(BusinessRuleViolation, match="Webhook alias"):
        asyncio.run(service.execute_all(batch["batch_id"], FeedbackOptimizationBatchExecuteAllRequest(force=True)))

    assert store.latest_batch_execution_run(batch["batch_id"]) is None


def test_rollback_execute_all_resets_workspace_task_projection(tmp_path):
    service, store, agent_store = _service(tmp_path)
    batch = _batch_with_plan_tasks(store, [_workspace_plan_task("CLAUDE.md")])
    response = asyncio.run(service.execute_all(batch["batch_id"], FeedbackOptimizationBatchExecuteAllRequest(force=True)))
    run = response.execution_run
    plan_task_id = run.task_results[0].plan_task_id
    task_id = run.task_results[0].optimization_task_id

    rollback = service.rollback(
        batch["batch_id"],
        run.execution_run_id,
        FeedbackOptimizationBatchExecutionRollbackRequest(note="测试回滚"),
    )

    updated = store.find_optimization_batch(batch["batch_id"])
    plan_task = next(item for item in updated["optimization_plan"]["tasks"] if item["plan_task_id"] == plan_task_id)
    task = store.find_task(task_id)

    assert rollback.execution_run.status == "rolled_back"
    assert rollback.execution_run.rollback_result.status == "restored"
    assert agent_store.current_version_id() == run.pre_execution_agent_version_id
    assert updated["status"] == "pending_execution"
    assert plan_task["status"] == "pending_execution"
    assert plan_task.get("applied_agent_version_id") is None
    assert task["status"] == "pending_execution"
    assert task.get("applied_agent_version_id") is None
