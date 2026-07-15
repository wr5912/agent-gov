from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from ..errors import ConflictError, DataIntegrityError, NotFoundError
from ..improvement_db import ExecutionRecordModel, ImprovementItemModel
from ..json_types import JsonObject
from ..records.eval_run_records import (
    EvalRunGateResultRecord,
    EvalRunItemRecord,
    EvalRunProjectionRecord,
    EvalRunRecord,
    EvalRunReviewDecisionRecord,
    EvalRunReviewItemDecisionRecord,
    EvalRunSummaryRecord,
)
from ..runtime_db import (
    AgentChangeSetModel,
    EvalRunItemModel,
    EvalRunModel,
    TestDatasetModel,
    utc_now,
)
from ..runtime_db_base import begin_sqlite_write_transaction
from ..runtime_recovery import runtime_operation_heartbeat, runtime_operation_is_stale
from ..test_dataset_schemas import TestCaseRecord, TestDatasetRecord
from .test_dataset_store import project_test_dataset_record, transition_test_dataset_lifecycle_row

_RUNTIME_HEARTBEAT_FIELD = "runtime_heartbeat_at"


@dataclass(frozen=True)
class EvalRunReviewPlan:
    original_record: EvalRunProjectionRecord
    reviewed_record: EvalRunProjectionRecord
    idempotent: bool


def _validate_regression_eval_request(
    *,
    source: str,
    change_set_id: Optional[str],
    regression_attempt_id: Optional[str],
    agent_version_id: Optional[str],
    candidate_commit_sha: Optional[str],
    candidate_worktree_path: Optional[str],
) -> None:
    if source != "agent_change_set_regression":
        if regression_attempt_id:
            raise ConflictError("regression_attempt_id is only valid for Agent change set regression EvalRuns")
        return
    bindings = (change_set_id, regression_attempt_id, candidate_commit_sha, candidate_worktree_path)
    if any(not str(value or "").strip() for value in bindings):
        raise ConflictError("Agent change set regression EvalRun requires complete backend-owned bindings")
    if agent_version_id != candidate_commit_sha:
        raise ConflictError("Agent change set regression EvalRun version must equal its candidate commit")
    if not Path(str(candidate_worktree_path)).is_absolute():
        raise ConflictError("Agent change set regression EvalRun requires an absolute candidate worktree path")


def _resolve_eval_run_agent(
    change_set: AgentChangeSetModel | None,
    *,
    requested_change_set_id: Optional[str],
    requested_agent_id: Optional[str],
    source: str,
    dataset_id: str,
    regression_attempt_id: Optional[str],
    candidate_commit_sha: Optional[str],
    candidate_worktree_path: Optional[str],
) -> Optional[str]:
    if requested_change_set_id and change_set is None:
        raise ConflictError(f"Agent change set does not exist for EvalRun: {requested_change_set_id}")
    if change_set is None:
        return requested_agent_id
    if requested_agent_id and requested_agent_id != change_set.agent_id:
        raise ConflictError(f"Requested EvalRun Agent does not match Agent change set: {requested_change_set_id}")
    if source == "agent_change_set_regression":
        payload = dict(change_set.payload_json or {})
        expected = {
            "change_set status": (change_set.status, "regression_running"),
            "candidate commit": (change_set.candidate_commit_sha, candidate_commit_sha),
            "candidate worktree": (change_set.worktree_path, candidate_worktree_path),
            "payload change set": (payload.get("change_set_id"), change_set.change_set_id),
            "regression attempt": (payload.get("latest_eval_run_id"), regression_attempt_id),
            "regression dataset": (payload.get("regression_dataset_id"), dataset_id),
        }
        drifted = [field for field, values in expected.items() if not values[0] or values[0] != values[1]]
        if drifted:
            raise ConflictError(f"Agent change set regression binding drifted ({', '.join(drifted)}): {requested_change_set_id}")
    return change_set.agent_id


