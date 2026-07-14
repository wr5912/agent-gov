from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import sessionmaker

from ..errors import BusinessRuleViolation, ConflictError, DataIntegrityError, NotFoundError
from ..improvement_content_schemas import RegressionCase
from ..improvement_db import (
    AttributionModel,
    ExecutionRecordModel,
    ImprovementFeedbackModel,
    ImprovementItemModel,
    NormalizedFeedbackModel,
    OptimizationPlanModel,
    RegressionAssessmentModel,
)
from ..json_types import JsonObject
from ..runtime_db import AgentChangeSetModel, EvalRunModel, TestDatasetCaseModel, TestDatasetModel, TestDatasetRevisionModel, utc_now
from ..state_machines import validate_transition
from ..test_dataset_schemas import (
    TestCaseRecord,
    TestDatasetProvenanceRecord,
    TestDatasetRecord,
    TestDatasetRevisionRecord,
)

_ADOPTABLE_CHANGE_SET_STATES = {
    "candidate_committed",
    "pending_approval",
    "approved",
    "regression_running",
    "regression_passed",
    "regression_failed",
    "publishing",
    "published",
}


@dataclass(frozen=True)
class _AdoptionSource:
    improvement: ImprovementItemModel
    assessment: RegressionAssessmentModel
    normalized_feedback: NormalizedFeedbackModel
    attribution: AttributionModel
    optimization_plan: OptimizationPlanModel
    execution: ExecutionRecordModel
    feedback_ids: list[str]
    cases: list[RegressionCase]


