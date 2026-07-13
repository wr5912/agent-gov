from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from app.runtime.errors import ConflictError
from app.runtime.improvement_db import AttributionModel, ExecutionRecordModel, ImprovementItemModel, OptimizationPlanModel
from app.runtime.json_types import JsonObject
from app.runtime.runtime_db import AgentChangeSetModel
from app.runtime.state_machines import IMPROVEMENT_STAGE_ORDER, validate_transition


@dataclass(frozen=True)
class PublicationSourceRevision:
    improvement_id: str
    updated_at: str


def validate_publication_provenance(
    db: Session,
    change_set_id: str,
    *,
    expected_improvement_id: str | None = None,
    expected_updated_at: str | None = None,
    require_revision_match: bool = False,
) -> PublicationSourceRevision | None:
    change_set = db.get(AgentChangeSetModel, change_set_id)
    if change_set is None:
        raise ConflictError("Agent change set disappeared during publication provenance validation")
    payload = dict(change_set.payload_json or {})
    improvement_id = str(payload.get("source_improvement_id") or "")
    if not improvement_id:
        if require_revision_match and (expected_improvement_id or expected_updated_at):
            raise ConflictError("Publication intent source improvement no longer matches its change set")
        return None
    improvement = db.get(ImprovementItemModel, improvement_id)
    if improvement is None or improvement.improvement_status != "active" or improvement.improvement_stage == "release":
        raise ConflictError("来源改进事项缺失、已归档或已发布")
    revision = PublicationSourceRevision(improvement_id, improvement.updated_at)
    if require_revision_match and (revision.improvement_id, revision.updated_at) != (
        expected_improvement_id,
        expected_updated_at,
    ):
        raise ConflictError("来源改进事项在发布预留后发生变化")
    execution_id = str(change_set.execution_job_id or "")
    execution = db.get(ExecutionRecordModel, execution_id) if execution_id else None
    if execution is None or execution.improvement_id != improvement_id or execution.change_set_id != change_set_id or execution.status != "confirmed":
        raise ConflictError("改进执行尚未确认或执行来源不完整，请先确认执行结果")
    plan_id = str(execution.source_optimization_plan_id or "")
    plan_updated_at = str(execution.source_optimization_plan_updated_at or "")
    plan = db.get(OptimizationPlanModel, plan_id) if plan_id else None
    if plan is None or plan.improvement_id != improvement_id or plan.status != "confirmed" or plan.updated_at != plan_updated_at:
        raise ConflictError("优化方案未确认、已返工或来源版本不匹配，请重新执行优化")
    attribution_id = str(execution.source_attribution_id or "")
    attribution_updated_at = str(execution.source_attribution_updated_at or "")
    attribution = db.get(AttributionModel, attribution_id) if attribution_id else None
    if (
        attribution is None
        or attribution.improvement_id != improvement_id
        or attribution.status != "confirmed"
        or attribution.updated_at != attribution_updated_at
        or payload.get("source_attribution_id") != attribution_id
    ):
        raise ConflictError("归因未确认、已返工或来源版本不匹配，请重新执行优化")
    return revision


def finalize_source_improvement(
    db: Session,
    *,
    improvement_id: str | None,
    expected_updated_at: str | None,
    completed_at: str,
) -> None:
    if not improvement_id:
        return
    improvement = db.get(ImprovementItemModel, improvement_id)
    if improvement is None or improvement.updated_at != expected_updated_at:
        raise ConflictError("Source improvement changed before publication finalization")
    current = improvement.improvement_stage or "feedback_intake"
    try:
        current_index = IMPROVEMENT_STAGE_ORDER.index(current)
        release_index = IMPROVEMENT_STAGE_ORDER.index("release")
    except ValueError as exc:
        raise ConflictError(f"Unknown source improvement stage: {current}") from exc
    for index in range(current_index, release_index):
        validate_transition("improvement_stage", IMPROVEMENT_STAGE_ORDER[index], IMPROVEMENT_STAGE_ORDER[index + 1])
    changed = db.execute(
        update(ImprovementItemModel)
        .where(
            ImprovementItemModel.improvement_id == improvement_id,
            ImprovementItemModel.updated_at == expected_updated_at,
            ImprovementItemModel.improvement_status == "active",
        )
        .values(improvement_stage="release", improvement_status="done", updated_at=completed_at)
    ).rowcount
    if changed != 1:
        raise ConflictError("Source improvement changed during publication finalization")


def project_current_attribution(session_factory: sessionmaker, change_set_data: Mapping[str, object]) -> JsonObject:
    projected: JsonObject = dict(change_set_data)
    improvement_id = str(projected.get("source_improvement_id") or "")
    attribution_id = str(projected.get("source_attribution_id") or "")
    if not improvement_id:
        projected["source_attribution_status"] = None
        projected["publication_provenance_blocker"] = None
        return projected
    if not attribution_id:
        projected["source_attribution_status"] = None
    with session_factory() as db:
        attribution = db.get(AttributionModel, attribution_id) if attribution_id else None
        change_set = db.get(AgentChangeSetModel, str(projected.get("change_set_id") or ""))
        try:
            if change_set is None or change_set.status != "published":
                validate_publication_provenance(db, str(projected.get("change_set_id") or ""))
            blocker = None
        except ConflictError as exc:
            blocker = str(exc)
    projected["source_attribution_status"] = attribution.status if attribution is not None and attribution.improvement_id == improvement_id else None
    projected["publication_provenance_blocker"] = blocker
    return projected
