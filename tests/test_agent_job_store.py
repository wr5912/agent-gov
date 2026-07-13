import asyncio
import logging
import threading

import pytest
from app.runtime.agent_job_errors import AGENT_AUTH_REQUIRED, AgentAuthenticationRequiredError
from app.runtime.agent_job_types import agent_job_spec
from app.runtime.runtime_db import AgentJobModel
from app.services.agent_job_worker import AgentJobWorker
from pydantic import ValidationError
from sqlalchemy import text

from feedback_store_test_utils import (
    FeedbackSignalCreateRequest,
    _complete_eval_case_generation_job,
    _eval_case_generation_output,
    _record_run,
    _store,
)


def _claimed_eval_case_generation_job(store):
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_incomplete"], comment="数据不全"))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]], title="数据不全")
    store.sync_feedback_eval_cases(feedback_case_id=feedback_case["feedback_case_id"])
    claimed = store.claim_next_agent_job()
    assert claimed is not None
    return claimed, feedback_case


def _expire_claimed_job(store, job_id: str) -> None:
    with store.Session.begin() as db:
        row = db.get(AgentJobModel, job_id)
        assert row is not None
        row.started_at = "2026-01-01T00:00:00+00:00"
        row.timeout_seconds = 1


def test_unified_agent_job_schema_drops_legacy_job_tables(tmp_path):
    store, _ = _store(tmp_path)
    with store.Session() as db:
        table_names = {str(row[0]) for row in db.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).all()}

    assert "agent_jobs" in table_names
    assert "execution_applications" not in table_names
    assert "feedback_jobs" not in table_names
    assert "optimization_executions" not in table_names


def test_agent_job_claim_is_single_consumer(tmp_path):
    store, _ = _store(tmp_path)
    spec = agent_job_spec("eval_case_generation")
    store.create_agent_job(
        job_id="evg-single-consumer",
        job_type=spec.job_type,
        scope_kind="feedback_dataset",
        scope_id="feedback-dataset",
        profile_name=spec.profile_name,
        input_payload={"schema_version": "feedback-eval-case-generation-input/v1", "task": "generate_feedback_eval_cases"},
    )

    claimed = store.claim_next_agent_job()
    second_claim = store.claim_next_agent_job()

    assert claimed is not None
    assert claimed["job_id"] == "evg-single-consumer"
    assert claimed["status"] == "running"
    assert second_claim is None


def test_agent_job_default_timeout_uses_store_governance_timeout(tmp_path):
    store, _ = _store(tmp_path)
    store.agent_job_timeout_seconds = 123
    spec = agent_job_spec("eval_case_generation")

    job = store.create_agent_job(
        job_id="evg-configured-timeout",
        job_type=spec.job_type,
        scope_kind="feedback_dataset",
        scope_id="feedback-dataset",
        profile_name=spec.profile_name,
        input_payload={"schema_version": "feedback-eval-case-generation-input/v1", "task": "generate_feedback_eval_cases"},
    )

    assert job["timeout_seconds"] == 123


def test_stale_running_agent_job_times_out_and_next_job_can_claim(tmp_path):
    store, _ = _store(tmp_path)
    spec = agent_job_spec("eval_case_generation")
    store.create_agent_job(
        job_id="evg-stale-running",
        job_type=spec.job_type,
        scope_kind="feedback_dataset",
        scope_id="feedback-dataset",
        profile_name=spec.profile_name,
        input_payload={"schema_version": "feedback-eval-case-generation-input/v1", "task": "generate_feedback_eval_cases"},
    )
    claimed = store.claim_next_agent_job()
    assert claimed is not None
    store.create_agent_job(
        job_id="evg-next-queued",
        job_type=spec.job_type,
        scope_kind="feedback_dataset",
        scope_id="feedback-dataset",
        profile_name=spec.profile_name,
        input_payload={"schema_version": "feedback-eval-case-generation-input/v1", "task": "generate_feedback_eval_cases"},
    )
    with store.Session.begin() as db:
        row = db.get(AgentJobModel, claimed["job_id"])
        assert row is not None
        row.started_at = "2026-01-01T00:00:00+00:00"
        row.timeout_seconds = 1

    timed_out = store._timeout_stale_agent_jobs()
    next_claimed = store.claim_next_agent_job()

    assert next_claimed is not None
    assert [job["job_id"] for job in timed_out] == ["evg-stale-running"]
    assert timed_out[0]["status"] == "timeout"
    assert timed_out[0]["error_json"]["error_code"] == "AGENT_TIMEOUT"
    assert next_claimed["job_id"] == "evg-next-queued"


