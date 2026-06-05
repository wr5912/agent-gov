from feedback_store_test_utils import (
    ValidationError,
    _create_approved_task_for_target,
    _store,
    pytest,
)
from sqlalchemy import select

from app.runtime.runtime_db import OptimizationTaskModel


def test_execution_application_rejects_invalid_persisted_task_payload(tmp_path):
    store, _ = _store(tmp_path)
    task = _create_approved_task_for_target(store, "CLAUDE.md")
    job = store.create_execution_job(task["optimization_task_id"])
    store.start_execution_job(job["execution_job_id"])
    completed = store.complete_execution_job(
        job["execution_job_id"],
        {
            "optimization_task_id": task["optimization_task_id"],
            "execution_job_id": job["execution_job_id"],
            "status": "ready",
            "baseline_agent_version_id": task["baseline_agent_version_id"],
            "summary": "append controlled instruction",
            "operations": [
                {
                    "operation": "append_text",
                    "path": "CLAUDE.md",
                    "append_text": "\nrecord projection check\n",
                    "rationale": "exercise execution application task projection",
                }
            ],
            "validation": "focused regression",
            "risk": "low",
            "human_review_required": True,
        },
    )

    with store.Session.begin() as db:
        row = db.scalars(select(OptimizationTaskModel)).one()
        row.payload_json = {**row.payload_json, "target_paths": []}

    with pytest.raises(ValidationError):
        store.record_execution_application_applied(
            completed["execution_job_id"],
            pre_execution_version={"agent_version_id": "main-before"},
            applied_agent_version={"agent_version_id": "main-after"},
        )
