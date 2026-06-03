import asyncio
import logging

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from feedback_store_test_utils import (
    FeedbackSignalCreateRequest,
    _complete_eval_case_generation_job,
    _record_run,
    _store,
)
from app.runtime.agent_job_types import agent_job_spec
from app.runtime.records.regression_impact_records import RegressionImpactAnalysisRecord
from app.runtime.runtime_db import AgentJobModel
from app.runtime.schema_versions import REGRESSION_IMPACT_ANALYSIS_OUTPUT_SCHEMA_VERSION
from app.services.agent_job_worker import AgentJobWorker


def test_unified_agent_job_schema_drops_legacy_job_tables(tmp_path):
    store, _ = _store(tmp_path)
    with store.Session() as db:
        table_names = {
            str(row[0])
            for row in db.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).all()
        }

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
        output_schema_version=spec.output_schema_version,
    )

    claimed = store.claim_next_agent_job()
    second_claim = store.claim_next_agent_job()

    assert claimed is not None
    assert claimed["job_id"] == "evg-single-consumer"
    assert claimed["status"] == "running"
    assert second_claim is None


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
        output_schema_version=spec.output_schema_version,
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
        output_schema_version=spec.output_schema_version,
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
        output_schema_version=spec.output_schema_version,
    )

    async def fail_runtime(**_kwargs):
        raise RuntimeError("formatter crashed")

    caplog.set_level(logging.INFO, logger="app.services.agent_job_worker")
    worker = AgentJobWorker(
        feedback_store=store,
        run_profile_json=fail_runtime,
        poll_interval_seconds=0,
    )

    result = asyncio.run(worker.run_once())
    messages = "\n".join(record.getMessage() for record in caplog.records)

    assert result is not None
    assert result.error_json is not None
    assert result.status == "failed"
    assert result.error_json.error_code == "AGENT_RUNTIME_ERROR"
    assert "agent job claimed job_id=evg-worker-fails" in messages
    assert "agent job failed job_id=evg-worker-fails" in messages


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
        output_schema_version=spec.output_schema_version,
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
        output_schema_version=spec.output_schema_version,
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
    job = store.create_agent_job(
        job_id="riaj-projection",
        job_type=spec.job_type,
        scope_kind="eval_run",
        scope_id=eval_run["eval_run_id"],
        profile_name=spec.profile_name,
        input_payload={"schema_version": "regression-impact-analysis-input/v1", "eval_run_id": eval_run["eval_run_id"]},
        output_schema_version=spec.output_schema_version,
    )

    completed = store.complete_projected_agent_job(
        job,
        {
            "schema_version": REGRESSION_IMPACT_ANALYSIS_OUTPUT_SCHEMA_VERSION,
            "eval_run_id": eval_run["eval_run_id"],
            "status": "completed",
            "result_status": "passed",
            "gate_result": {"status": "passed"},
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
    assert completed_job["raw_output_json"]["_formatter"]["name"] == "dspy"
    assert impact["job_id"] == "riaj-projection"
    assert "_formatter" not in impact
    assert impact["impacted_assets"][0]["agent_note"] == {"source": "regression-impact-analyzer"}
    assert impact["recommendations"] == ["继续保留当前回归资产。"]


def test_regression_impact_force_rerun_clears_previous_error_json(tmp_path):
    store, _ = _store(tmp_path)
    eval_run = store.create_eval_run(eval_case_ids=[], agent_version_id="main-v-test", source="manual_feedback_dataset")
    failed_job = store.queue_regression_impact_agent_job(eval_run["eval_run_id"], force=True)

    store.complete_projected_agent_job(
        failed_job,
        {
            "schema_version": REGRESSION_IMPACT_ANALYSIS_OUTPUT_SCHEMA_VERSION,
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
            "schema_version": REGRESSION_IMPACT_ANALYSIS_OUTPUT_SCHEMA_VERSION,
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