def test_agent_job_worker_logs_claim_and_runtime_failure(tmp_path, caplog):
    store, _ = _store(tmp_path)
    spec = agent_job_spec("eval_case_generation")
    store.create_agent_job(
        job_id="evg-worker-fails",
        job_type=spec.job_type,
        scope_kind="feedback_dataset",
        scope_id="feedback-dataset",
        profile_name=spec.profile_name,
        input_payload={"schema_version": "feedback-eval-case-generation-input/v1", "task": "generate_feedback_eval_cases"},
    )

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

    assert result is not None
    assert result.error_json is not None
    assert result.status == "failed"
    assert result.error_json.error_code == "AGENT_RUNTIME_ERROR"
    assert "event=agent_job.claimed" in messages
    assert "event=agent_job.failed" in messages
    assert "job_id=evg-worker-fails" in messages
    assert "error_code=AGENT_RUNTIME_ERROR" in messages
    assert "worker_instance=test-worker" in messages
    assert "input_json" not in messages
    assert "raw_output" not in messages


def test_agent_job_worker_maps_auth_required_failure(tmp_path, caplog):
    store, _ = _store(tmp_path)
    spec = agent_job_spec("eval_case_generation")
    store.create_agent_job(
        job_id="evg-worker-auth-required",
        job_type=spec.job_type,
        scope_kind="feedback_dataset",
        scope_id="feedback-dataset",
        profile_name=spec.profile_name,
        input_payload={"schema_version": "feedback-eval-case-generation-input/v1", "task": "generate_feedback_eval_cases"},
    )

    async def fail_auth(**_kwargs):
        raise AgentAuthenticationRequiredError(
            profile_name="governor",
            runtime_volume_mode="local-debug",
            settings_env_file="docker/.env.local-debug",
            missing=["MODEL_PROVIDER_API_KEY", "ANTHROPIC_API_KEY"],
        )

    caplog.set_level(logging.INFO, logger="app.services.agent_job_worker")
    worker = AgentJobWorker(
        feedback_store=store,
        run_profile_json=fail_auth,
        poll_interval_seconds=0,
        worker_instance="test-worker",
    )

    result = asyncio.run(worker.run_once())
    messages = "\n".join(record.getMessage() for record in caplog.records)

    assert result is not None
    assert result.status == "failed"
    assert result.error_json is not None
    assert result.error_json.error_code == AGENT_AUTH_REQUIRED
    assert result.raw_output_json is not None
    assert result.raw_output_json["error_type"] == "agent_auth_required"
    assert result.raw_output_json["settings_env_file"] == "docker/.env.local-debug"
    assert "error_code=AGENT_AUTH_REQUIRED" in messages
    assert "MODEL_PROVIDER_API_KEY" not in messages