def _new_eval_run_record(
    *,
    eval_run_id: str,
    dataset_id: str,
    dataset_snapshot: TestDatasetRecord,
    created_at: str,
    agent_id: str,
    agent_version_id: Optional[str],
    source: str,
    change_set_id: Optional[str],
    regression_attempt_id: Optional[str],
    candidate_commit_sha: Optional[str],
    candidate_worktree_path: Optional[str],
) -> EvalRunRecord:
    return EvalRunRecord(
        eval_run_id=eval_run_id,
        dataset_id=dataset_id,
        dataset_snapshot=dataset_snapshot,
        created_at=created_at,
        completed_at=None,
        status="running",
        result_status="running",
        agent_id=agent_id,
        agent_version_id=agent_version_id,
        source=source,
        change_set_id=change_set_id,
        regression_attempt_id=regression_attempt_id,
        candidate_commit_sha=candidate_commit_sha,
        candidate_worktree_path=candidate_worktree_path,
        runtime_heartbeat_at=created_at,
        summary={"total": len(dataset_snapshot.cases)},
        gate_result={
            "status": "running",
            "blocked_dataset_case_ids": [],
            "review_dataset_case_ids": [],
            "note_dataset_case_ids": [],
        },
    )


def _validated_eval_dataset(
    db: Any,
    *,
    dataset_id: str,
    requested_agent_id: str | None,
    source: str,
    change_set: AgentChangeSetModel | None,
    candidate_commit_sha: str | None,
) -> tuple[TestDatasetModel, TestDatasetRecord]:
    dataset = db.get(TestDatasetModel, dataset_id)
    if dataset is None:
        raise NotFoundError(f"TestDataset not found: {dataset_id}")
    if dataset.owner_kind != "business_agent" or dataset.owner_id != dataset.agent_id:
        raise DataIntegrityError(f"TestDataset owner is inconsistent: {dataset_id}")
    if requested_agent_id and dataset.agent_id != requested_agent_id:
        raise ConflictError(f"TestDataset agent {dataset.agent_id} does not match EvalRun agent {requested_agent_id}: {dataset_id}")
    if dataset.lifecycle_state != "active":
        raise ConflictError(f"Only active TestDataset can start an EvalRun; current state is {dataset.lifecycle_state}: {dataset_id}")
    snapshot = project_test_dataset_record(db, dataset)
    if not snapshot.cases:
        raise ConflictError(f"TestDataset has no cases and cannot start an EvalRun: {dataset_id}")
    if source == "agent_change_set_regression":
        if change_set is None:
            raise ConflictError("Agent change set is required for regression TestDataset validation")
        payload = dict(change_set.payload_json or {})
        expected = {
            "source improvement": (snapshot.source_improvement_id, str(payload.get("source_improvement_id") or "")),
            "source execution": (snapshot.provenance.execution_id, str(change_set.execution_job_id or "")),
            "candidate version": (snapshot.provenance.candidate_agent_version_id, str(candidate_commit_sha or "")),
        }
        drifted = [field for field, values in expected.items() if not values[1] or values[0] != values[1]]
        if drifted:
            raise ConflictError(f"TestDataset regression provenance drifted ({', '.join(drifted)}): {dataset_id}")
        improvement = db.get(ImprovementItemModel, snapshot.source_improvement_id)
        execution = db.get(ExecutionRecordModel, snapshot.provenance.execution_id)
        if (
            improvement is None
            or improvement.agent_id != dataset.agent_id
            or execution is None
            or execution.status != "confirmed"
            or execution.improvement_id != snapshot.source_improvement_id
            or execution.change_set_id != change_set.change_set_id
            or execution.applied_agent_version_id != candidate_commit_sha
        ):
            raise ConflictError(f"TestDataset regression source evidence is missing or inconsistent: {dataset_id}")
    return dataset, snapshot


def _release_eval_dataset(db: Any, record: EvalRunRecord) -> None:
    dataset = db.get(TestDatasetModel, record.dataset_id)
    if dataset is None:
        raise DataIntegrityError(f"EvalRun TestDataset disappeared: {record.dataset_id}")
    if dataset.lifecycle_state != "evaluating":
        return
    if dataset.revision != record.dataset_snapshot.revision:
        raise DataIntegrityError(f"EvalRun TestDataset revision drifted while evaluating: {record.dataset_id}")
    transition_test_dataset_lifecycle_row(
        db,
        dataset,
        target_state="active",
        expected_revision=dataset.revision,
        operator="eval-run",
        reason=f"eval_run_terminal:{record.eval_run_id}",
    )


