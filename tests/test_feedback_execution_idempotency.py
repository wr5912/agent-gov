from feedback_store_test_utils import _create_approved_task_for_target, _store, pytest

from app.runtime.errors import ConflictError


def test_mark_execution_job_applied_rejects_duplicate_application(tmp_path):
    store, _ = _store(tmp_path)
    task = _create_approved_task_for_target(store, "CLAUDE.md")
    job = store.create_execution_job(task["optimization_task_id"])
    store.start_execution_job(job["execution_job_id"])
    ready_job = store.complete_execution_job(
        job["execution_job_id"],
        {
            "schema_version": "execution-plan-output/v1",
            "optimization_task_id": task["optimization_task_id"],
            "execution_job_id": job["execution_job_id"],
            "status": "ready",
            "baseline_agent_version_id": task["baseline_agent_version_id"],
            "summary": "追加配置读取要求。",
            "operations": [
                {
                    "operation": "append_text",
                    "path": "CLAUDE.md",
                    "append_text": "\n配置读取要求。\n",
                    "rationale": "测试重复应用。",
                }
            ],
            "validation": "复测反馈场景。",
            "risk": "测试风险。",
            "human_review_required": True,
        },
    )

    first = store.mark_execution_job_applied(
        ready_job["execution_job_id"],
        pre_execution_version={"agent_version_id": "main-v-before"},
        applied_agent_version={"agent_version_id": "main-v-after"},
        applied_diff={"changed_files": ["CLAUDE.md"]},
    )
    with pytest.raises(ConflictError, match="already been applied|not ready"):
        store.mark_execution_job_applied(
            ready_job["execution_job_id"],
            pre_execution_version={"agent_version_id": "main-v-before-2"},
            applied_agent_version={"agent_version_id": "main-v-after-2"},
            applied_diff={"changed_files": ["CLAUDE.md"]},
        )

    updated_job = store.get_execution_job(ready_job["execution_job_id"])
    updated_task = store.find_task(task["optimization_task_id"])
    assert first["applied_agent_version_id"] == "main-v-after"
    assert updated_job["applied_agent_version_id"] == "main-v-after"
    assert updated_task["applied_agent_version_id"] == "main-v-after"