def test_agent_job_worker_timeout_is_terminal_timeout(tmp_path, caplog):
    store, _ = _store(tmp_path)
    spec = agent_job_spec("eval_case_generation")
    store.create_agent_job(
        job_id="evg-worker-timeout",
        job_type=spec.job_type,
        scope_kind="feedback_dataset",
        scope_id="feedback-dataset",
        profile_name=spec.profile_name,
        input_payload={"schema_version": "feedback-eval-case-generation-input/v1", "task": "generate_feedback_eval_cases"},
    )

    async def timeout_runtime(**_kwargs):
        raise asyncio.TimeoutError("runner timed out")

    caplog.set_level(logging.INFO, logger="app.services.agent_job_worker")
    worker = AgentJobWorker(
        feedback_store=store,
        run_profile_json=timeout_runtime,
        poll_interval_seconds=0,
        worker_instance="test-worker",
    )

    result = asyncio.run(worker.run_once())
    messages = "\n".join(record.getMessage() for record in caplog.records)

    assert result is not None and result.status == "timeout"
    assert result.error_json is not None and result.error_json.error_code == "AGENT_TIMEOUT"
    assert "event=agent_job.timeout" in messages
    assert "status=timeout" in messages


def test_agent_job_worker_logs_stale_timeout(tmp_path, caplog):
    store, _ = _store(tmp_path)
    spec = agent_job_spec("eval_case_generation")
    store.create_agent_job(
        job_id="evg-worker-stale",
        job_type=spec.job_type,
        scope_kind="feedback_dataset",
        scope_id="feedback-dataset",
        profile_name=spec.profile_name,
        input_payload={"schema_version": "feedback-eval-case-generation-input/v1", "task": "generate_feedback_eval_cases"},
    )
    claimed = store.claim_next_agent_job()
    assert claimed is not None
    with store.Session.begin() as db:
        row = db.get(AgentJobModel, claimed["job_id"])
        assert row is not None
        row.started_at = "2026-01-01T00:00:00+00:00"
        row.timeout_seconds = 1

    async def unused_runtime(**_kwargs):
        raise AssertionError("stale timeout scan should not run queued job")

    caplog.set_level(logging.INFO, logger="app.services.agent_job_worker")
    worker = AgentJobWorker(
        feedback_store=store,
        run_profile_json=unused_runtime,
        poll_interval_seconds=0,
        worker_instance="test-worker",
    )

    result = asyncio.run(worker.run_once())
    messages = "\n".join(record.getMessage() for record in caplog.records)

    assert result is None
    assert "event=agent_job.stale_timeout" in messages
    assert "job_id=evg-worker-stale" in messages
    assert "status=timeout" in messages
    assert "error_code=AGENT_TIMEOUT" in messages
    assert "worker_instance=test-worker" in messages
    assert "input_json" not in messages
    assert "raw_output" not in messages


def test_agent_job_worker_logs_discarded_late_completion_after_timeout(tmp_path, caplog):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_incomplete"], comment="数据不全"))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]], title="数据不全")
    queued = store.sync_feedback_eval_cases(feedback_case_id=feedback_case["feedback_case_id"])

    async def timeout_then_return_output(**_kwargs):
        _expire_claimed_job(store, queued["job_id"])
        assert [item["job_id"] for item in store._timeout_stale_agent_jobs()] == [queued["job_id"]]
        return _eval_case_generation_output(queued, feedback_case)

    caplog.set_level(logging.INFO, logger="app.services.agent_job_worker")
    worker = AgentJobWorker(
        feedback_store=store,
        run_profile_json=timeout_then_return_output,
        poll_interval_seconds=0,
        worker_instance="test-worker",
    )

    result = asyncio.run(worker.run_once())
    messages = "\n".join(record.getMessage() for record in caplog.records)

    assert result is not None and result.status == "timeout"
    assert "event=agent_job.completion_discarded" in messages
    assert "event=agent_job.completed" not in messages
    assert "status=timeout" in messages
    assert "error_code=AGENT_TIMEOUT" in messages
    assert store.list_eval_cases() == []