def _dataset_id_for_source(source: _AdoptionSource, *, baseline_agent_version_id: str) -> str:
    fingerprint = {
        "agent_id": source.improvement.agent_id,
        "source_improvement_id": source.improvement.improvement_id,
        "regression_assessment": [source.assessment.regression_assessment_id, source.assessment.updated_at],
        "normalized_feedback": [source.normalized_feedback.normalized_feedback_id, source.normalized_feedback.updated_at],
        "attribution": [source.attribution.attribution_id, source.attribution.updated_at],
        "optimization_plan": [source.optimization_plan.optimization_plan_id, source.optimization_plan.updated_at],
        "execution": [
            source.execution.execution_id,
            source.execution.updated_at,
            source.execution.change_set_id,
            source.execution.applied_agent_version_id,
        ],
        "baseline_agent_version_id": baseline_agent_version_id,
        "source_feedback_ids": source.feedback_ids,
        "cases": [case.model_dump(mode="json") for case in source.cases],
    }
    encoded = json.dumps(fingerprint, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return f"tds-{hashlib.sha256(encoded).hexdigest()[:24]}"


def project_test_dataset_record(db: Any, row: TestDatasetModel) -> TestDatasetRecord:
    """Project one persisted dataset for API responses and immutable EvalRun snapshots."""
    if row.owner_kind != "business_agent" or row.owner_id != row.agent_id:
        raise DataIntegrityError(f"TestDataset owner is inconsistent: {row.dataset_id}")
    cases = db.scalars(select(TestDatasetCaseModel).where(TestDatasetCaseModel.dataset_id == row.dataset_id).order_by(TestDatasetCaseModel.position)).all()
    provenance_values = {
        "regression_assessment_id": row.source_regression_assessment_id or "",
        "regression_assessment_updated_at": row.source_regression_assessment_updated_at or "",
        "normalized_feedback_id": row.source_normalized_feedback_id or "",
        "normalized_feedback_updated_at": row.source_normalized_feedback_updated_at or "",
        "attribution_id": row.source_attribution_id or "",
        "attribution_updated_at": row.source_attribution_updated_at or "",
        "optimization_plan_id": row.source_optimization_plan_id or "",
        "optimization_plan_updated_at": row.source_optimization_plan_updated_at or "",
        "execution_id": row.source_execution_id or "",
        "execution_updated_at": row.source_execution_updated_at or "",
        "candidate_agent_version_id": row.candidate_agent_version_id or "",
    }
    missing_provenance = [field for field, value in provenance_values.items() if not value]
    if missing_provenance:
        raise DataIntegrityError(f"TestDataset provenance is incomplete ({', '.join(missing_provenance)}): {row.dataset_id}")
    provenance = TestDatasetProvenanceRecord(
        regression_assessment_id=provenance_values["regression_assessment_id"],
        regression_assessment_updated_at=provenance_values["regression_assessment_updated_at"],
        normalized_feedback_id=provenance_values["normalized_feedback_id"],
        normalized_feedback_updated_at=provenance_values["normalized_feedback_updated_at"],
        attribution_id=provenance_values["attribution_id"],
        attribution_updated_at=provenance_values["attribution_updated_at"],
        optimization_plan_id=provenance_values["optimization_plan_id"],
        optimization_plan_updated_at=provenance_values["optimization_plan_updated_at"],
        execution_id=provenance_values["execution_id"],
        execution_updated_at=provenance_values["execution_updated_at"],
        source_feedback_ids=list(row.source_feedback_ids_json or []),
        baseline_agent_version_id=row.baseline_agent_version_id or "",
        candidate_agent_version_id=provenance_values["candidate_agent_version_id"],
    )
    return TestDatasetRecord(
        dataset_id=row.dataset_id,
        agent_id=row.agent_id,
        owner_kind=row.owner_kind,
        owner_id=row.owner_id,
        source_improvement_id=row.source_improvement_id,
        name=row.name,
        description=row.description or "",
        scope=row.scope or "",
        revision=row.revision,
        lifecycle_state=row.lifecycle_state,
        quality_tags=list(row.quality_tags_json or []),
        provenance=provenance,
        cases=[
            TestCaseRecord(
                case_id=case.case_id,
                position=case.position,
                prompt=case.prompt,
                expected_behavior=case.expected_behavior or "",
                checkpoints=list(case.checkpoints_json or []),
            )
            for case in cases
        ],
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _test_dataset_lifecycle_snapshot(row: TestDatasetModel) -> JsonObject:
    return {
        "dataset_id": row.dataset_id,
        "agent_id": row.agent_id,
        "owner_kind": row.owner_kind,
        "owner_id": row.owner_id,
        "revision": row.revision,
        "lifecycle_state": row.lifecycle_state,
        "updated_at": row.updated_at,
    }


def transition_test_dataset_lifecycle_row(
    db: Any,
    row: TestDatasetModel,
    *,
    target_state: str,
    expected_revision: int,
    operator: str,
    reason: str,
) -> TestDatasetModel:
    if row.revision != expected_revision:
        raise ConflictError(f"TestDataset revision changed; expected {expected_revision}, current {row.revision}: {row.dataset_id}")
    validate_transition("test_dataset", row.lifecycle_state, target_state)
    if row.lifecycle_state == target_state:
        return row
    now = utc_now()
    previous_state = row.lifecycle_state
    next_revision = expected_revision + 1
    before = _test_dataset_lifecycle_snapshot(row)
    after = {
        **before,
        "revision": next_revision,
        "lifecycle_state": target_state,
        "updated_at": now,
    }
    changed = db.execute(
        update(TestDatasetModel)
        .where(
            TestDatasetModel.dataset_id == row.dataset_id,
            TestDatasetModel.revision == expected_revision,
            TestDatasetModel.lifecycle_state == previous_state,
        )
        .values(lifecycle_state=target_state, revision=next_revision, updated_at=now)
    ).rowcount
    if changed != 1:
        raise ConflictError(f"TestDataset changed during lifecycle transition: {row.dataset_id}")
    db.add(
        TestDatasetRevisionModel(
            revision_id=f"tdr-{uuid4().hex[:16]}",
            dataset_id=row.dataset_id,
            revision=next_revision,
            previous_lifecycle_state=previous_state,
            lifecycle_state=target_state,
            operator=operator,
            reason=reason,
            before_json=before,
            after_json=after,
            created_at=now,
        )
    )
    db.flush()
    db.expire(row)
    db.refresh(row)
    return row


class TestDatasetStore:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    def list_datasets(self, *, agent_id: str, source_improvement_id: str | None = None) -> list[TestDatasetRecord]:
        clean_agent = self._require_agent_id(agent_id)
        with self._session_factory.begin() as db:
            query = select(TestDatasetModel).where(TestDatasetModel.agent_id == clean_agent)
            if source_improvement_id:
                query = query.where(TestDatasetModel.source_improvement_id == source_improvement_id)
            rows = db.scalars(query.order_by(TestDatasetModel.created_at.desc(), TestDatasetModel.dataset_id)).all()
            return [self._record(db, row) for row in rows]

    def get_dataset(self, dataset_id: str, *, agent_id: str) -> TestDatasetRecord:
        clean_agent = self._require_agent_id(agent_id)
        with self._session_factory.begin() as db:
            row = db.get(TestDatasetModel, dataset_id)
            if row is None or row.agent_id != clean_agent:
                raise NotFoundError(f"TestDataset not found for agent {clean_agent}: {dataset_id}")
            return self._record(db, row)

    def list_revisions(self, dataset_id: str, *, agent_id: str) -> list[TestDatasetRevisionRecord]:
        clean_agent = self._require_agent_id(agent_id)
        with self._session_factory.begin() as db:
            row = db.get(TestDatasetModel, dataset_id)
            if row is None or row.agent_id != clean_agent:
                raise NotFoundError(f"TestDataset not found for agent {clean_agent}: {dataset_id}")
            revisions = db.scalars(
                select(TestDatasetRevisionModel).where(TestDatasetRevisionModel.dataset_id == dataset_id).order_by(TestDatasetRevisionModel.revision)
            ).all()
            return [self._revision_record(revision) for revision in revisions]

    def adopt_from_improvement(self, improvement_id: str) -> TestDatasetRecord:
        with self._session_factory.begin() as db:
            source = self._load_adoption_source(db, improvement_id)
            baseline_agent_version_id = self._baseline_version(db, source.improvement.improvement_id)
            dataset_id = _dataset_id_for_source(source, baseline_agent_version_id=baseline_agent_version_id)
            existing = db.get(TestDatasetModel, dataset_id)
            if existing is not None:
                if (
                    existing.agent_id != source.improvement.agent_id
                    or existing.owner_kind != "business_agent"
                    or existing.owner_id != source.improvement.agent_id
                    or existing.source_improvement_id != source.improvement.improvement_id
                    or existing.source_execution_id != source.execution.execution_id
                    or existing.candidate_agent_version_id != source.execution.applied_agent_version_id
                ):
                    raise DataIntegrityError(f"TestDataset source fingerprint resolved to inconsistent content: {dataset_id}")
                return self._record(db, existing)
            return self._insert_dataset(
                db,
                source,
                dataset_id=dataset_id,
                baseline_agent_version_id=baseline_agent_version_id,
            )

    def transition_lifecycle(
        self,
        dataset_id: str,
        *,
        agent_id: str,
        target_state: str,
        expected_revision: int,
        operator: str,
        reason: str,
    ) -> TestDatasetRecord:
        clean_agent = self._require_agent_id(agent_id)
        clean_operator = self._require_audit_text(operator, "operator")
        clean_reason = self._require_audit_text(reason, "reason")
        with self._session_factory.begin() as db:
            row = db.get(TestDatasetModel, dataset_id)
            if row is None or row.agent_id != clean_agent:
                raise NotFoundError(f"TestDataset not found for agent {clean_agent}: {dataset_id}")
            if target_state == "evaluating":
                raise BusinessRuleViolation("TestDataset evaluating is owned by EvalRun execution")
            if row.lifecycle_state == "evaluating":
                running_eval_run_id = db.scalar(
                    select(EvalRunModel.eval_run_id).where(
                        EvalRunModel.dataset_id == dataset_id,
                        EvalRunModel.status == "running",
                    )
                )
                if running_eval_run_id:
                    raise ConflictError(f"TestDataset is owned by running EvalRun {running_eval_run_id}: {dataset_id}")
            updated = transition_test_dataset_lifecycle_row(
                db,
                row,
                target_state=target_state,
                expected_revision=expected_revision,
                operator=clean_operator,
                reason=clean_reason,
            )
            return self._record(db, updated)

    def _load_adoption_source(self, db: Any, improvement_id: str) -> _AdoptionSource:
        improvement = db.get(ImprovementItemModel, improvement_id)
        if improvement is None:
            raise NotFoundError(f"ImprovementItem not found: {improvement_id}")
        if improvement.improvement_status == "archived":
            raise ConflictError(f"Archived improvement cannot adopt a TestDataset: {improvement_id}")
        assessment = self._required_artifact(db, RegressionAssessmentModel, improvement_id, "regression assessment")
        normalized = self._required_artifact(db, NormalizedFeedbackModel, improvement_id, "normalized feedback")
        attribution = self._required_artifact(db, AttributionModel, improvement_id, "attribution")
        plan = self._required_artifact(db, OptimizationPlanModel, improvement_id, "optimization plan")
        execution = self._required_artifact(db, ExecutionRecordModel, improvement_id, "execution")
        for label, artifact in (
            ("regression assessment", assessment),
            ("normalized feedback", normalized),
            ("attribution", attribution),
            ("optimization plan", plan),
            ("execution", execution),
        ):
            if artifact.status != "confirmed":
                raise BusinessRuleViolation(f"Confirmed {label} is required before TestDataset adoption: {improvement_id}")
        if not execution.change_set_id or not execution.applied_agent_version_id or not execution.applied_diff_json:
            raise BusinessRuleViolation(f"Applied execution evidence is required before TestDataset adoption: {improvement_id}")
        self._validate_execution_dependencies(
            execution=execution,
            plan=plan,
            attribution=attribution,
            improvement_id=improvement_id,
        )
        change_set = db.get(AgentChangeSetModel, execution.change_set_id)
        self._validate_execution_change_set(
            change_set=change_set,
            execution=execution,
            improvement=improvement,
        )
        cases = self._validated_cases(assessment, improvement_id)
        feedback_rows = db.scalars(
            select(ImprovementFeedbackModel)
            .where(ImprovementFeedbackModel.improvement_id == improvement_id)
            .order_by(ImprovementFeedbackModel.created_at, ImprovementFeedbackModel.feedback_id)
        ).all()
        feedback_ids = self._validated_source_feedback_ids(improvement, feedback_rows)
        return _AdoptionSource(improvement, assessment, normalized, attribution, plan, execution, feedback_ids, cases)

    def _insert_dataset(
        self,
        db: Any,
        source: _AdoptionSource,
        *,
        dataset_id: str,
        baseline_agent_version_id: str,
    ) -> TestDatasetRecord:
        now = utc_now()
        values = {
            "dataset_id": dataset_id,
            "agent_id": source.improvement.agent_id,
            "owner_kind": "business_agent",
            "owner_id": source.improvement.agent_id,
            "source_improvement_id": source.improvement.improvement_id,
            "name": f"测试数据集：{source.improvement.title}",
            "description": source.assessment.summary or "",
            "scope": "feedback-derived",
            "revision": 1,
            "lifecycle_state": "draft",
            "source_regression_assessment_id": source.assessment.regression_assessment_id,
            "source_regression_assessment_updated_at": source.assessment.updated_at,
            "source_normalized_feedback_id": source.normalized_feedback.normalized_feedback_id,
            "source_normalized_feedback_updated_at": source.normalized_feedback.updated_at,
            "source_attribution_id": source.attribution.attribution_id,
            "source_attribution_updated_at": source.attribution.updated_at,
            "source_optimization_plan_id": source.optimization_plan.optimization_plan_id,
            "source_optimization_plan_updated_at": source.optimization_plan.updated_at,
            "source_execution_id": source.execution.execution_id,
            "source_execution_updated_at": source.execution.updated_at,
            "baseline_agent_version_id": baseline_agent_version_id,
            "candidate_agent_version_id": source.execution.applied_agent_version_id,
            "source_feedback_ids_json": source.feedback_ids,
            "quality_tags_json": ["feedback-derived"],
            "created_at": now,
            "updated_at": now,
        }
        inserted = db.execute(sqlite_insert(TestDatasetModel).values(**values).on_conflict_do_nothing(index_elements=[TestDatasetModel.dataset_id]))
        if inserted.rowcount == 1:
            self._insert_initial_dataset_content(db, source, dataset_id=dataset_id, created_at=now)
        row = db.get(TestDatasetModel, dataset_id)
        if row is None:  # pragma: no cover - insert or conflict row must exist
            raise DataIntegrityError(f"TestDataset adoption did not persist: {dataset_id}")
        if (
            row.source_improvement_id != source.improvement.improvement_id
            or row.source_execution_id != source.execution.execution_id
            or row.candidate_agent_version_id != source.execution.applied_agent_version_id
        ):
            raise DataIntegrityError(f"TestDataset idempotency key collision: {dataset_id}")
        return self._record(db, row)

    @staticmethod
    def _insert_initial_dataset_content(db: Any, source: _AdoptionSource, *, dataset_id: str, created_at: str) -> None:
        for position, case in enumerate(source.cases, start=1):
            db.add(
                TestDatasetCaseModel(
                    case_id=f"tdc-{uuid4().hex[:16]}",
                    dataset_id=dataset_id,
                    position=position,
                    prompt=case.prompt,
                    expected_behavior=case.expected_behavior,
                    checkpoints_json=list(case.checkpoints),
                )
            )
        db.add(
            TestDatasetRevisionModel(
                revision_id=f"tdr-{uuid4().hex[:16]}",
                dataset_id=dataset_id,
                revision=1,
                previous_lifecycle_state=None,
                lifecycle_state="draft",
                operator="system",
                reason="adopted_from_confirmed_regression_assessment",
                before_json={},
                after_json={
                    "dataset_id": dataset_id,
                    "agent_id": source.improvement.agent_id,
                    "owner_kind": "business_agent",
                    "owner_id": source.improvement.agent_id,
                    "revision": 1,
                    "lifecycle_state": "draft",
                    "updated_at": created_at,
                },
                created_at=created_at,
            )
        )
        db.flush()

    @staticmethod
    def _required_artifact(db: Any, model: type[Any], improvement_id: str, label: str) -> Any:
        row = db.scalar(select(model).where(model.improvement_id == improvement_id))
        if row is None:
            raise BusinessRuleViolation(f"{label.title()} is required before TestDataset adoption: {improvement_id}")
        return row

    @staticmethod
    def _validate_execution_dependencies(
        *,
        execution: ExecutionRecordModel,
        plan: OptimizationPlanModel,
        attribution: AttributionModel,
        improvement_id: str,
    ) -> None:
        if (execution.source_optimization_plan_id, execution.source_optimization_plan_updated_at) != (
            plan.optimization_plan_id,
            plan.updated_at,
        ):
            raise ConflictError(f"Optimization plan revision changed after execution: {improvement_id}")
        if (execution.source_attribution_id, execution.source_attribution_updated_at) != (
            attribution.attribution_id,
            attribution.updated_at,
        ):
            raise ConflictError(f"Attribution revision changed after execution: {improvement_id}")

    @staticmethod
    def _validate_execution_change_set(
        *,
        change_set: AgentChangeSetModel | None,
        execution: ExecutionRecordModel,
        improvement: ImprovementItemModel,
    ) -> None:
        if change_set is None or change_set.change_set_id != execution.change_set_id:
            raise DataIntegrityError(f"Execution change set does not exist: {execution.change_set_id}")
        if change_set.status not in _ADOPTABLE_CHANGE_SET_STATES:
            raise DataIntegrityError(f"Execution change set is not adoptable from status {change_set.status}: {execution.change_set_id}")
        if execution.improvement_id != improvement.improvement_id:
            raise DataIntegrityError(f"Execution belongs to a different improvement: {execution.execution_id}")
        if change_set.agent_id != improvement.agent_id:
            raise DataIntegrityError(f"Execution change set belongs to a different Agent: {execution.change_set_id}")
        if change_set.execution_job_id != execution.execution_id:
            raise DataIntegrityError(f"Execution change set belongs to a different execution: {execution.change_set_id}")
        payload = dict(change_set.payload_json or {})
        if payload.get("source_improvement_id") != improvement.improvement_id:
            raise DataIntegrityError(f"Execution change set belongs to a different improvement: {execution.change_set_id}")
        if not change_set.candidate_commit_sha or change_set.candidate_commit_sha != execution.applied_agent_version_id:
            raise DataIntegrityError(f"Execution applied Agent version does not match change set candidate: {execution.change_set_id}")
        if not execution.base_commit_sha or change_set.base_commit_sha != execution.base_commit_sha:
            raise DataIntegrityError(f"Execution base commit does not match change set base: {execution.change_set_id}")
        worktree_path = (change_set.worktree_path or "").strip()
        if not worktree_path or not Path(worktree_path).is_absolute():
            raise DataIntegrityError(f"Execution change set worktree is invalid: {execution.change_set_id}")
        expected_payload = {
            "change_set_id": change_set.change_set_id,
            "agent_id": change_set.agent_id,
            "execution_job_id": execution.execution_id,
            "base_commit_sha": change_set.base_commit_sha,
            "candidate_commit_sha": change_set.candidate_commit_sha,
            "worktree_path": worktree_path,
            "source_improvement_id": improvement.improvement_id,
            "source_attribution_id": execution.source_attribution_id,
        }
        drifted_fields = [field for field, expected in expected_payload.items() if payload.get(field) != expected]
        if drifted_fields:
            raise DataIntegrityError(f"Execution change set payload disagrees with persisted binding ({', '.join(drifted_fields)}): {execution.change_set_id}")

    @staticmethod
    def _validated_source_feedback_ids(
        improvement: ImprovementItemModel,
        feedback_rows: list[ImprovementFeedbackModel],
    ) -> list[str]:
        if any(row.agent_id != improvement.agent_id for row in feedback_rows):
            raise DataIntegrityError(f"Improvement feedback owner disagrees with TestDataset owner: {improvement.improvement_id}")
        rows_by_ref = {row.feedback_id: row for row in feedback_rows}
        rows_by_ref.update({row.case_id: row for row in feedback_rows if row.case_id})
        raw_refs = list(improvement.source_feedback_refs_json or [])
        if any(not isinstance(ref, str) or not ref.strip() for ref in raw_refs):
            raise DataIntegrityError(f"Improvement source feedback refs are invalid: {improvement.improvement_id}")
        orphan_refs = [ref for ref in dict.fromkeys(raw_refs) if ref not in rows_by_ref]
        if orphan_refs:
            raise DataIntegrityError(f"Improvement source feedback refs lack same-Agent evidence ({', '.join(orphan_refs)}): {improvement.improvement_id}")
        return list(dict.fromkeys(row.feedback_id for row in feedback_rows))

    @staticmethod
    def _validated_cases(assessment: RegressionAssessmentModel, improvement_id: str) -> list[RegressionCase]:
        try:
            cases = [RegressionCase.model_validate(item) for item in list(assessment.cases_json or [])]
        except ValidationError as exc:
            raise DataIntegrityError(f"Regression assessment cases are invalid: {improvement_id}") from exc
        if not cases:
            raise BusinessRuleViolation(f"Regression assessment has no cases: {improvement_id}")
        return cases

    @staticmethod
    def _baseline_version(db: Any, improvement_id: str) -> str:
        versions = db.scalars(
            select(ImprovementFeedbackModel.agent_version_id)
            .where(
                ImprovementFeedbackModel.improvement_id == improvement_id,
                ImprovementFeedbackModel.agent_version_id != "",
            )
            .order_by(ImprovementFeedbackModel.created_at)
        ).all()
        return versions[0] if versions else ""

    @staticmethod
    def _require_agent_id(agent_id: str) -> str:
        clean_agent = (agent_id or "").strip()
        if not clean_agent:
            raise BusinessRuleViolation("agent_id is required for TestDataset access")
        return clean_agent

    @staticmethod
    def _require_audit_text(value: str, field: str) -> str:
        clean_value = (value or "").strip()
        if not clean_value:
            raise BusinessRuleViolation(f"{field} is required for TestDataset lifecycle transitions")
        return clean_value

    @staticmethod
    def _revision_record(row: TestDatasetRevisionModel) -> TestDatasetRevisionRecord:
        return TestDatasetRevisionRecord(
            revision_id=row.revision_id,
            dataset_id=row.dataset_id,
            revision=row.revision,
            previous_lifecycle_state=row.previous_lifecycle_state,
            lifecycle_state=row.lifecycle_state,
            operator=row.operator,
            reason=row.reason,
            before=dict(row.before_json or {}),
            after=dict(row.after_json or {}),
            created_at=row.created_at,
        )

    @staticmethod
    def _record(db: Any, row: TestDatasetModel) -> TestDatasetRecord:
        return project_test_dataset_record(db, row)
