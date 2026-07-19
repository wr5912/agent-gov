from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import sessionmaker

from ..errors import BusinessRuleViolation, ConflictError, NotFoundError
from ..improvement_db import (
    AttributionModel,
    ExecutionRecordModel,
    ImprovementFeedbackCaseAssignmentModel,
    ImprovementFeedbackModel,
    ImprovementItemModel,
    ImprovementLinkModel,
    NormalizedFeedbackModel,
    OptimizationPlanModel,
    RegressionTestDesignModel,
)
from ..runtime_db import AgentChangeSetModel, utc_now
from ..state_machines import IMPROVEMENT_STAGE_ORDER, StateTransitionError, validate_transition

# 改进事项可引用的当前闭环对象类型（W2-c 轻引用）。
LINK_KINDS = {"attribution", "optimization_plan", "test_run", "change_set"}


@dataclass(frozen=True)
class ImprovementLinkRecord:
    link_id: str
    improvement_id: str
    kind: str
    ref_id: str
    created_at: str


@dataclass(frozen=True)
class ImprovementItemRecord:
    """改进事项事项级领域记录（四阶段改进治理 跨代重建的单一事实来源）。"""

    improvement_id: str
    agent_id: str
    title: str
    summary: str
    source_feedback_refs: list[str]
    improvement_stage: str
    improvement_status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ImprovementDeletionImpact:
    """删除改进事项前的影响面（dry-run）：随删计数 + 退回未归属池的来源反馈引用数。"""

    improvement_id: str
    title: str
    source_feedback_refs: int
    feedbacks: int
    links: int
    has_attribution: bool
    has_optimization_plan: bool


def derive_improvement_status(stage: str) -> str:
    """improvement_status 由 stage 派生：release 为完成态，其余为进行中。

    archived 等更多派生态留待 Wave 2（与闭环引擎对接后），本期保持单一来源、最小派生。
    """
    return "done" if stage == "release" else "active"


def advance_improvement_stage_in_transaction(db: Any, improvement_id: str, *, stage: str) -> ImprovementItemModel:
    """Advance an already-locked artifact owner within the artifact's transaction."""
    locked = db.execute(
        update(ImprovementItemModel)
        .where(
            ImprovementItemModel.improvement_id == improvement_id,
            ImprovementItemModel.improvement_status != "archived",
        )
        .values(updated_at=ImprovementItemModel.updated_at)
    ).rowcount
    row = db.get(ImprovementItemModel, improvement_id)
    if row is None:
        raise NotFoundError(f"ImprovementItem not found: {improvement_id}")
    if locked != 1:
        raise ConflictError(f"Archived improvement cannot be mutated: {improvement_id}")
    current = row.improvement_stage or "feedback_intake"
    try:
        current_index = IMPROVEMENT_STAGE_ORDER.index(current)
        target_index = IMPROVEMENT_STAGE_ORDER.index(stage)
    except ValueError as exc:
        raise StateTransitionError(f"Unknown improvement stage: {stage}") from exc
    if target_index < current_index:
        raise ConflictError(f"Refine improvement {improvement_id} to {stage} before replacing an earlier-stage artifact")
    if target_index == current_index:
        return row
    for index in range(current_index, target_index):
        validate_transition(
            "improvement_stage",
            IMPROVEMENT_STAGE_ORDER[index],
            IMPROVEMENT_STAGE_ORDER[index + 1],
        )
    row.improvement_stage = stage
    row.improvement_status = derive_improvement_status(stage)
    row.updated_at = utc_now()
    return row


