from __future__ import annotations

import asyncio
import logging

import pytest
from app.runtime.agent_job_errors import AGENT_AUTH_REQUIRED, AgentAuthenticationRequiredError
from app.runtime.agent_job_types import agent_job_spec, coerce_agent_job_type
from app.runtime.runtime_db import AgentJobModel
from app.services.agent_job_worker import AgentJobWorker
from pydantic import ValidationError
from sqlalchemy import text

from feedback_store_test_utils import _store


def _queue_job(store, job_id: str):
    spec = agent_job_spec("attribution")
    return store.create_agent_job(
        job_id=job_id,
        job_type=spec.job_type,
        scope_kind="improvement",
        scope_id="imp-1",
        profile_name=spec.profile_name,
        input_payload={"feedback_case_id": "fbc-1"},
    )


def _expire_claimed_job(store, job_id: str) -> None:
    with store.Session.begin() as db:
        row = db.get(AgentJobModel, job_id)
        assert row is not None
        row.started_at = "2026-01-01T00:00:00+00:00"
        row.timeout_seconds = 1


def test_agent_job_claim_is_single_consumer(tmp_path) -> None:
    store, _ = _store(tmp_path)
    _queue_job(store, "job-single-consumer")

    claimed = store.claim_next_agent_job()

    assert claimed is not None
    assert claimed["job_id"] == "job-single-consumer"
    assert claimed["status"] == "running"
    assert store.claim_next_agent_job() is None


def test_agent_job_default_timeout_uses_store_governance_timeout(tmp_path) -> None:
    store, _ = _store(tmp_path)
    store.agent_job_timeout_seconds = 123

    assert _queue_job(store, "job-configured-timeout")["timeout_seconds"] == 123


def test_stale_running_agent_job_times_out_and_next_job_can_claim(tmp_path) -> None:
    store, _ = _store(tmp_path)
    _queue_job(store, "job-stale-running")
    claimed = store.claim_next_agent_job()
    assert claimed is not None
    _queue_job(store, "job-next-queued")
    _expire_claimed_job(store, claimed["job_id"])

    timed_out = store._timeout_stale_agent_jobs()
    next_claimed = store.claim_next_agent_job()

    assert [job["job_id"] for job in timed_out] == ["job-stale-running"]
    assert timed_out[0]["status"] == "timeout"
    assert timed_out[0]["error_json"]["error_code"] == "AGENT_TIMEOUT"
    assert next_claimed is not None and next_claimed["job_id"] == "job-next-queued"


def test_agent_job_worker_logs_claim_and_runtime_failure(tmp_path, caplog) -> None:
    store, _ = _store(tmp_path)
    _queue_job(store, "job-worker-fails")

    async def fail_runtime(**_kwargs):
        raise RuntimeError("formatter crashed")

    caplog.set_level(logging.INFO, logger="app.services.agent_job_worker")
    worker = AgentJobWorker(
        feedback_store=store,
        run_profile_json=fail_runtime,
        poll_interval_seconds=0,
        worker_instance="test-worker",
    )

    result = asyncio.run(worker.run_once())
    messages = "\n".join(record.getMessage() for record in caplog.records)

    assert result is not None and result.status == "failed"
    assert result.error_json is not None and result.error_json.error_code == "AGENT_RUNTIME_ERROR"
    assert "event=agent_job.claimed" in messages
    assert "event=agent_job.failed" in messages
    assert "input_json" not in messages
    assert "raw_output" not in messages


def test_agent_job_worker_maps_auth_required_failure(tmp_path) -> None:
    store, _ = _store(tmp_path)
    _queue_job(store, "job-worker-auth-required")

    async def fail_auth(**_kwargs):
        raise AgentAuthenticationRequiredError(
            profile_name="governor",
            runtime_volume_mode="local-debug",
            settings_env_file="docker/.env.local-debug",
            missing=["MODEL_PROVIDER_API_KEY", "ANTHROPIC_API_KEY"],
        )

    result = asyncio.run(
        AgentJobWorker(feedback_store=store, run_profile_json=fail_auth).run_once()
    )

    assert result is not None and result.status == "failed"
    assert result.error_json is not None and result.error_json.error_code == AGENT_AUTH_REQUIRED
    assert result.raw_output_json is not None
    assert result.raw_output_json["error_type"] == "agent_auth_required"


def test_timeout_winner_discards_late_agent_failure(tmp_path) -> None:
    store, _ = _store(tmp_path)
    _queue_job(store, "job-timeout-wins")
    job = store.claim_next_agent_job()
    assert job is not None
    _expire_claimed_job(store, job["job_id"])

    timed_out = store._timeout_stale_agent_jobs()
    late_failure = store.fail_projected_agent_job(
        job,
        error_code="AGENT_RUNTIME_ERROR",
        message="late worker failure",
        raw_output_json={"late": True},
    )

    assert [item["job_id"] for item in timed_out] == [job["job_id"]]
    assert late_failure is not None and late_failure["status"] == "timeout"
    assert late_failure["error_json"]["error_code"] == "AGENT_TIMEOUT"
    assert late_failure["raw_output_json"] is None


def test_agent_job_projection_rejects_invalid_persisted_status(tmp_path) -> None:
    store, _ = _store(tmp_path)
    _queue_job(store, "job-invalid-status")
    with store.Session.begin() as db:
        db.execute(
            text("UPDATE agent_jobs SET status = 'unknown_status' WHERE job_id = 'job-invalid-status'")
        )

    with pytest.raises(ValidationError):
        store.get_agent_job("job-invalid-status")


def test_removed_eval_case_generation_job_type_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unsupported agent job type"):
        coerce_agent_job_type("eval_case_generation")
