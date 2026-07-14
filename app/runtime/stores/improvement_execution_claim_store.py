from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from ..errors import BusinessRuleViolation, ConflictError
from ..improvement_db import AttributionModel, ExecutionRecordModel, ImprovementItemModel, OptimizationPlanModel
from ..runtime_db import utc_now
from ..state_machines import IMPROVEMENT_STAGE_ORDER, validate_transition

MutableImprovementGuard = Callable[[Any, str], None]


@dataclass(frozen=True)
class ExecutionClaim:
    execution_id: str
    improvement_id: str
    change_set_id: str
    base_commit_sha: str
    source_optimization_plan_id: str
    source_optimization_plan_updated_at: str
    source_attribution_id: str
    source_attribution_updated_at: str
    claim_token: str
    claim_generation: int


class ImprovementExecutionClaimStore:
    """CAS/fencing boundary for automatic improvement execution intents."""

    def __init__(self, session_factory: sessionmaker, *, mutable_guard: MutableImprovementGuard) -> None:
        self._session_factory = session_factory
        self._mutable_guard = mutable_guard

    def claim_execution(
        self,
        improvement_id: str,
        *,
        change_set_id: str,
        base_commit_sha: str,
        source_optimization_plan_id: str,
        source_optimization_plan_updated_at: str,
        claim_token: str,
        now: str,
        claim_expires_at: str,
        source_attribution_id: str = "",
        source_attribution_updated_at: str = "",
    ) -> ExecutionClaim:
        if not all(
            (
                change_set_id,
                base_commit_sha,
                source_optimization_plan_id,
                source_optimization_plan_updated_at,
                claim_token,
                now,
                claim_expires_at,
            )
        ):
            raise BusinessRuleViolation("Execution claim requires change set, base commit, plan revision, token, and lease")
        if bool(source_attribution_id) != bool(source_attribution_updated_at):
            raise BusinessRuleViolation("Execution claim attribution revision must include both id and updated_at")
        for attempt in range(2):
            try:
                with self._session_factory.begin() as db:
                    self._mutable_guard(db, improvement_id)
                    self._validate_source_revisions(
                        db,
                        improvement_id,
                        source_optimization_plan_id=source_optimization_plan_id,
                        source_optimization_plan_updated_at=source_optimization_plan_updated_at,
                        source_attribution_id=source_attribution_id,
                        source_attribution_updated_at=source_attribution_updated_at,
                    )
                    row = db.query(ExecutionRecordModel).filter(ExecutionRecordModel.improvement_id == improvement_id).one_or_none()
                    if row is None:
                        row = self._new_claim(
                            improvement_id,
                            change_set_id=change_set_id,
                            base_commit_sha=base_commit_sha,
                            source_optimization_plan_id=source_optimization_plan_id,
                            source_optimization_plan_updated_at=source_optimization_plan_updated_at,
                            source_attribution_id=source_attribution_id,
                            source_attribution_updated_at=source_attribution_updated_at,
                            claim_token=claim_token,
                            now=now,
                            claim_expires_at=claim_expires_at,
                        )
                        db.add(row)
                        db.flush()
                        return _claim_from_row(row)
                    return self._take_claim(
                        db,
                        row,
                        change_set_id=change_set_id,
                        base_commit_sha=base_commit_sha,
                        source_optimization_plan_id=source_optimization_plan_id,
                        source_optimization_plan_updated_at=source_optimization_plan_updated_at,
                        source_attribution_id=source_attribution_id,
                        source_attribution_updated_at=source_attribution_updated_at,
                        claim_token=claim_token,
                        now=now,
                        claim_expires_at=claim_expires_at,
                    )
            except IntegrityError:
                if attempt:
                    raise
        raise ConflictError(f"Execution claim raced for improvement: {improvement_id}")

    def renew_execution_claim(
        self,
        improvement_id: str,
        *,
        claim_token: str,
        claim_generation: int,
        now: str,
        claim_expires_at: str,
    ) -> None:
        with self._session_factory.begin() as db:
            self._mutable_guard(db, improvement_id)
            self._assert_owned_source_revisions(
                db,
                improvement_id,
                claim_token=claim_token,
                claim_generation=claim_generation,
            )
            changed = db.execute(
                update(ExecutionRecordModel)
                .where(
                    ExecutionRecordModel.improvement_id == improvement_id,
                    ExecutionRecordModel.status == "applying",
                    ExecutionRecordModel.claim_token == claim_token,
                    ExecutionRecordModel.claim_generation == claim_generation,
                )
                .values(claim_expires_at=claim_expires_at, updated_at=now)
            ).rowcount
            if changed != 1:
                raise ConflictError(f"Execution claim is no longer owned: {improvement_id}")

    def finalize_execution_claim(
        self,
        improvement_id: str,
        *,
        claim_token: str,
        claim_generation: int,
        summary: str,
        changes_applied: list[str],
        agent_version: str,
        risk_level: str,
        rollback_strategy: str,
        rollback_instructions: list[str],
        applied_diff: dict,
        generation_trace_id: str = "",
        generation_trace_url: str = "",
    ) -> None:
        if not changes_applied or not agent_version or not applied_diff:
            raise BusinessRuleViolation("Applied execution requires changes, candidate version, and diff evidence")
        self._complete_claim(
            improvement_id,
            claim_token=claim_token,
            claim_generation=claim_generation,
            advance_stage="execution",
            validate_source_revisions=True,
            values={
                "summary": summary,
                "changes_applied_json": list(changes_applied),
                "agent_version": agent_version,
                "applied_agent_version_id": agent_version,
                "applied_diff_json": dict(applied_diff),
                "risk_level": risk_level,
                "rollback_strategy": rollback_strategy,
                "rollback_instructions_json": list(rollback_instructions),
                "generated_by": "governor",
                "generation_trace_id": generation_trace_id,
                "generation_trace_url": generation_trace_url,
            },
        )

    def finish_without_application(
        self,
        improvement_id: str,
        *,
        claim_token: str,
        claim_generation: int,
        summary: str,
        generated_by: str = "heuristic",
        retain_change_set: bool = True,
    ) -> None:
        values: dict[str, object] = {
            "summary": summary,
            "changes_applied_json": [],
            "agent_version": "",
            "applied_agent_version_id": "",
            "applied_diff_json": {},
            "risk_level": "",
            "rollback_strategy": "未应用变更，无需回滚",
            "rollback_instructions_json": [],
            "generated_by": generated_by,
            "generation_trace_id": "",
            "generation_trace_url": "",
        }
        if not retain_change_set:
            values.update(change_set_id="", base_commit_sha="")
        self._complete_claim(
            improvement_id,
            claim_token=claim_token,
            claim_generation=claim_generation,
            advance_stage=None,
            validate_source_revisions=False,
            values=values,
        )

    def expire_claim(
        self,
        improvement_id: str,
        *,
        claim_token: str,
        claim_generation: int,
        now: str,
    ) -> None:
        with self._session_factory.begin() as db:
            self._mutable_guard(db, improvement_id)
            db.execute(
                update(ExecutionRecordModel)
                .where(
                    ExecutionRecordModel.improvement_id == improvement_id,
                    ExecutionRecordModel.status == "applying",
                    ExecutionRecordModel.claim_token == claim_token,
                    ExecutionRecordModel.claim_generation == claim_generation,
                )
                .values(claim_expires_at=now, updated_at=now)
            )

    def list_expired_claims(self, *, now: str, limit: int = 100) -> list[ExecutionClaim]:
        with self._session_factory() as db:
            rows = (
                db.query(ExecutionRecordModel)
                .filter(
                    ExecutionRecordModel.status == "applying",
                    ExecutionRecordModel.claim_token != "",
                    ExecutionRecordModel.claim_expires_at != "",
                    ExecutionRecordModel.claim_expires_at <= now,
                )
                .order_by(ExecutionRecordModel.claim_expires_at, ExecutionRecordModel.execution_id)
                .limit(max(1, limit))
                .all()
            )
            return [_claim_from_row(row) for row in rows]

    @staticmethod
    def _new_claim(
        improvement_id: str,
        *,
        change_set_id: str,
        base_commit_sha: str,
        source_optimization_plan_id: str,
        source_optimization_plan_updated_at: str,
        source_attribution_id: str,
        source_attribution_updated_at: str,
        claim_token: str,
        now: str,
        claim_expires_at: str,
    ) -> ExecutionRecordModel:
        return ExecutionRecordModel(
            execution_id=f"exec-{uuid4().hex[:12]}",
            improvement_id=improvement_id,
            summary="执行申请已登记，等待候选版本证据。",
            status="applying",
            generated_by="governor",
            change_set_id=change_set_id,
            base_commit_sha=base_commit_sha,
            source_optimization_plan_id=source_optimization_plan_id,
            source_optimization_plan_updated_at=source_optimization_plan_updated_at,
            source_attribution_id=source_attribution_id,
            source_attribution_updated_at=source_attribution_updated_at,
            claim_token=claim_token,
            claim_generation=1,
            claim_expires_at=claim_expires_at,
            created_at=now,
            updated_at=now,
        )

    def _take_claim(
        self,
        db: Any,
        row: ExecutionRecordModel,
        *,
        change_set_id: str,
        base_commit_sha: str,
        source_optimization_plan_id: str,
        source_optimization_plan_updated_at: str,
        source_attribution_id: str,
        source_attribution_updated_at: str,
        claim_token: str,
        now: str,
        claim_expires_at: str,
    ) -> ExecutionClaim:
        if _has_application_evidence(row):
            raise ConflictError(f"Execution already has applied evidence: {row.improvement_id}")
        if row.status == "applying" and row.claim_expires_at and row.claim_expires_at > now:
            raise ConflictError(f"Execution is already applying: {row.improvement_id}")
        requested_source = (
            source_optimization_plan_id,
            source_optimization_plan_updated_at,
            source_attribution_id,
            source_attribution_updated_at,
        )
        persisted_source = _source_revision_from_row(row)
        if row.change_set_id == change_set_id and persisted_source[0] and persisted_source != requested_source:
            raise ConflictError(f"Execution change set is bound to a different source revision: {row.improvement_id}")
        validate_transition("improvement_execution", row.status, "applying")
        next_generation = int(row.claim_generation or 0) + 1
        changed = db.execute(
            update(ExecutionRecordModel)
            .where(
                ExecutionRecordModel.execution_id == row.execution_id,
                ExecutionRecordModel.status == row.status,
                ExecutionRecordModel.updated_at == row.updated_at,
                ExecutionRecordModel.claim_token == (row.claim_token or ""),
                ExecutionRecordModel.claim_generation == int(row.claim_generation or 0),
                ExecutionRecordModel.change_set_id == (row.change_set_id or ""),
            )
            .values(
                summary="执行申请已登记，等待候选版本证据。",
                changes_applied_json=[],
                agent_version="",
                status="applying",
                generated_by="governor",
                change_set_id=change_set_id,
                applied_agent_version_id="",
                applied_diff_json={},
                base_commit_sha=base_commit_sha,
                source_optimization_plan_id=source_optimization_plan_id,
                source_optimization_plan_updated_at=source_optimization_plan_updated_at,
                source_attribution_id=source_attribution_id,
                source_attribution_updated_at=source_attribution_updated_at,
                claim_token=claim_token,
                claim_generation=next_generation,
                claim_expires_at=claim_expires_at,
                updated_at=now,
            )
        ).rowcount
        if changed != 1:
            raise ConflictError(f"Execution claim changed concurrently: {row.improvement_id}")
        return ExecutionClaim(
            execution_id=row.execution_id,
            improvement_id=row.improvement_id,
            change_set_id=change_set_id,
            base_commit_sha=base_commit_sha,
            source_optimization_plan_id=source_optimization_plan_id,
            source_optimization_plan_updated_at=source_optimization_plan_updated_at,
            source_attribution_id=source_attribution_id,
            source_attribution_updated_at=source_attribution_updated_at,
            claim_token=claim_token,
            claim_generation=next_generation,
        )

    def _complete_claim(
        self,
        improvement_id: str,
        *,
        claim_token: str,
        claim_generation: int,
        advance_stage: str | None,
        validate_source_revisions: bool,
        values: dict[str, object],
    ) -> None:
        now = utc_now()
        validate_transition("improvement_execution", "applying", "draft")
        with self._session_factory.begin() as db:
            self._mutable_guard(db, improvement_id)
            if validate_source_revisions:
                self._assert_owned_source_revisions(
                    db,
                    improvement_id,
                    claim_token=claim_token,
                    claim_generation=claim_generation,
                )
            changed = db.execute(
                update(ExecutionRecordModel)
                .where(
                    ExecutionRecordModel.improvement_id == improvement_id,
                    ExecutionRecordModel.status == "applying",
                    ExecutionRecordModel.claim_token == claim_token,
                    ExecutionRecordModel.claim_generation == claim_generation,
                )
                .values(**values, status="draft", claim_token="", claim_expires_at="", updated_at=now)
            ).rowcount
            if changed != 1:
                raise ConflictError(f"Execution claim is no longer owned: {improvement_id}")
            if advance_stage:
                self._advance_stage(db, improvement_id, target=advance_stage)

    def _assert_owned_source_revisions(
        self,
        db: Any,
        improvement_id: str,
        *,
        claim_token: str,
        claim_generation: int,
    ) -> None:
        row = (
            db.query(ExecutionRecordModel)
            .filter(
                ExecutionRecordModel.improvement_id == improvement_id,
                ExecutionRecordModel.status == "applying",
                ExecutionRecordModel.claim_token == claim_token,
                ExecutionRecordModel.claim_generation == claim_generation,
            )
            .one_or_none()
        )
        if row is None:
            raise ConflictError(f"Execution claim is no longer owned: {improvement_id}")
        self._validate_source_revisions(
            db,
            improvement_id,
            source_optimization_plan_id=row.source_optimization_plan_id,
            source_optimization_plan_updated_at=row.source_optimization_plan_updated_at,
            source_attribution_id=row.source_attribution_id,
            source_attribution_updated_at=row.source_attribution_updated_at,
        )

    @staticmethod
    def _validate_source_revisions(
        db: Any,
        improvement_id: str,
        *,
        source_optimization_plan_id: str,
        source_optimization_plan_updated_at: str,
        source_attribution_id: str,
        source_attribution_updated_at: str,
    ) -> None:
        plan = db.query(OptimizationPlanModel).filter(OptimizationPlanModel.improvement_id == improvement_id).one_or_none()
        if plan is None or plan.status != "confirmed":
            raise ConflictError(f"Execution requires a confirmed optimization plan: {improvement_id}")
        if (plan.optimization_plan_id, plan.updated_at) != (
            source_optimization_plan_id,
            source_optimization_plan_updated_at,
        ):
            raise ConflictError(f"Optimization plan revision changed before execution claim: {improvement_id}")
        attribution = db.query(AttributionModel).filter(AttributionModel.improvement_id == improvement_id).one_or_none()
        if attribution is not None and attribution.status != "confirmed":
            raise ConflictError(f"Execution requires confirmed attribution when attribution exists: {improvement_id}")
        current_attribution = (attribution.attribution_id, attribution.updated_at) if attribution is not None else ("", "")
        if current_attribution != (source_attribution_id, source_attribution_updated_at):
            raise ConflictError(f"Attribution revision changed before execution claim: {improvement_id}")

    @staticmethod
    def _advance_stage(db: Any, improvement_id: str, *, target: str) -> None:
        item = db.get(ImprovementItemModel, improvement_id)
        if item is None:
            raise ConflictError(f"Improvement item disappeared: {improvement_id}")
        current = item.improvement_stage or "feedback_intake"
        try:
            current_index = IMPROVEMENT_STAGE_ORDER.index(current)
            target_index = IMPROVEMENT_STAGE_ORDER.index(target)
        except ValueError as exc:
            raise ConflictError(f"Unknown improvement stage during execution finalize: {current} -> {target}") from exc
        if target_index <= current_index:
            return
        for index in range(current_index, target_index):
            validate_transition(
                "improvement_stage",
                IMPROVEMENT_STAGE_ORDER[index],
                IMPROVEMENT_STAGE_ORDER[index + 1],
            )
        item.improvement_stage = target
        item.improvement_status = "active"
        item.updated_at = utc_now()


def _claim_from_row(row: ExecutionRecordModel) -> ExecutionClaim:
    return ExecutionClaim(
        execution_id=row.execution_id,
        improvement_id=row.improvement_id,
        change_set_id=row.change_set_id,
        base_commit_sha=row.base_commit_sha,
        source_optimization_plan_id=row.source_optimization_plan_id,
        source_optimization_plan_updated_at=row.source_optimization_plan_updated_at,
        source_attribution_id=row.source_attribution_id,
        source_attribution_updated_at=row.source_attribution_updated_at,
        claim_token=row.claim_token,
        claim_generation=int(row.claim_generation or 0),
    )


def _has_application_evidence(row: ExecutionRecordModel) -> bool:
    bound_candidate = bool(row.change_set_id and row.applied_agent_version_id and row.applied_diff_json)
    manual_evidence = bool(row.changes_applied_json and str(row.agent_version or "").strip())
    return bound_candidate or manual_evidence


def _source_revision_from_row(row: ExecutionRecordModel) -> tuple[str, str, str, str]:
    return (
        row.source_optimization_plan_id or "",
        row.source_optimization_plan_updated_at or "",
        row.source_attribution_id or "",
        row.source_attribution_updated_at or "",
    )