class ImprovementStore:
    """改进事项存储：按 agent_id 归属与查询；阶段转移交集中状态机判定合法性。"""

    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _lock_mutable_improvement(db: Any, improvement_id: str) -> ImprovementItemModel:
        locked = db.execute(
            update(ImprovementItemModel)
            .where(
                ImprovementItemModel.improvement_id == improvement_id,
                ImprovementItemModel.improvement_status != "archived",
            )
            .values(updated_at=ImprovementItemModel.updated_at)
        ).rowcount
        row = db.get(ImprovementItemModel, improvement_id)
        if row is None:
            raise NotFoundError(f"ImprovementItem not found: {improvement_id}")
        if locked != 1:
            raise ConflictError(f"Archived improvement cannot be mutated: {improvement_id}")
        return row

    @staticmethod
    def _require_feedback_intake(row: ImprovementItemModel) -> None:
        if row.improvement_stage != "feedback_intake":
            raise ConflictError(f"Refine improvement {row.improvement_id} to feedback_intake before changing source feedback")

    def list_improvements(self, *, agent_id: str | None = None) -> list[ImprovementItemRecord]:
        with self._session_factory.begin() as db:
            query = db.query(ImprovementItemModel)
            if agent_id:
                query = query.filter(ImprovementItemModel.agent_id == agent_id)
            rows = query.order_by(ImprovementItemModel.created_at.desc(), ImprovementItemModel.improvement_id).all()
            return [_record(row) for row in rows]

    def get_improvement(self, improvement_id: str) -> ImprovementItemRecord | None:
        with self._session_factory.begin() as db:
            row = db.get(ImprovementItemModel, improvement_id)
            return _record(row) if row is not None else None

    def assigned_feedback_case_ids(self) -> set[str]:
        """FeedbackCase assignment relation is authoritative; item refs are its UI projection."""
        with self._session_factory.begin() as db:
            return {str(case_id) for (case_id,) in db.query(ImprovementFeedbackCaseAssignmentModel.feedback_case_id).all()}

    def improvement_id_for_feedback_case(self, feedback_case_id: str) -> str | None:
        with self._session_factory.begin() as db:
            row = db.get(ImprovementFeedbackCaseAssignmentModel, feedback_case_id)
            return row.improvement_id if row is not None else None

    def create_improvement(
        self,
        *,
        agent_id: str,
        title: str,
        summary: str = "",
        source_feedback_refs: list[str] | None = None,
    ) -> ImprovementItemRecord:
        clean_agent = (agent_id or "").strip()
        clean_title = (title or "").strip()
        if not clean_agent:
            raise BusinessRuleViolation("ImprovementItem must belong to a business agent (agent_id required)")
        if not clean_title:
            raise BusinessRuleViolation("ImprovementItem title cannot be empty")
        refs = [str(ref).strip() for ref in (source_feedback_refs or []) if str(ref).strip()]
        if any(ref.startswith("fbc-") for ref in refs):
            raise BusinessRuleViolation("FeedbackCase refs must be assigned through attach-feedback-case")
        improvement_id = f"imp-{uuid4().hex[:12]}"
        now = utc_now()
        with self._session_factory.begin() as db:
            db.add(
                ImprovementItemModel(
                    improvement_id=improvement_id,
                    agent_id=clean_agent,
                    title=clean_title,
                    summary=summary or "",
                    improvement_stage="feedback_intake",
                    improvement_status="active",
                    source_feedback_refs_json=refs,
                    created_at=now,
                    updated_at=now,
                )
            )
        return ImprovementItemRecord(
            improvement_id=improvement_id,
            agent_id=clean_agent,
            title=clean_title,
            summary=summary or "",
            source_feedback_refs=refs,
            improvement_stage="feedback_intake",
            improvement_status="active",
            created_at=now,
            updated_at=now,
        )

    def refine_stage(self, improvement_id: str, *, stage: str) -> ImprovementItemRecord:
        """执行用户请求的返工转移；公开 lifecycle 不得用于前推。"""
        with self._session_factory.begin() as db:
            row = self._lock_mutable_improvement(db, improvement_id)
            current = row.improvement_stage or "feedback_intake"
            try:
                current_index = IMPROVEMENT_STAGE_ORDER.index(current)
                target_index = IMPROVEMENT_STAGE_ORDER.index(stage)
            except ValueError as exc:
                raise StateTransitionError(f"Unknown improvement stage: {stage}") from exc
            if target_index >= current_index:
                raise StateTransitionError(f"Forward improvement transition {current} -> {stage} requires a successful business artifact command")
            validate_transition("improvement_stage", current, stage)
            self._invalidate_artifacts_after_stage(db, improvement_id, target_index=target_index)
            row.improvement_stage = stage
            row.improvement_status = derive_improvement_status(stage)
            row.updated_at = utc_now()
            return _record(row)

    @staticmethod
    def _invalidate_artifacts_after_stage(db: Any, improvement_id: str, *, target_index: int) -> None:
        execution_index = IMPROVEMENT_STAGE_ORDER.index("execution")
        if target_index < execution_index:
            ImprovementStore._assert_execution_settled(db, improvement_id, action="refine")

        artifact_models = (
            ("triage", NormalizedFeedbackModel),
            ("attribution", AttributionModel),
            ("optimization", OptimizationPlanModel),
            ("execution", ExecutionRecordModel),
            ("regression", RegressionTestDesignModel),
        )
        for artifact_stage, model in artifact_models:
            if IMPROVEMENT_STAGE_ORDER.index(artifact_stage) > target_index:
                db.query(model).filter(model.improvement_id == improvement_id).delete(synchronize_session=False)

        link_stage = {
            "attribution": "attribution",
            "optimization_plan": "optimization",
            "change_set": "execution",
            "test_run": "regression",
        }
        invalid_link_kinds = [kind for kind, artifact_stage in link_stage.items() if IMPROVEMENT_STAGE_ORDER.index(artifact_stage) > target_index]
        if invalid_link_kinds:
            db.query(ImprovementLinkModel).filter(
                ImprovementLinkModel.improvement_id == improvement_id,
                ImprovementLinkModel.kind.in_(invalid_link_kinds),
            ).delete(synchronize_session=False)

    @staticmethod
    def _assert_execution_settled(db: Any, improvement_id: str, *, action: str) -> None:
        execution = db.query(ExecutionRecordModel).filter(ExecutionRecordModel.improvement_id == improvement_id).one_or_none()
        if execution is None:
            return
        if execution.status == "applying":
            raise ConflictError(f"Cannot {action} while execution is applying: {improvement_id}")
        if not execution.change_set_id:
            return
        change_set = db.get(AgentChangeSetModel, execution.change_set_id)
        if change_set is None:
            raise ConflictError(f"Cannot {action}; execution change set is missing: {execution.change_set_id}")
        if change_set.status not in {"published", "abandoned"}:
            raise ConflictError(f"Abandon change set {execution.change_set_id} before {action} of {improvement_id}")

    def add_source_refs(self, improvement_id: str, refs: list[str]) -> ImprovementItemRecord:
        """把来源反馈引用并入已有改进事项（去重）。"""
        clean = [str(r).strip() for r in (refs or []) if str(r).strip()]
        if any(ref.startswith("fbc-") for ref in clean):
            raise BusinessRuleViolation("FeedbackCase refs must be assigned through attach-feedback-case")
        with self._session_factory.begin() as db:
            row = self._lock_mutable_improvement(db, improvement_id)
            self._require_feedback_intake(row)
            existing = list(row.source_feedback_refs_json or [])
            for ref in clean:
                if ref not in existing:
                    existing.append(ref)
            row.source_feedback_refs_json = existing
            row.updated_at = utc_now()
            return _record(row)

    def merge_improvements(self, target_id: str, *, source_id: str) -> ImprovementItemRecord:
        """把 source 改进事项归并进 target：来源反馈并入 target，source 置 archived（被归并）。"""
        if target_id == source_id:
            raise BusinessRuleViolation("Cannot merge an improvement into itself")
        with self._session_factory.begin() as db:
            locked = {improvement_id: self._lock_mutable_improvement(db, improvement_id) for improvement_id in sorted((target_id, source_id))}
            target = locked[target_id]
            source = locked[source_id]
            self._require_feedback_intake(target)
            self._require_feedback_intake(source)
            if target.agent_id != source.agent_id:
                raise BusinessRuleViolation("Cannot merge improvements across different business agents")
            existing = list(target.source_feedback_refs_json or [])
            for ref in source.source_feedback_refs_json or []:
                if ref not in existing:
                    existing.append(ref)
            target.source_feedback_refs_json = existing
            target.updated_at = utc_now()
            db.query(ImprovementFeedbackCaseAssignmentModel).filter(ImprovementFeedbackCaseAssignmentModel.improvement_id == source_id).update(
                {ImprovementFeedbackCaseAssignmentModel.improvement_id: target_id},
                synchronize_session=False,
            )
            db.query(ImprovementFeedbackModel).filter(ImprovementFeedbackModel.improvement_id == source_id).update(
                {ImprovementFeedbackModel.improvement_id: target_id},
                synchronize_session=False,
            )
            source.improvement_status = "archived"
            source.updated_at = utc_now()
            return _record(target)

    def split_improvement(self, improvement_id: str, *, feedback_ref: str) -> ImprovementItemRecord:
        """把某条来源反馈从改进事项拆出为一个新的改进事项（同 Agent，回到 feedback_intake）。"""
        clean = (feedback_ref or "").strip()
        if not clean:
            raise BusinessRuleViolation("feedback_ref is required to split")
        new_id = f"imp-{uuid4().hex[:12]}"
        now = utc_now()
        with self._session_factory.begin() as db:
            row = self._lock_mutable_improvement(db, improvement_id)
            self._require_feedback_intake(row)
            refs = list(row.source_feedback_refs_json or [])
            if clean not in refs:
                raise BusinessRuleViolation(f"feedback_ref not in improvement: {clean}")
            row.source_feedback_refs_json = [ref for ref in refs if ref != clean]
            row.updated_at = now
            assignment = db.get(ImprovementFeedbackCaseAssignmentModel, clean)
            if assignment is not None and assignment.improvement_id == improvement_id:
                assignment.improvement_id = new_id
                db.query(ImprovementFeedbackModel).filter(
                    ImprovementFeedbackModel.feedback_id == assignment.feedback_id,
                    ImprovementFeedbackModel.improvement_id == improvement_id,
                ).update(
                    {ImprovementFeedbackModel.improvement_id: new_id},
                    synchronize_session=False,
                )
            db.add(
                ImprovementItemModel(
                    improvement_id=new_id,
                    agent_id=row.agent_id,
                    title=f"{row.title}（拆分）",
                    summary="",
                    improvement_stage="feedback_intake",
                    improvement_status="active",
                    source_feedback_refs_json=[clean],
                    created_at=now,
                    updated_at=now,
                )
            )
        return self.get_improvement(new_id)  # type: ignore[return-value]

    def add_link(self, improvement_id: str, *, kind: str, ref_id: str) -> ImprovementLinkRecord:
        """幂等建立轻引用；唯一身份是 improvement_id + kind + ref_id。"""
        clean_kind = (kind or "").strip()
        clean_ref = (ref_id or "").strip()
        if clean_kind not in LINK_KINDS:
            raise BusinessRuleViolation(f"Unknown link kind: {clean_kind}; expected one of {sorted(LINK_KINDS)}")
        if not clean_ref:
            raise BusinessRuleViolation("link ref_id cannot be empty")
        link_id = f"lnk-{uuid4().hex[:12]}"
        now = utc_now()
        with self._session_factory.begin() as db:
            self._lock_mutable_improvement(db, improvement_id)
            db.execute(
                insert(ImprovementLinkModel)
                .values(
                    link_id=link_id,
                    improvement_id=improvement_id,
                    kind=clean_kind,
                    ref_id=clean_ref,
                    created_at=now,
                )
                .on_conflict_do_nothing(index_elements=["improvement_id", "kind", "ref_id"])
            )
            row = (
                db.query(ImprovementLinkModel)
                .filter(
                    ImprovementLinkModel.improvement_id == improvement_id,
                    ImprovementLinkModel.kind == clean_kind,
                    ImprovementLinkModel.ref_id == clean_ref,
                )
                .one()
            )
            return ImprovementLinkRecord(
                link_id=row.link_id,
                improvement_id=row.improvement_id,
                kind=row.kind,
                ref_id=row.ref_id,
                created_at=row.created_at,
            )

    def list_links(self, improvement_id: str) -> list[ImprovementLinkRecord]:
        with self._session_factory.begin() as db:
            rows = (
                db.query(ImprovementLinkModel)
                .filter(ImprovementLinkModel.improvement_id == improvement_id)
                .order_by(ImprovementLinkModel.created_at, ImprovementLinkModel.link_id)
                .all()
            )
            return [
                ImprovementLinkRecord(
                    link_id=row.link_id,
                    improvement_id=row.improvement_id,
                    kind=row.kind,
                    ref_id=row.ref_id,
                    created_at=row.created_at,
                )
                for row in rows
            ]

    def archive_improvement(self, improvement_id: str) -> ImprovementItemRecord:
        """归档改进事项：improvement_status 置为终态 archived（不改 stage），归档后不再推进阶段。"""
        with self._session_factory.begin() as db:
            row = db.get(ImprovementItemModel, improvement_id)
            if row is None:
                raise NotFoundError(f"ImprovementItem not found: {improvement_id}")
            if row.improvement_status == "archived":
                return _record(row)
            row = self._lock_mutable_improvement(db, improvement_id)
            self._assert_execution_settled(db, improvement_id, action="archive")
            row.improvement_status = "archived"
            row.updated_at = utc_now()
            return _record(row)

    def update_title(self, improvement_id: str, *, title: str) -> ImprovementItemRecord:
        """回填/更新事项标题（反馈整理生成的简洁 title 取代前端截断的自动标题）。"""
        clean = (title or "").strip()
        if not clean:
            raise BusinessRuleViolation("ImprovementItem title cannot be empty")
        with self._session_factory.begin() as db:
            row = self._lock_mutable_improvement(db, improvement_id)
            row.title = clean
            row.updated_at = utc_now()
            return _record(row)

    def deletion_impact(self, improvement_id: str) -> ImprovementDeletionImpact:
        """删除前影响面（dry-run，区别于归档）：将随删的反馈/链接/内容计数 + 退回未归属池的来源反馈引用数。"""
        with self._session_factory.begin() as db:
            row = db.get(ImprovementItemModel, improvement_id)
            if row is None:
                raise NotFoundError(f"ImprovementItem not found: {improvement_id}")

            def _count(model) -> int:
                return db.query(model).filter(model.improvement_id == improvement_id).count()

            return ImprovementDeletionImpact(
                improvement_id=improvement_id,
                title=row.title,
                source_feedback_refs=len(row.source_feedback_refs_json or []),
                feedbacks=_count(ImprovementFeedbackModel),
                links=_count(ImprovementLinkModel),
                has_attribution=_count(AttributionModel) > 0,
                has_optimization_plan=_count(OptimizationPlanModel) > 0,
            )

    def delete_improvement(self, improvement_id: str) -> None:
        """硬删除改进事项及其 1:多 反馈、1:1 内容、轻引用（同一 improvement_db 内原子级联）。

        一等反馈 FeedbackCase 存于独立 feedback 域、不在此删除：删后它们不再被任何事项引用，
        退回未归属池可重新归入别处（区别于归档——归档保留事项与反馈）。
        """
        with self._session_factory.begin() as db:
            row = db.get(ImprovementItemModel, improvement_id)
            if row is None:
                raise NotFoundError(f"ImprovementItem not found: {improvement_id}")
            if row.improvement_status != "archived":
                row = self._lock_mutable_improvement(db, improvement_id)
            self._assert_execution_settled(db, improvement_id, action="delete")
            for model in (
                ImprovementFeedbackCaseAssignmentModel,
                ImprovementFeedbackModel,
                ImprovementLinkModel,
                NormalizedFeedbackModel,
                AttributionModel,
                OptimizationPlanModel,
                ExecutionRecordModel,
                RegressionTestDesignModel,
            ):
                db.query(model).filter(model.improvement_id == improvement_id).delete(synchronize_session=False)
            db.delete(row)


def _record(row: ImprovementItemModel) -> ImprovementItemRecord:
    return ImprovementItemRecord(
        improvement_id=row.improvement_id,
        agent_id=row.agent_id,
        title=row.title,
        summary=row.summary or "",
        source_feedback_refs=list(row.source_feedback_refs_json or []),
        improvement_stage=row.improvement_stage or "feedback_intake",
        improvement_status=row.improvement_status or "active",
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
