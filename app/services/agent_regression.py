from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError
from sqlalchemy import select

from app.runtime.errors import ConflictError, DataIntegrityError, NotFoundError
from app.runtime.json_types import JsonObject
from app.runtime.records.eval_run_records import EvalRunProjectionRecord
from app.runtime.runtime_db import AgentChangeSetModel, utc_now
from app.runtime.runtime_recovery import runtime_operation_is_stale
from app.runtime.stores.feedback_eval_store import EvalRunReviewPlan


class RegressionTransitionTarget(Protocol):
    feedback_store: Any

    def get_change_set(self, change_set_id: str) -> JsonObject | None: ...

    def _get_persisted_regression_eval_run(self, eval_run_id: str) -> JsonObject | None: ...

    def _get_persisted_regression_eval_run_by_attempt(self, regression_attempt_id: str) -> JsonObject | None: ...

    def complete_regression(self, change_set_id: str, *, eval_run_id: str, operator: str = "runtime") -> JsonObject: ...

    def fail_regression(
        self,
        change_set_id: str,
        *,
        expected_eval_run_id: str,
        error_type: str,
        operator: str = "runtime",
    ) -> JsonObject: ...

    def _plan_regression_review(
        self,
        eval_run_id: str,
        *,
        review_id: str,
        operator: str,
        reason: str,
        scope: str,
        items: list[JsonObject],
    ) -> EvalRunReviewPlan: ...

    def _apply_regression_review(self, db: object, plan: EvalRunReviewPlan) -> None: ...

    def _transition_change_set(
        self,
        change_set_id: str,
        status: str,
        *,
        fields: JsonObject,
        action: str,
        operator: str,
        expected_fields: JsonObject | None = None,
        transaction_mutation: Any | None = None,
    ) -> JsonObject: ...

    def _eval_run_publication_blocker(self, eval_run_value: object) -> str | None: ...


