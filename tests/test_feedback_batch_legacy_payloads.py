from app.runtime.runtime_db import FeedbackOptimizationBatchModel

from feedback_store_test_utils import _store


def test_list_batches_sanitizes_legacy_internal_action_payload(tmp_path):
    store, _ = _store(tmp_path)
    batch_id = "fob-legacy-internal-action"
    now = "2026-06-16T00:00:00+00:00"
    legacy_result = {
        "plan_task_id": "fopt-internal",
        "execution_kind": "internal_action",
        "internal_action": "promote_eval_cases",
        "status": "completed",
        "started_at": now,
        "completed_at": now,
    }
    payload = {
        "schema_version": "feedback-optimization-batch/v1",
        "batch_id": batch_id,
        "created_at": now,
        "updated_at": now,
        "status": "applied_pending_regression",
        "title": "历史内部动作批次",
        "source_refs": [{"source_kind": "signal", "source_id": "sig-1"}],
        "feedback_case_ids": ["case-1"],
        "optimization_plan": {
            "optimization_plan_id": "fop-legacy",
            "status": "approved",
            "title": "历史内部动作方案",
            "tasks": [
                {
                    "plan_task_id": "fopt-workspace",
                    "source_index": 1,
                    "execution_kind": "workspace_execution",
                    "status": "completed",
                    "title": "更新 CLAUDE.md",
                    "target_type": "main_agent_claude_md",
                    "target_path": "CLAUDE.md",
                    "actionability": "direct_workspace_change",
                },
                {
                    "plan_task_id": "fopt-internal",
                    "source_index": 2,
                    "execution_kind": "internal_action",
                    "internal_action": "promote_eval_cases",
                    "status": "completed",
                    "title": "晋级评估用例",
                    "target_type": "eval_case",
                    "actionability": "regression_asset_governance",
                    "eval_case_ids": ["eval-1"],
                },
            ],
            "task_summary": {"total": 2, "workspace_execution": 1, "external_webhook": 0, "internal_action": 1},
            "blocked_items": [],
            "blocked_summary": {"total": 0},
        },
        "execution_runs": [
            {
                "execution_run_id": "fbx-legacy",
                "batch_id": batch_id,
                "created_at": now,
                "started_at": now,
                "completed_at": now,
                "status": "completed",
                "task_results": [
                    {
                        "plan_task_id": "fopt-workspace",
                        "execution_kind": "workspace_execution",
                        "status": "completed",
                        "started_at": now,
                        "completed_at": now,
                        "execution_job_id": "job-1",
                    },
                    legacy_result,
                ],
            }
        ],
        "latest_execution_run": {
            "execution_run_id": "fbx-legacy",
            "batch_id": batch_id,
            "created_at": now,
            "started_at": now,
            "completed_at": now,
            "status": "completed",
            "task_results": [legacy_result],
        },
    }
    with store.Session.begin() as db:
        db.add(
            FeedbackOptimizationBatchModel(
                batch_id=batch_id,
                created_at=now,
                updated_at=now,
                status="applied_pending_regression",
                title="历史内部动作批次",
                payload_json=payload,
            )
        )

    batch = store.list_optimization_batches()[0]
    plan = batch["optimization_plan"]

    assert [item["execution_kind"] for item in plan["tasks"]] == ["workspace_execution"]
    assert "internal_action" not in plan["task_summary"]
    assert plan["blocked_items"][0]["blocked_item_id"] == "fopt-internal"
    assert "历史内部动作 promote_eval_cases 已停用" in plan["blocked_items"][0]["reason"]
    assert batch["execution_runs"][0]["task_results"][0]["plan_task_id"] == "fopt-workspace"
    assert len(batch["execution_runs"][0]["task_results"]) == 1
    assert "已隐藏 1 条停用的历史内部动作执行结果。" in batch["latest_execution_run"]["warnings"]
    assert batch["latest_execution_run"]["status"] == "partial_failed"