def _normalized_review_decision(
    *,
    review_id: str,
    operator: str,
    reason: str,
    scope: str,
    items: list[JsonObject],
    created_at: str,
) -> EvalRunReviewDecisionRecord:
    normalized_items = sorted(
        (
            EvalRunReviewItemDecisionRecord.model_validate(
                {
                    "dataset_case_id": str(item.get("dataset_case_id") or "").strip(),
                    "decision": str(item.get("decision") or "").strip(),
                    "note": str(item.get("note") or "").strip(),
                }
            )
            for item in items
        ),
        key=lambda item: item.dataset_case_id,
    )
    return EvalRunReviewDecisionRecord.model_validate(
        {
            "review_id": review_id.strip(),
            "operator": operator.strip(),
            "reason": reason.strip(),
            "scope": scope,
            "items": normalized_items,
            "created_at": created_at,
        }
    )


def _same_review_decision(
    existing: EvalRunReviewDecisionRecord,
    requested: EvalRunReviewDecisionRecord,
) -> bool:
    return existing.model_copy(update={"created_at": requested.created_at}) == requested


def _build_eval_run_review_plan(
    persisted: JsonObject,
    *,
    review_id: str,
    operator: str,
    reason: str,
    scope: str,
    items: list[JsonObject],
) -> EvalRunReviewPlan:
    try:
        current = EvalRunProjectionRecord.model_validate(persisted)
    except ValueError as exc:
        raise DataIntegrityError("Persisted EvalRun cannot be reviewed because its projection is invalid") from exc
    existing = current.gate_result.review_decision
    requested = _normalized_review_decision(
        review_id=review_id,
        operator=operator,
        reason=reason,
        scope=scope,
        items=items,
        created_at=existing.created_at if existing else utc_now(),
    )
    if existing is not None:
        if not _same_review_decision(existing, requested):
            raise ConflictError(f"EvalRun already has a different review decision: {current.eval_run_id}")
        return EvalRunReviewPlan(
            original_record=current,
            reviewed_record=current,
            idempotent=True,
        )
    return _new_eval_run_review_plan(current, requested)


def _new_eval_run_review_plan(
    current: EvalRunProjectionRecord,
    review: EvalRunReviewDecisionRecord,
) -> EvalRunReviewPlan:
    if current.status != "completed" or current.result_status not in {"needs_human_review", "review_required"}:
        raise ConflictError(f"EvalRun is not awaiting human review: {current.eval_run_id}")
    if current.gate_result.status != "review_required" or current.gate_result.blocked_dataset_case_ids:
        raise ConflictError(f"EvalRun gate is not reviewable: {current.eval_run_id}")
    expected_case_ids = set(current.gate_result.review_dataset_case_ids)
    submitted_case_ids = {item.dataset_case_id for item in review.items}
    if not expected_case_ids or submitted_case_ids != expected_case_ids:
        raise ConflictError(f"EvalRun review must cover exactly the pending review cases: {current.eval_run_id}")
    item_by_case = {item.dataset_case_id: item for item in current.items}
    if any(
        case_id not in item_by_case
        or item_by_case[case_id].status != "needs_human_review"
        or any(not check.passed for check in item_by_case[case_id].check_results if check.required)
        for case_id in expected_case_ids
    ):
        raise ConflictError(f"EvalRun review cannot override missing evidence or failed required checks: {current.eval_run_id}")
    return _project_reviewed_eval_run(current, review)


def _project_reviewed_eval_run(
    current: EvalRunProjectionRecord,
    review: EvalRunReviewDecisionRecord,
) -> EvalRunReviewPlan:
    accepted = [item.dataset_case_id for item in review.items if item.decision == "approve"]
    rejected = [item.dataset_case_id for item in review.items if item.decision == "reject"]
    summary = EvalRunSummaryRecord(
        total=current.summary.total,
        passed=current.summary.passed,
        failed=current.summary.failed,
        needs_human_review=current.summary.needs_human_review,
        blocked=len(rejected),
        review_required=0,
        passed_with_notes=len(accepted),
    )
    gate_result = EvalRunGateResultRecord(
        status="blocked" if rejected else "passed_with_notes",
        blocked_dataset_case_ids=rejected,
        review_dataset_case_ids=[],
        note_dataset_case_ids=accepted,
        review_decision=review,
    )
    reviewed = EvalRunProjectionRecord(
        eval_run_id=current.eval_run_id,
        dataset_id=current.dataset_id,
        dataset_snapshot=current.dataset_snapshot,
        created_at=current.created_at,
        completed_at=current.completed_at,
        status=current.status,
        result_status="failed" if rejected else "passed_with_notes",
        agent_id=current.agent_id,
        agent_version_id=current.agent_version_id,
        source=current.source,
        change_set_id=current.change_set_id,
        regression_attempt_id=current.regression_attempt_id,
        candidate_commit_sha=current.candidate_commit_sha,
        candidate_worktree_path=current.candidate_worktree_path,
        summary=summary,
        gate_result=gate_result,
        error_json=current.error_json,
        items=current.items,
    )
    return EvalRunReviewPlan(
        original_record=current,
        reviewed_record=reviewed,
        idempotent=False,
    )


