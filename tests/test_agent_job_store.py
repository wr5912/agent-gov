from feedback_store_test_utils import (
    FeedbackSignalCreateRequest,
    _complete_eval_case_generation_job,
    _record_run,
    _store,
)
from app.runtime.agent_job_types import agent_job_spec
from app.runtime.schema_versions import REGRESSION_IMPACT_ANALYSIS_OUTPUT_SCHEMA_VERSION
from pydantic import ValidationError
import pytest
from sqlalchemy import text


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

    assert claimed["job_id"] == "evg-single-consumer"
    assert claimed["status"] == "running"
    assert second_claim is None


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
            "impacted_assets": [],
            "recommendations": ["继续保留当前回归资产。"],
            "summary": "未发现回归影响。",
            "risk_assessment": "low",
            "next_steps": [],
        },
    )

    impact = store.get_regression_impact_analysis(eval_run["eval_run_id"])
    assert completed["status"] == "completed"
    assert impact["job_id"] == "riaj-projection"
    assert impact["recommendations"] == ["继续保留当前回归资产。"]


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