def test_agent_job_projection_rejects_invalid_persisted_status(tmp_path):
    store, _ = _store(tmp_path)
    spec = agent_job_spec("eval_case_generation")
    store.create_agent_job(
        job_id="evg-invalid-status",
        job_type=spec.job_type,
        scope_kind="feedback_dataset",
        scope_id="feedback-dataset",
        profile_name=spec.profile_name,
        input_payload={"schema_version": "feedback-eval-case-generation-input/v1", "task": "generate_feedback_eval_cases"},
    )
    with store.Session.begin() as db:
        db.execute(text("UPDATE agent_jobs SET status = 'unknown_status' WHERE job_id = 'evg-invalid-status'"))

    with pytest.raises(ValidationError):
        store.get_agent_job("evg-invalid-status")


def test_agent_job_json_update_rejects_invalid_persisted_json(tmp_path):
    store, _ = _store(tmp_path)
    spec = agent_job_spec("eval_case_generation")
    store.create_agent_job(
        job_id="evg-invalid-error-json",
        job_type=spec.job_type,
        scope_kind="feedback_dataset",
        scope_id="feedback-dataset",
        profile_name=spec.profile_name,
        input_payload={"schema_version": "feedback-eval-case-generation-input/v1", "task": "generate_feedback_eval_cases"},
    )
    with store.Session.begin() as db:
        row = db.get(AgentJobModel, "evg-invalid-error-json")
        row.error_json = ["not", "an", "object"]

    with pytest.raises(ValidationError):
        store.fail_agent_job("evg-invalid-error-json", error_code="RUNTIME_ERROR", message="failed")