def _persist_eval_run_terminal(db: Any, run: EvalRunModel, record: EvalRunRecord) -> bool:
    changed = db.execute(
        update(EvalRunModel)
        .where(
            EvalRunModel.eval_run_id == run.eval_run_id,
            EvalRunModel.status == "running",
            EvalRunModel.payload_json == dict(run.payload_json or {}),
        )
        .values(status=record.status, completed_at=record.completed_at, payload_json=record.to_payload())
        .execution_options(synchronize_session=False)
    ).rowcount
    if changed != 1:
        return False
    _release_eval_dataset(db, record)
    return True


def _running_eval_payload(record: EvalRunRecord) -> JsonObject:
    payload = record.to_payload()
    payload[_RUNTIME_HEARTBEAT_FIELD] = record.runtime_heartbeat_at or record.created_at
    return payload


def _eval_run_heartbeat(row: EvalRunModel) -> str:
    value = (row.payload_json or {}).get(_RUNTIME_HEARTBEAT_FIELD)
    return str(value) if value else row.created_at


class FeedbackEvalStoreMixin:
    """Store operations for immutable typed TestDataset evaluation runs."""

    def create_eval_run(
        self,
        *,
        dataset_id: str,
        agent_version_id: Optional[str],
        source: str = "manual_feedback_dataset",
        change_set_id: Optional[str] = None,
        regression_attempt_id: Optional[str] = None,
        candidate_commit_sha: Optional[str] = None,
        candidate_worktree_path: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> JsonObject:
        created_at = utc_now()
        _validate_regression_eval_request(
            source=source,
            change_set_id=change_set_id,
            regression_attempt_id=regression_attempt_id,
            agent_version_id=agent_version_id,
            candidate_commit_sha=candidate_commit_sha,
            candidate_worktree_path=candidate_worktree_path,
        )
        with self.Session.begin() as db:
            change_set = db.get(AgentChangeSetModel, change_set_id) if change_set_id else None
            requested_agent_id = _resolve_eval_run_agent(
                change_set,
                requested_change_set_id=change_set_id,
                requested_agent_id=agent_id,
                source=source,
                dataset_id=dataset_id,
                regression_attempt_id=regression_attempt_id,
                candidate_commit_sha=candidate_commit_sha,
                candidate_worktree_path=candidate_worktree_path,
            )
            dataset, dataset_snapshot = _validated_eval_dataset(
                db,
                dataset_id=dataset_id,
                requested_agent_id=requested_agent_id,
                source=source,
                change_set=change_set,
                candidate_commit_sha=candidate_commit_sha,
            )
            resolved_agent_id = requested_agent_id or dataset.agent_id
            resolved_agent_version_id = agent_version_id or self._current_agent_version_id(resolved_agent_id)
            eval_run_id = f"evr-{uuid.uuid4()}"
            dataset = transition_test_dataset_lifecycle_row(
                db,
                dataset,
                target_state="evaluating",
                expected_revision=dataset.revision,
                operator="eval-run",
                reason=f"eval_run_started:{eval_run_id}",
            )
            dataset_snapshot = project_test_dataset_record(db, dataset)
            record = _new_eval_run_record(
                eval_run_id=eval_run_id,
                dataset_id=dataset_id,
                dataset_snapshot=dataset_snapshot,
                created_at=created_at,
                agent_id=resolved_agent_id,
                agent_version_id=resolved_agent_version_id,
                source=source,
                change_set_id=change_set_id,
                regression_attempt_id=regression_attempt_id,
                candidate_commit_sha=candidate_commit_sha,
                candidate_worktree_path=candidate_worktree_path,
            )
            db.add(
                EvalRunModel(
                    eval_run_id=record.eval_run_id,
                    dataset_id=record.dataset_id,
                    created_at=created_at,
                    completed_at=None,
                    status=record.status,
                    agent_id=record.agent_id,
                    agent_version_id=record.agent_version_id,
                    source=record.source,
                    payload_json=_running_eval_payload(record),
                )
            )
        return record.to_payload()

    def validate_regression_eval_dataset(
        self,
        *,
        dataset_id: str,
        change_set_id: str,
        candidate_commit_sha: str,
    ) -> None:
        with self.Session.begin() as db:
            change_set = db.get(AgentChangeSetModel, change_set_id)
            if change_set is None:
                raise ConflictError(f"Agent change set does not exist for EvalRun: {change_set_id}")
            if change_set.candidate_commit_sha != candidate_commit_sha:
                raise ConflictError(f"Agent change set candidate changed before regression: {change_set_id}")
            _validated_eval_dataset(
                db,
                dataset_id=dataset_id,
                requested_agent_id=change_set.agent_id,
                source="agent_change_set_regression",
                change_set=change_set,
                candidate_commit_sha=candidate_commit_sha,
            )

    def reconcile_orphan_eval_runs(self, *, now: str | None = None) -> list[str]:
        with self.Session() as db:
            eval_run_ids = list(db.scalars(select(EvalRunModel.eval_run_id).where(EvalRunModel.status == "running")).all())
        reconciled: list[str] = []
        for eval_run_id in eval_run_ids:
            failed = self._fail_eval_run(
                eval_run_id,
                error_code="EVAL_RUN_LEASE_EXPIRED",
                message="EvalRun heartbeat expired before terminal completion",
                recovery_now=now or utc_now(),
            )
            if failed is not None:
                reconciled.append(eval_run_id)
        return reconciled

    def renew_eval_run_lease(self, eval_run_id: str, *, now: str | None = None) -> bool:
        heartbeat_at = runtime_operation_heartbeat(now=now)
        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            run = db.get(EvalRunModel, eval_run_id)
            if run is None or run.status != "running":
                return False
            payload = dict(run.payload_json or {})
            payload[_RUNTIME_HEARTBEAT_FIELD] = heartbeat_at
            run.payload_json = payload
        return True

    def append_eval_run_item(
        self,
        eval_run_id: str,
        *,
        dataset_case: TestCaseRecord,
        agent_result: Optional[JsonObject],
        status: str,
        score: float,
        check_results: list[JsonObject],
        error_json: Optional[JsonObject] = None,
    ) -> Optional[JsonObject]:
        item_id = f"evi-{uuid.uuid4()}"
        answer = self._string((agent_result or {}).get("answer"))
        answer_summary = answer.strip().replace("\n", " ")[:500] if answer else ""
        try:
            with self.Session.begin() as db:
                begin_sqlite_write_transaction(db.connection())
                run = db.get(EvalRunModel, eval_run_id)
                if run is None:
                    return None
                run_record = EvalRunRecord.from_row(run)
                if run_record.status != "running":
                    raise ConflictError(f"EvalRun is already terminal and cannot accept items: {eval_run_id}")
                expected_case = next(
                    (case for case in run_record.dataset_snapshot.cases if case.case_id == dataset_case.case_id),
                    None,
                )
                if expected_case is None or expected_case != dataset_case:
                    raise ConflictError(f"Dataset case does not belong to the immutable EvalRun snapshot: {dataset_case.case_id}")
                actual_agent_version_id = (agent_result or {}).get("agent_version_id")
                if not run_record.agent_version_id or actual_agent_version_id != run_record.agent_version_id:
                    raise ConflictError(f"EvalRun item agent version does not match its run: {eval_run_id}")
                item_record = EvalRunItemRecord(
                    eval_run_item_id=item_id,
                    eval_run_id=eval_run_id,
                    dataset_case_id=dataset_case.case_id,
                    agent_run_id=(agent_result or {}).get("run_id"),
                    agent_version_id=run_record.agent_version_id,
                    status=status,
                    score=score,
                    check_results=check_results,
                    dataset_case_snapshot=dataset_case,
                    answer_summary=answer_summary,
                    error_json=error_json,
                    created_at=utc_now(),
                )
                db.add(
                    EvalRunItemModel(
                        eval_run_item_id=item_record.eval_run_item_id,
                        eval_run_id=item_record.eval_run_id,
                        dataset_case_id=item_record.dataset_case_id,
                        agent_run_id=item_record.agent_run_id,
                        status=item_record.status,
                        score=item_record.score,
                        payload_json=item_record.to_payload(),
                    )
                )
                payload = dict(run.payload_json or {})
                payload[_RUNTIME_HEARTBEAT_FIELD] = runtime_operation_heartbeat()
                run.payload_json = payload
        except IntegrityError as exc:
            raise ConflictError(f"Dataset case already has an item in EvalRun {eval_run_id}: {dataset_case.case_id}") from exc
        return item_record.to_payload()

    def finish_eval_run(self, eval_run_id: str) -> Optional[JsonObject]:
        completed_at = utc_now()
        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            run = db.get(EvalRunModel, eval_run_id)
            if not run:
                return None
            if run.status != "running":
                return self._eval_run_to_dict(run)
            items = list(db.scalars(select(EvalRunItemModel).where(EvalRunItemModel.eval_run_id == eval_run_id)).all())
            record = EvalRunRecord.from_row(run)
            expected_case_ids = {case.case_id for case in record.dataset_snapshot.cases}
            completed_case_ids = {item.dataset_case_id for item in items}
            if completed_case_ids != expected_case_ids:
                missing = sorted(expected_case_ids - completed_case_ids)
                error_json = {
                    "error_code": "EVAL_RUN_INCOMPLETE_DATASET_SNAPSHOT",
                    "message": f"EvalRun did not execute every dataset snapshot case: {', '.join(missing)}",
                    "created_at": completed_at,
                    "eval_run_id": eval_run_id,
                }
                record = record.transition_to(
                    "failed",
                    fields={"result_status": "failed", "completed_at": completed_at, "error_json": error_json},
                )
            else:
                summary = self._eval_run_summary(items)
                gate_result = self._gate_result_for_items(items)
                gated = bool(record.change_set_id or record.source == "agent_change_set_regression")
                result_status = gate_result["status"] if gated else self._eval_result_status(summary)
                record = record.transition_to(
                    "completed",
                    fields={
                        "completed_at": completed_at,
                        "result_status": result_status,
                        "summary": summary,
                        "gate_result": gate_result,
                    },
                )
            if not _persist_eval_run_terminal(db, run, record):
                raise ConflictError(f"EvalRun changed during terminal transition: {eval_run_id}")
        finished = self.get_eval_run(eval_run_id)
        return finished

    def fail_eval_run(self, eval_run_id: str, *, error_code: str, message: str) -> Optional[JsonObject]:
        return self._fail_eval_run(eval_run_id, error_code=error_code, message=message)

    def _fail_eval_run(
        self,
        eval_run_id: str,
        *,
        error_code: str,
        message: str,
        recovery_now: str | None = None,
    ) -> Optional[JsonObject]:
        completed_at = recovery_now or utc_now()
        error_json = {"error_code": error_code, "message": message, "created_at": completed_at, "eval_run_id": eval_run_id}
        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            run = db.get(EvalRunModel, eval_run_id)
            if not run:
                return None
            if run.status != "running":
                return None if recovery_now is not None else self._eval_run_to_dict(run)
            if recovery_now is not None and not runtime_operation_is_stale(_eval_run_heartbeat(run), now=recovery_now):
                return None
            record = EvalRunRecord.from_row(run).transition_to(
                "failed",
                fields={"result_status": "failed", "completed_at": completed_at, "error_json": error_json},
            )
            if not _persist_eval_run_terminal(db, run, record):
                raise ConflictError(f"EvalRun changed during terminal transition: {eval_run_id}")
        failed = self.get_eval_run(eval_run_id)
        return failed

    def list_eval_runs(
        self,
        *,
        agent_version_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[JsonObject]:
        stmt = select(EvalRunModel).order_by(EvalRunModel.created_at.desc()).limit(limit)
        if agent_version_id:
            stmt = stmt.where(EvalRunModel.agent_version_id == agent_version_id)
        if agent_id:
            stmt = stmt.where(EvalRunModel.agent_id == agent_id)
        if status:
            stmt = stmt.where(EvalRunModel.status == status)
        with self.Session() as db:
            return [self._eval_run_to_dict(row) for row in db.scalars(stmt).all()]

    def get_eval_run(self, eval_run_id: str) -> Optional[JsonObject]:
        if not eval_run_id:
            return None
        with self.Session() as db:
            row = db.get(EvalRunModel, eval_run_id)
            return self._eval_run_to_dict(row) if row else None

    def plan_eval_run_review(
        self,
        eval_run_id: str,
        *,
        review_id: str,
        operator: str,
        reason: str,
        scope: str,
        items: list[JsonObject],
    ) -> EvalRunReviewPlan:
        persisted = self.get_eval_run(eval_run_id)
        if persisted is None:
            raise NotFoundError(f"EvalRun not found: {eval_run_id}")
        return _build_eval_run_review_plan(
            persisted,
            review_id=review_id,
            operator=operator,
            reason=reason,
            scope=scope,
            items=items,
        )

    @staticmethod
    def apply_eval_run_review(db: Any, plan: EvalRunReviewPlan) -> None:
        if plan.idempotent:
            return
        changed = db.execute(
            update(EvalRunModel)
            .where(
                EvalRunModel.eval_run_id == plan.reviewed_record.eval_run_id,
                EvalRunModel.status == "completed",
                EvalRunModel.payload_json == plan.original_record.model_dump(mode="json", exclude={"items"}),
            )
            .values(payload_json=plan.reviewed_record.model_dump(mode="json", exclude={"items"}))
            .execution_options(synchronize_session=False)
        ).rowcount
        if changed != 1:
            raise ConflictError(f"EvalRun changed during human review: {plan.reviewed_record.eval_run_id}")

    def _get_eval_run_by_regression_attempt_id(self, regression_attempt_id: str) -> Optional[JsonObject]:
        if not regression_attempt_id:
            return None
        with self.Session() as db:
            rows = list(
                db.scalars(
                    select(EvalRunModel)
                    .where(EvalRunModel.payload_json["regression_attempt_id"].as_string() == regression_attempt_id)
                    .order_by(EvalRunModel.created_at.desc())
                    .limit(2)
                ).all()
            )
        if len(rows) > 1:
            raise DataIntegrityError(f"Regression attempt owns multiple EvalRuns: {regression_attempt_id}")
        return self._eval_run_to_dict(rows[0]) if rows else None

    def _eval_run_to_dict(self, row: EvalRunModel) -> JsonObject:
        record = EvalRunRecord.from_row(row)
        with self.Session() as db:
            items = [
                EvalRunItemRecord.from_row(item).to_payload()
                for item in db.scalars(
                    select(EvalRunItemModel).where(EvalRunItemModel.eval_run_id == row.eval_run_id).order_by(EvalRunItemModel.eval_run_item_id.asc())
                ).all()
            ]
        return record.to_response(items=items)

    def _eval_run_summary(self, items: list[EvalRunItemModel]) -> JsonObject:
        return {
            "total": len(items),
            "passed": sum(1 for item in items if item.status == "passed"),
            "failed": sum(1 for item in items if item.status == "failed"),
            "needs_human_review": sum(1 for item in items if item.status == "needs_human_review"),
        }

    def _gate_result_for_items(self, items: list[EvalRunItemModel]) -> JsonObject:
        blocked: list[str] = []
        review: list[str] = []
        for item in items:
            record = EvalRunItemRecord.from_row(item)
            if item.status == "needs_human_review":
                review.append(record.dataset_case_id)
            elif item.status == "failed":
                blocked.append(record.dataset_case_id)
        return {
            "status": "blocked" if blocked else "review_required" if review else "passed",
            "blocked_dataset_case_ids": blocked,
            "review_dataset_case_ids": review,
            "note_dataset_case_ids": [],
        }

    def _eval_result_status(self, summary: dict[str, int]) -> str:
        if summary["failed"]:
            return "failed"
        if summary["needs_human_review"]:
            return "needs_human_review"
        if summary["passed"] == summary["total"] and summary["total"]:
            return "passed"
        return "needs_human_review"
