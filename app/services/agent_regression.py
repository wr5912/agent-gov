from __future__ import annotations

from typing import Protocol

from app.runtime.json_types import JsonObject
from app.runtime.runtime_db import utc_now


class RegressionTransitionTarget(Protocol):
    def _transition_change_set(
        self,
        change_set_id: str,
        status: str,
        *,
        fields: JsonObject,
        action: str,
        operator: str,
        expected_fields: JsonObject | None = None,
    ) -> JsonObject: ...

    def _eval_run_publication_blocker(self, eval_run_value: object) -> str | None: ...


class AgentRegressionMixin:
    def mark_regression_running(self: RegressionTransitionTarget, change_set_id: str, *, eval_run_id: str, operator: str = "runtime") -> JsonObject:
        return start_regression(self, change_set_id, eval_run_id=eval_run_id, operator=operator)

    def complete_regression(
        self: RegressionTransitionTarget,
        change_set_id: str,
        *,
        eval_run: JsonObject,
        operator: str = "runtime",
        expected_eval_run_id: str | None = None,
    ) -> JsonObject:
        return finish_regression(
            self,
            change_set_id,
            eval_run=eval_run,
            operator=operator,
            expected_eval_run_id=expected_eval_run_id,
        )

    def fail_regression(
        self: RegressionTransitionTarget,
        change_set_id: str,
        *,
        expected_eval_run_id: str,
        error_type: str,
        operator: str = "runtime",
    ) -> JsonObject:
        return record_regression_failure(
            self,
            change_set_id,
            expected_eval_run_id=expected_eval_run_id,
            error_type=error_type,
            operator=operator,
        )


def start_regression(
    governance: RegressionTransitionTarget,
    change_set_id: str,
    *,
    eval_run_id: str,
    operator: str,
) -> JsonObject:
    return governance._transition_change_set(
        change_set_id,
        "regression_running",
        fields={
            "latest_eval_run_id": eval_run_id,
            "latest_eval_run": None,
            "regression_error": None,
            "regression_started_at": utc_now(),
        },
        action="regression_running",
        operator=operator,
    )


def finish_regression(
    governance: RegressionTransitionTarget,
    change_set_id: str,
    *,
    eval_run: JsonObject,
    operator: str,
    expected_eval_run_id: str | None,
) -> JsonObject:
    result_status = str(eval_run.get("result_status") or "")
    target = (
        "regression_passed"
        if result_status in {"passed", "passed_with_notes"} and not governance._eval_run_publication_blocker(eval_run)
        else "regression_failed"
    )
    return governance._transition_change_set(
        change_set_id,
        target,
        fields={
            "latest_eval_run_id": eval_run.get("eval_run_id"),
            "latest_eval_run": eval_run,
            "regression_error": None,
        },
        action=target,
        operator=operator,
        expected_fields={"latest_eval_run_id": expected_eval_run_id} if expected_eval_run_id else None,
    )


def record_regression_failure(
    governance: RegressionTransitionTarget,
    change_set_id: str,
    *,
    expected_eval_run_id: str,
    error_type: str,
    operator: str,
) -> JsonObject:
    now = utc_now()
    eval_run: JsonObject = {
        "eval_run_id": expected_eval_run_id,
        "created_at": now,
        "completed_at": now,
        "status": "failed",
        "result_status": "failed",
        "source": "agent_change_set_regression",
        "change_set_id": change_set_id,
        "summary": {"total": 0, "passed": 0, "failed": 0, "needs_human_review": 0},
        "gate_result": {"status": "failed"},
        "items": [],
    }
    return governance._transition_change_set(
        change_set_id,
        "regression_failed",
        fields={
            "latest_eval_run_id": expected_eval_run_id,
            "latest_eval_run": eval_run,
            "regression_error": {"error_type": error_type, "updated_at": now},
        },
        action="regression_failed",
        operator=operator,
        expected_fields={"latest_eval_run_id": expected_eval_run_id},
    )