def test_eval_case_generation_agent_job_projects_to_eval_case(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_incomplete"], comment="数据不全"))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]], title="数据不全")
    job = store.sync_feedback_eval_cases(feedback_case_id=feedback_case["feedback_case_id"])

    eval_case = _complete_eval_case_generation_job(store, job, feedback_case=feedback_case)
    completed = store.get_agent_job(job["job_id"])

    assert completed["status"] == "completed"
    assert completed["validated_output_json"]["created"] == 1
    assert eval_case["source_feedback_case_id"] == feedback_case["feedback_case_id"]
    assert store.find_eval_case(eval_case["eval_case_id"])["prompt"]


def test_eval_case_generation_uses_backend_source_and_lifecycle_fields(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_incomplete"], comment="数据不全"))
    feedback_case = store.create_case(source_ids=[signal["signal_id"]], title="数据不全")
    job = store.sync_feedback_eval_cases(feedback_case_id=feedback_case["feedback_case_id"])

    completed = store.complete_projected_agent_job(
        job,
        {
            "job_id": "evg-agent-wrong",
            "scope_kind": "feedback_dataset",
            "scope_id": "feedback-dataset",
            "status": "completed",
            "eval_cases": [
                {
                    "eval_case_id": "evc-agent-wrong",
                    "status": "active",
                    "source": "agent_supplied",
                    "source_run_id": "run-agent-wrong",
                    "prompt": "复现原始问题。",
                    "expected_behavior": "回答前读取当前 workspace 配置。",
                    "checks_json": {"requires_tool_use": True},
                    "labels": ["tool_data_incomplete"],
                }
            ],
            "results": [{"status": "agent_supplied"}],
        },
    )
    eval_case = completed["validated_output_json"]["eval_cases"][0]

    assert completed["validated_output_json"]["job_id"] == job["job_id"]
    assert completed["validated_output_json"]["scope_kind"] == job["scope_kind"]
    assert completed["validated_output_json"]["scope_id"] == job["scope_id"]
    assert completed["validated_output_json"]["results"][0]["status"] == "created"
    assert eval_case["eval_case_id"] != "evc-agent-wrong"
    assert eval_case["status"] == "draft"
    assert eval_case["source"] == "eval_case_governor"
    assert eval_case["source_feedback_case_id"] == feedback_case["feedback_case_id"]
    assert eval_case["source_run_id"] == "run-1"


def test_timeout_winner_discards_late_eval_case_projection(tmp_path):
    store, _ = _store(tmp_path)
    job, feedback_case = _claimed_eval_case_generation_job(store)
    _expire_claimed_job(store, job["job_id"])

    timed_out = store._timeout_stale_agent_jobs()
    late_result = store.complete_projected_agent_job(job, _eval_case_generation_output(job, feedback_case))

    assert [item["job_id"] for item in timed_out] == [job["job_id"]]
    assert late_result["status"] == "timeout"
    assert late_result["error_json"]["error_code"] == "AGENT_TIMEOUT"
    assert late_result["raw_output_json"] is None
    assert late_result["validated_output_json"] is None
    assert store.list_eval_cases() == []


def test_timeout_winner_discards_late_agent_failure(tmp_path):
    store, _ = _store(tmp_path)
    job, _feedback_case = _claimed_eval_case_generation_job(store)
    _expire_claimed_job(store, job["job_id"])

    timed_out = store._timeout_stale_agent_jobs()
    late_failure = store.fail_projected_agent_job(
        job,
        error_code="AGENT_RUNTIME_ERROR",
        message="late worker failure",
        raw_output_json={"late": True},
    )

    assert [item["job_id"] for item in timed_out] == [job["job_id"]]
    assert late_failure["status"] == "timeout"
    assert late_failure["error_json"]["error_code"] == "AGENT_TIMEOUT"
    assert late_failure["raw_output_json"] is None
    assert store.list_eval_cases() == []


def test_completion_winner_is_not_overwritten_by_stale_timeout_snapshot(tmp_path, monkeypatch):
    store, _ = _store(tmp_path)
    job, feedback_case = _claimed_eval_case_generation_job(store)
    _expire_claimed_job(store, job["job_id"])
    timeout_ready = threading.Event()
    allow_timeout_cas = threading.Event()
    original_transition = store._compare_and_transition_agent_job_row

    def gated_transition(db, job_id, **kwargs):
        if kwargs.get("status") == "timeout":
            timeout_ready.set()
            assert allow_timeout_cas.wait(timeout=5)
        return original_transition(db, job_id, **kwargs)

    monkeypatch.setattr(store, "_compare_and_transition_agent_job_row", gated_transition)
    timeout_results: list[list[dict]] = []
    timeout_errors: list[BaseException] = []

    def timeout_stale_job() -> None:
        try:
            timeout_results.append(store._timeout_stale_agent_jobs())
        except BaseException as exc:  # noqa: BLE001 - thread errors must be asserted in the parent test
            timeout_errors.append(exc)

    thread = threading.Thread(target=timeout_stale_job)
    thread.start()
    try:
        assert timeout_ready.wait(timeout=5)
        completed = store.complete_projected_agent_job(job, _eval_case_generation_output(job, feedback_case))
    finally:
        allow_timeout_cas.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert timeout_errors == []
    assert timeout_results == [[]]
    assert completed["status"] == "completed"
    assert completed["error_json"] is None
    assert len(store.list_eval_cases()) == 1


def test_eval_case_projection_and_job_completion_roll_back_together(tmp_path, monkeypatch):
    store, _ = _store(tmp_path)
    job, feedback_case = _claimed_eval_case_generation_job(store)
    original_apply_fields = store._apply_agent_job_json_fields

    def fail_after_projection(row, fields):
        if "validated_output_json" in fields:
            raise RuntimeError("job completion write failed")
        return original_apply_fields(row, fields)

    monkeypatch.setattr(store, "_apply_agent_job_json_fields", fail_after_projection)

    with pytest.raises(RuntimeError, match="job completion write failed"):
        store.complete_projected_agent_job(job, _eval_case_generation_output(job, feedback_case))

    persisted = store.get_agent_job(job["job_id"])
    assert persisted["status"] == "running"
    assert persisted["raw_output_json"] is None
    assert persisted["validated_output_json"] is None
    assert store.list_eval_cases() == []