class AgentRegressionMixin:
    def reconcile_regression_runs(
        self: RegressionTransitionTarget,
        *,
        now: str | None = None,
    ) -> JsonObject:
        return reconcile_regression_runs(self, now=now)

    def mark_regression_running(
        self: RegressionTransitionTarget,
        change_set_id: str,
        *,
        eval_run_id: str,
        dataset_id: str,
        operator: str = "runtime",
    ) -> JsonObject:
        return start_regression(
            self,
            change_set_id,
            eval_run_id=eval_run_id,
            dataset_id=dataset_id,
            operator=operator,
        )

    def complete_regression(
        self: RegressionTransitionTarget,
        change_set_id: str,
        *,
        eval_run_id: str,
        operator: str = "runtime",
    ) -> JsonObject:
        return finish_regression(
            self,
            change_set_id,
            eval_run_id=eval_run_id,
            operator=operator,
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

    def review_regression(
        self: RegressionTransitionTarget,
        change_set_id: str,
        *,
        eval_run_id: str,
        review_id: str,
        operator: str,
        reason: str,
        scope: str,
        items: list[JsonObject],
    ) -> JsonObject:
        return review_regression_result(
            self,
            change_set_id,
            eval_run_id=eval_run_id,
            review_id=review_id,
            operator=operator,
            reason=reason,
            scope=scope,
            items=items,
        )


def reconcile_regression_runs(
    governance: RegressionTransitionTarget,
    *,
    now: str | None = None,
) -> JsonObject:
    recovery_now = now or utc_now()
    completed: list[str] = []
    failed: list[str] = []
    errors: list[str] = []
    with governance.feedback_store.Session() as db:
        change_set_ids = list(
            db.scalars(
                select(AgentChangeSetModel.change_set_id)
                .where(AgentChangeSetModel.status == "regression_running")
                .order_by(AgentChangeSetModel.created_at, AgentChangeSetModel.change_set_id)
            ).all()
        )
    for change_set_id in change_set_ids:
        change_set = governance.get_change_set(change_set_id) or {}
        if change_set.get("status") != "regression_running":
            continue
        attempt_id = str(change_set.get("regression_attempt_id") or change_set.get("latest_eval_run_id") or "")
        if not change_set_id or not attempt_id:
            errors.append(change_set_id or "missing-change-set-id")
            continue
        eval_run = governance._get_persisted_regression_eval_run_by_attempt(attempt_id)
        try:
            if eval_run and str(eval_run.get("status") or "") in {"completed", "failed"}:
                governance.complete_regression(
                    change_set_id,
                    eval_run_id=str(eval_run["eval_run_id"]),
                    operator="runtime-reconciler",
                )
                completed.append(change_set_id)
            elif (eval_run and str(eval_run.get("status") or "") == "running") or not runtime_operation_is_stale(
                str(change_set.get("regression_started_at") or "") or None, now=recovery_now
            ):
                continue
            else:
                governance.fail_regression(
                    change_set_id,
                    expected_eval_run_id=attempt_id,
                    error_type="LeaseExpired",
                    operator="runtime-reconciler",
                )
                failed.append(change_set_id)
        except Exception:
            try:
                governance.fail_regression(
                    change_set_id,
                    expected_eval_run_id=attempt_id,
                    error_type="ReconciliationError",
                    operator="runtime-reconciler",
                )
                failed.append(change_set_id)
            except Exception:
                errors.append(change_set_id)
    return {"completed": completed, "failed": failed, "errors": errors}


def start_regression(
    governance: RegressionTransitionTarget,
    change_set_id: str,
    *,
    eval_run_id: str,
    dataset_id: str,
    operator: str,
) -> JsonObject:
    clean_dataset_id = dataset_id.strip()
    if not clean_dataset_id:
        raise ConflictError("Regression dataset_id is required")
    return governance._transition_change_set(
        change_set_id,
        "regression_running",
        fields={
            "latest_eval_run_id": eval_run_id,
            "regression_attempt_id": eval_run_id,
            "latest_eval_run": None,
            "regression_dataset_id": clean_dataset_id,
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
    eval_run_id: str,
    operator: str,
) -> JsonObject:
    eval_run, regression_owner_id = _load_bound_regression_eval_run(governance, change_set_id, eval_run_id)
    result_status = str(eval_run.get("result_status") or "")
    if result_status in {"passed", "passed_with_notes"} and not governance._eval_run_publication_blocker(eval_run):
        target = "regression_passed"
    elif result_status in {"needs_human_review", "review_required"} and str((eval_run.get("gate_result") or {}).get("status") or "") == "review_required":
        target = "regression_review_required"
    else:
        target = "regression_failed"
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
        expected_fields={"latest_eval_run_id": regression_owner_id},
    )


def review_regression_result(
    governance: RegressionTransitionTarget,
    change_set_id: str,
    *,
    eval_run_id: str,
    review_id: str,
    operator: str,
    reason: str,
    scope: str,
    items: list[JsonObject],
) -> JsonObject:
    _load_bound_regression_eval_run(governance, change_set_id, eval_run_id)
    plan = governance._plan_regression_review(
        eval_run_id,
        review_id=review_id,
        operator=operator,
        reason=reason,
        scope=scope,
        items=items,
    )
    reviewed = plan.reviewed_record.model_dump(mode="json")
    target = "regression_passed" if plan.reviewed_record.result_status == "passed_with_notes" else "regression_failed"
    change_set = governance.get_change_set(change_set_id)
    if change_set is None:
        raise NotFoundError(f"Agent change set not found: {change_set_id}")
    if plan.idempotent:
        if change_set.get("status") != target or change_set.get("latest_eval_run_id") != eval_run_id:
            raise ConflictError(f"Reviewed EvalRun is not reflected by its Agent change set: {eval_run_id}")
        return reviewed
    if change_set.get("status") != "regression_review_required":
        raise ConflictError(f"Agent change set is not awaiting regression review: {change_set_id}")
    governance._transition_change_set(
        change_set_id,
        target,
        fields={"latest_eval_run_id": eval_run_id, "latest_eval_run": reviewed, "regression_error": None},
        action="regression_review_approved" if target == "regression_passed" else "regression_review_rejected",
        operator=operator,
        expected_fields={"latest_eval_run_id": eval_run_id},
        transaction_mutation=lambda db: governance._apply_regression_review(db, plan),
    )
    return reviewed


def _load_bound_regression_eval_run(
    governance: RegressionTransitionTarget,
    change_set_id: str,
    eval_run_id: str,
) -> tuple[JsonObject, str]:
    change_set = governance.get_change_set(change_set_id)
    if change_set is None:
        raise NotFoundError(f"Agent change set not found: {change_set_id}")
    persisted = governance._get_persisted_regression_eval_run(eval_run_id)
    if persisted is None:
        raise ConflictError(f"Persisted EvalRun not found for regression completion: {eval_run_id}")
    try:
        record = EvalRunProjectionRecord.model_validate(persisted)
    except ValidationError as exc:
        raise DataIntegrityError(f"Persisted EvalRun is invalid: {eval_run_id}") from exc

    candidate_commit = str(change_set.get("candidate_commit_sha") or "").strip()
    expected_agent_id = str(change_set.get("agent_id") or "").strip()
    expected_worktree = str(change_set.get("worktree_path") or "").strip()
    regression_owner_id = str(change_set.get("latest_eval_run_id") or "").strip()
    regression_attempt_id = str(change_set.get("regression_attempt_id") or regression_owner_id).strip()
    regression_started_at = str(change_set.get("regression_started_at") or "").strip()
    if (
        not candidate_commit
        or not expected_agent_id
        or not expected_worktree
        or not Path(expected_worktree).is_absolute()
        or not regression_owner_id
        or not regression_attempt_id
        or not regression_started_at
    ):
        raise DataIntegrityError(f"Agent change set lacks regression binding fields: {change_set_id}")
    bindings = (
        ("change_set_id", record.change_set_id, change_set_id),
        ("regression_attempt_id", record.regression_attempt_id, regression_attempt_id),
        ("agent_id", record.agent_id, expected_agent_id),
        ("candidate_commit_sha", record.candidate_commit_sha, candidate_commit),
        ("agent_version_id", record.agent_version_id, candidate_commit),
        ("candidate_worktree_path", record.candidate_worktree_path, expected_worktree),
        ("dataset_id", record.dataset_id, str(change_set.get("regression_dataset_id") or "").strip()),
        ("dataset_snapshot.agent_id", record.dataset_snapshot.agent_id, expected_agent_id),
        (
            "dataset_snapshot.source_improvement_id",
            record.dataset_snapshot.source_improvement_id,
            str(change_set.get("source_improvement_id") or "").strip(),
        ),
        (
            "dataset_snapshot.provenance.execution_id",
            record.dataset_snapshot.provenance.execution_id,
            str(change_set.get("execution_job_id") or "").strip(),
        ),
        (
            "dataset_snapshot.provenance.candidate_agent_version_id",
            record.dataset_snapshot.provenance.candidate_agent_version_id,
            candidate_commit,
        ),
    )
    for field, actual, expected in bindings:
        if not actual or actual != expected:
            raise ConflictError(f"Persisted EvalRun {field} does not match Agent change set: {eval_run_id}")
    if record.source != "agent_change_set_regression":
        raise ConflictError(f"Persisted EvalRun source is not agent_change_set_regression: {eval_run_id}")
    if not record.dataset_id.strip() or record.dataset_snapshot.dataset_id != record.dataset_id:
        raise DataIntegrityError(f"Persisted EvalRun dataset binding is invalid: {eval_run_id}")
    if record.status not in {"completed", "failed"}:
        raise ConflictError(f"Persisted EvalRun is not terminal: {eval_run_id}")
    run_created_at = _parse_timestamp(record.created_at, field="EvalRun.created_at")
    attempt_started_at = _parse_timestamp(regression_started_at, field="AgentChangeSet.regression_started_at")
    run_completed_at = _parse_timestamp(record.completed_at or "", field="EvalRun.completed_at")
    if run_created_at < attempt_started_at:
        raise ConflictError(f"Persisted EvalRun predates the current regression attempt: {eval_run_id}")
    if run_completed_at < run_created_at:
        raise DataIntegrityError(f"Persisted EvalRun completed before it was created: {eval_run_id}")

    _validate_projection_items(record, eval_run_id=eval_run_id)
    if record.result_status in {"passed", "passed_with_notes"} and record.status != "completed":
        raise DataIntegrityError(f"Persisted EvalRun cannot pass from status {record.status}: {eval_run_id}")
    return record.model_dump(mode="json"), regression_owner_id


def _validate_projection_items(record: EvalRunProjectionRecord, *, eval_run_id: str) -> None:
    snapshot_cases = {case.case_id: case for case in record.dataset_snapshot.cases}
    if len(snapshot_cases) != len(record.dataset_snapshot.cases):
        raise DataIntegrityError(f"Persisted EvalRun dataset snapshot contains duplicate cases: {eval_run_id}")
    item_case_ids: set[str] = set()
    for item in record.items:
        expected_case = snapshot_cases.get(item.dataset_case_id)
        if item.eval_run_id != record.eval_run_id:
            raise DataIntegrityError(f"Persisted EvalRun item belongs to a different run: {item.eval_run_item_id}")
        if not item.agent_version_id or item.agent_version_id != record.agent_version_id:
            raise ConflictError(f"Persisted EvalRun item agent version does not match its run: {item.eval_run_item_id}")
        if expected_case is None or item.dataset_case_snapshot != expected_case:
            raise ConflictError(f"Persisted EvalRun item does not match its dataset snapshot: {item.eval_run_item_id}")
        if item.dataset_case_id in item_case_ids:
            raise DataIntegrityError(f"Persisted EvalRun contains duplicate dataset case items: {item.dataset_case_id}")
        item_case_ids.add(item.dataset_case_id)
    if record.status == "completed" and item_case_ids != set(snapshot_cases):
        raise ConflictError(f"Persisted EvalRun did not execute the complete dataset snapshot: {eval_run_id}")


def _parse_timestamp(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone offset is required")
        return parsed
    except ValueError as exc:
        raise DataIntegrityError(f"{field} is not a timezone-aware ISO-8601 timestamp") from exc


def record_regression_failure(
    governance: RegressionTransitionTarget,
    change_set_id: str,
    *,
    expected_eval_run_id: str,
    error_type: str,
    operator: str,
) -> JsonObject:
    now = utc_now()
    eval_run = governance._get_persisted_regression_eval_run_by_attempt(expected_eval_run_id)
    persisted_eval_run_id = str(eval_run.get("eval_run_id") or "").strip() if eval_run else ""
    return governance._transition_change_set(
        change_set_id,
        "regression_failed",
        fields={
            "latest_eval_run_id": persisted_eval_run_id or None,
            "latest_eval_run": eval_run,
            "regression_error": {"error_type": error_type, "updated_at": now},
        },
        action="regression_failed",
        operator=operator,
        expected_fields={"latest_eval_run_id": expected_eval_run_id},
    )
