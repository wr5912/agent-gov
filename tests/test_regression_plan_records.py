from pydantic import ValidationError
from sqlalchemy import text

from feedback_store_test_utils import FeedbackSignalCreateRequest, _record_run, _store, pytest
from app.runtime.runtime_db import RegressionGateOverrideModel


def _create_regression_plan_fixture(tmp_path):
    store, _ = _store(tmp_path)
    _record_run(store)
    signal = store.create_signal(FeedbackSignalCreateRequest(run_id="run-1", labels=["tool_data_incomplete"]))
    batch = store.create_optimization_batch([{"source_kind": "signal", "source_id": signal["signal_id"]}], title="回归计划批次")
    eval_case = {
        "schema_version": "feedback-eval-case/v1",
        "eval_case_id": "evc-regression-plan",
        "created_at": "2026-05-29T00:00:00+00:00",
        "updated_at": "2026-05-29T00:00:00+00:00",
        "status": "active",
        "source": "test",
        "source_feedback_case_id": None,
        "source_run_id": None,
        "source_kind": "manual",
        "source_id": "evc-regression-plan",
        "source_refs": [],
        "asset_layer": "core_regression",
        "promotion_status": "approved",
        "blocking_policy": "blocking",
        "flaky_status": "stable",
        "variant_role": "manual_regression",
        "prompt": "回归计划用例",
        "labels": ["feedback_optimization"],
        "expected_behavior": "回答非空且无运行错误。",
        "checks_json": {"requires_non_empty_answer": True},
        "source_summary": {},
    }
    with store.Session.begin() as db:
        store._add_eval_case_row(db, eval_case)
        row = store._batch_row_for_update(db, batch["batch_id"])
        store._update_batch_eval_case_ids_row(db, row, append_id=eval_case["eval_case_id"])
    plan = store.create_regression_plan(batch["batch_id"])
    return store, batch, plan


@pytest.mark.parametrize("column", ["status", "selection_fingerprint"])
def test_regression_plan_projection_rejects_invalid_persisted_shape(tmp_path, column):
    store, _, plan = _create_regression_plan_fixture(tmp_path)

    with store.Session.begin() as db:
        if column == "status":
            statement = text("UPDATE regression_plans SET status = 'unknown_status' WHERE regression_plan_id = :plan_id")
        else:
            statement = text("UPDATE regression_plans SET selection_fingerprint = 'not-a-sha' WHERE regression_plan_id = :plan_id")
        db.execute(statement, {"plan_id": plan["regression_plan_id"]})

    with pytest.raises(ValidationError):
        store.get_regression_plan(plan["regression_plan_id"])


def test_regression_gate_override_projection_rejects_invalid_persisted_operator(tmp_path):
    store, batch, plan = _create_regression_plan_fixture(tmp_path)
    eval_run = store.create_eval_run(
        eval_case_ids=plan["eval_case_ids"],
        agent_version_id="main-v-test",
        source="optimization_batch_regression",
        regression_plan_id=plan["regression_plan_id"],
    )
    override_id = "rgo-invalid-operator"
    with store.Session.begin() as db:
        db.add(
            RegressionGateOverrideModel(
                override_id=override_id,
                batch_id=batch["batch_id"],
                eval_run_id=eval_run["eval_run_id"],
                operator="tester",
                reason="人工 break-glass",
                expires_at="2026-06-03T00:00:00+00:00",
                created_at="2026-06-02T00:00:00+00:00",
                before_json=eval_run,
                after_json={**eval_run, "result_status": "passed_with_notes"},
            )
        )

    with store.Session.begin() as db:
        db.execute(
            text("UPDATE regression_gate_overrides SET operator = '' WHERE override_id = :override_id"),
            {"override_id": override_id},
        )

    with pytest.raises(ValidationError):
        store.get_regression_gate_override(override_id)
