from __future__ import annotations

import pytest
from app.runtime.agent_job_types import coerce_agent_job_type
from app.runtime.runtime_db import AgentJobModel
from pydantic import ValidationError
from sqlalchemy import text

from feedback_store_test_utils import _store


def _historical_job(job_id: str, *, status: str = "completed") -> AgentJobModel:
    return AgentJobModel(
        job_id=job_id,
        job_type="attribution",
        scope_kind="improvement",
        scope_id="imp-1",
        status=status,
        profile_name="governor",
        created_at="2026-07-01T00:00:00+00:00",
        started_at="2026-07-01T00:00:01+00:00",
        completed_at="2026-07-01T00:00:02+00:00",
        input_path="",
        raw_output_path=f"sqlite://agent_jobs/{job_id}/raw_output_json",
        validated_output_path=f"sqlite://agent_jobs/{job_id}/validated_output_json",
        error_path=f"sqlite://agent_jobs/{job_id}/error_json",
        runtime_version="test",
        schema_version="attribution-agent-job/v1",
        timeout_seconds=300,
        retry_count=0,
        input_json={"feedback_case_id": "fbc-1"},
    )


def test_historical_agent_jobs_remain_read_only_and_queryable(tmp_path) -> None:
    store, _ = _store(tmp_path)
    with store.Session.begin() as db:
        db.add(_historical_job("job-history"))

    assert store.get_agent_job("job-history")["status"] == "completed"
    assert [job["job_id"] for job in store.list_agent_jobs(status="completed")] == ["job-history"]
    for removed_write_method in (
        "create_agent_job",
        "claim_next_agent_job",
        "fail_agent_job",
        "complete_projected_agent_job",
    ):
        assert not hasattr(store, removed_write_method)


def test_agent_job_projection_rejects_invalid_persisted_status(tmp_path) -> None:
    store, _ = _store(tmp_path)
    with store.Session.begin() as db:
        db.add(_historical_job("job-invalid-status"))
    with store.Session.begin() as db:
        db.execute(
            text("UPDATE agent_jobs SET status = 'unknown_status' WHERE job_id = 'job-invalid-status'")
        )

    with pytest.raises(ValidationError):
        store.get_agent_job("job-invalid-status")


def test_removed_eval_case_generation_job_type_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unsupported agent job type"):
        coerce_agent_job_type("eval_case_generation")
