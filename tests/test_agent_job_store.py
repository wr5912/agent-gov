import asyncio
import logging

import pytest
from app.runtime.agent_job_errors import AGENT_AUTH_REQUIRED, AgentAuthenticationRequiredError
from app.runtime.agent_job_types import agent_job_spec
from app.runtime.records.regression_impact_records import RegressionImpactAnalysisRecord
from app.runtime.runtime_db import AgentJobModel
from app.services.agent_job_worker import AgentJobWorker
from pydantic import ValidationError
from sqlalchemy import text

from feedback_store_test_utils import (
    FeedbackSignalCreateRequest,
    _complete_eval_case_generation_job,
    _record_run,
    _store,
)


def test_unified_agent_job_schema_drops_legacy_job_tables(tmp_path):
    store, _ = _store(tmp_path)
    with store.Session() as db:
        table_names = {str(row[0]) for row in db.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).all()}

    assert "agent_jobs" in table_names
    assert "execution_applications" in table_names
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


def test_batch_projection_refreshes_eval_case_generation_job_status(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_incomplete"], comment="数据不全"))
    batch = store.create_optimization_batch(
        [{"source_kind": "signal", "source_id": signal["signal_id"]}],
        title="数据不全批次",
    )
    queued_job = batch["eval_case_generation_job"]
    feedback_case = queued_job["input_json"]["feedback_cases"][0]["feedback_case"]

    _complete_eval_case_generation_job(store, queued_job, feedback_case=feedback_case)
    refreshed = store.find_optimization_batch(batch["batch_id"])

    assert refreshed["eval_case_generation_job"]["status"] == "completed"
    assert refreshed["eval_case_generation_job"]["completed_at"]
    assert refreshed["eval_case_ids"]


def test_regression_impact_agent_job_projects_to_impact_analysis(tmp_path):
    store, _ = _store(tmp_path)
    spec = agent_job_spec("regression_impact_analysis")
    eval_run = store.create_eval_run(eval_case_ids=[], agent_version_id="main-v-test", source="manual_feedback_dataset")
    finished_eval_run = store.finish_eval_run(eval_run["eval_run_id"])
    job = store.create_agent_job(
        job_id="riaj-projection",
        job_type=spec.job_type,
        scope_kind="eval_run",
        scope_id=eval_run["eval_run_id"],
        profile_name=spec.profile_name,
        input_payload={"schema_version": "regression-impact-analysis-input/v1", "eval_run_id": eval_run["eval_run_id"]},
    )

    completed = store.complete_projected_agent_job(
        job,
        {
            "impact_analysis_id": "ria-agent-wrong",
            "eval_run_id": "evr-agent-wrong",
            "status": "completed",
            "result_status": "failed",
            "gate_result": {"status": "agent_wrong"},
            "impacted_assets": [
                {
                    "asset_id": "eval-asset-1",
                    "summary": "核心回归资产受影响。",
                    "agent_note": {"source": "regression-impact-analyzer"},
                }
            ],
            "recommendations": ["继续保留当前回归资产。"],
            "summary": "未发现回归影响。",
            "risk_assessment": "low",
            "next_steps": [],
            "_formatter": {"name": "dspy", "source": "fallback", "candidate_count": 0},
        },
    )

    impact = store.get_regression_impact_analysis(eval_run["eval_run_id"])
    completed_job = store.get_agent_job("riaj-projection")
    assert completed["status"] == "completed"
    assert "_formatter" not in completed_job["raw_output_json"]
    assert impact["job_id"] == "riaj-projection"
    assert "_formatter" not in impact
    assert impact["impact_analysis_id"] != "ria-agent-wrong"
    assert impact["eval_run_id"] == eval_run["eval_run_id"]
    assert impact["result_status"] == finished_eval_run["result_status"]
    assert impact["gate_result"] == finished_eval_run["gate_result"]
    assert impact["impacted_assets"] == []
    assert impact["recommendations"] == ["继续保留当前回归资产。"]


def test_regression_impact_force_rerun_clears_previous_error_json(tmp_path):
    store, _ = _store(tmp_path)
    eval_run = store.create_eval_run(eval_case_ids=[], agent_version_id="main-v-test", source="manual_feedback_dataset")
    failed_job = store.queue_regression_impact_agent_job(eval_run["eval_run_id"], force=True)

    store.complete_projected_agent_job(
        failed_job,
        {
            "eval_run_id": eval_run["eval_run_id"],
            "status": "completed",
            "impacted_assets": [],
        },
    )
    failed = store.get_regression_impact_analysis(eval_run["eval_run_id"])

    rerun_job = store.queue_regression_impact_agent_job(eval_run["eval_run_id"], force=True)
    pending = store.get_regression_impact_analysis(eval_run["eval_run_id"])
    store.complete_projected_agent_job(
        rerun_job,
        {
            "eval_run_id": eval_run["eval_run_id"],
            "status": "completed",
            "result_status": "passed",
            "gate_result": {"status": "passed"},
            "impacted_assets": [],
            "recommendations": ["重跑通过。"],
        },
    )
    completed = store.get_regression_impact_analysis(eval_run["eval_run_id"])

    assert failed["status"] == "failed"
    assert failed["error_json"]
    assert pending["status"] == "pending"
    assert pending["error_json"] is None
    assert completed["status"] == "completed"
    assert completed["error_json"] is None


def test_regression_impact_record_rejects_unidentified_impacted_asset():
    with pytest.raises(ValidationError):
        RegressionImpactAnalysisRecord.model_validate(
            {
                "schema_version": "regression-impact-analysis/v1",
                "impact_analysis_id": "ria-invalid",
                "eval_run_id": "erun-invalid",
                "created_at": "2026-06-02T00:00:00+00:00",
                "completed_at": "2026-06-02T00:00:01+00:00",
                "status": "completed",
                "impacted_assets": [{"agent_note": {"source": "bad-agent"}}],
                "recommendations": ["复查输出。"],
            }
        )


def test_regression_impact_projection_rejects_invalid_persisted_status(tmp_path):
    store, _ = _store(tmp_path)
    eval_run = store.create_eval_run(eval_case_ids=[], agent_version_id="main-v-test", source="manual_feedback_dataset")
    job = store.queue_regression_impact_agent_job(eval_run["eval_run_id"], force=True)
    with store.Session.begin() as db:
        db.execute(
            text("UPDATE regression_impact_analyses SET status = 'unknown_status' WHERE eval_run_id = :eval_run_id"),
            {"eval_run_id": eval_run["eval_run_id"]},
        )

    assert job is not None
    with pytest.raises(ValidationError):
        store.get_regression_impact_analysis(eval_run["eval_run_id"])
