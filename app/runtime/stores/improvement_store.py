from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy.orm import sessionmaker

from ..errors import BusinessRuleViolation, ConflictError, NotFoundError
from ..improvement_db import (
    AttributionModel,
    ExecutionRecordModel,
    ImprovementFeedbackModel,
    ImprovementItemModel,
    ImprovementLinkModel,
    NormalizedFeedbackModel,
    OptimizationPlanModel,
    RegressionAssessmentModel,
)
from ..runtime_db import utc_now
from ..state_machines import validate_transition

# 改进事项可引用的既有闭环对象类型（W2-c 轻引用）。
LINK_KINDS = {"attribution", "optimization_plan", "eval_run", "change_set", "batch"}


@dataclass(frozen=True)
class ImprovementLinkRecord:
    link_id: str
    improvement_id: str
    kind: str
    ref_id: str
    created_at: str


@dataclass(frozen=True)
class ImprovementItemRecord:
    """改进事项事项级领域记录（v2.7 跨代重建的单一事实来源）。"""

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


class ImprovementStore:
    """改进事项存储：按 agent_id 归属与查询；阶段转移交集中状态机判定合法性。"""

    def __init__(self, session_factory: sessionmaker) -> None:
        self._session_factory = session_factory

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

    def all_source_feedback_refs(self) -> set[str]:
        """所有改进事项已登记的来源反馈引用集合——用于判定一等反馈 Case 是否「未归属」可选。"""
        with self._session_factory.begin() as db:
            refs: set[str] = set()
            for (json_refs,) in db.query(ImprovementItemModel.source_feedback_refs_json).all():
                refs.update(str(r) for r in (json_refs or []))
            return refs

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

    def transition_stage(self, improvement_id: str, *, stage: str) -> ImprovementItemRecord:
        """改进事项阶段转移；非法转移由 state_machines 抛 StateTransitionError(409)。

        已归档（archived）为终态状态：不再接受阶段推进，转移请求被拒（409）。
        """
        with self._session_factory.begin() as db:
            row = db.get(ImprovementItemModel, improvement_id)
            if row is None:
                raise NotFoundError(f"ImprovementItem not found: {improvement_id}")
            if row.improvement_status == "archived":
                raise ConflictError(f"Archived improvement cannot transition: {improvement_id}")
            validate_transition("improvement_stage", row.improvement_stage or "feedback_intake", stage)
            row.improvement_stage = stage
            row.improvement_status = derive_improvement_status(stage)
            row.updated_at = utc_now()
            return _record(row)

    def add_source_refs(self, improvement_id: str, refs: list[str]) -> ImprovementItemRecord:
        """把来源反馈引用并入已有改进事项（去重）。"""
        clean = [str(r).strip() for r in (refs or []) if str(r).strip()]
        with self._session_factory.begin() as db:
            row = db.get(ImprovementItemModel, improvement_id)
            if row is None:
                raise NotFoundError(f"ImprovementItem not found: {improvement_id}")
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
            target = db.get(ImprovementItemModel, target_id)
            source = db.get(ImprovementItemModel, source_id)
            if target is None:
                raise NotFoundError(f"ImprovementItem not found: {target_id}")
            if source is None:
                raise NotFoundError(f"ImprovementItem not found: {source_id}")
            if target.agent_id != source.agent_id:
                raise BusinessRuleViolation("Cannot merge improvements across different business agents")
            if source.improvement_status == "archived":
                raise ConflictError(f"Source improvement already archived/merged: {source_id}")
            existing = list(target.source_feedback_refs_json or [])
            for ref in source.source_feedback_refs_json or []:
                if ref not in existing:
                    existing.append(ref)
            target.source_feedback_refs_json = existing
            target.updated_at = utc_now()
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
            row = db.get(ImprovementItemModel, improvement_id)
            if row is None:
                raise NotFoundError(f"ImprovementItem not found: {improvement_id}")
            refs = list(row.source_feedback_refs_json or [])
            if clean not in refs:
                raise BusinessRuleViolation(f"feedback_ref not in improvement: {clean}")
            row.source_feedback_refs_json = [ref for ref in refs if ref != clean]
            row.updated_at = now
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
        """把改进事项与一个既有闭环对象建立轻引用（W2-c）。"""
        clean_kind = (kind or "").strip()
        clean_ref = (ref_id or "").strip()
        if clean_kind not in LINK_KINDS:
            raise BusinessRuleViolation(f"Unknown link kind: {clean_kind}; expected one of {sorted(LINK_KINDS)}")
        if not clean_ref:
            raise BusinessRuleViolation("link ref_id cannot be empty")
        link_id = f"lnk-{uuid4().hex[:12]}"
        now = utc_now()
        with self._session_factory.begin() as db:
            if db.get(ImprovementItemModel, improvement_id) is None:
                raise NotFoundError(f"ImprovementItem not found: {improvement_id}")
            db.add(
                ImprovementLinkModel(
                    link_id=link_id,
                    improvement_id=improvement_id,
                    kind=clean_kind,
                    ref_id=clean_ref,
                    created_at=now,
                )
            )
        return ImprovementLinkRecord(link_id=link_id, improvement_id=improvement_id, kind=clean_kind, ref_id=clean_ref, created_at=now)

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
            row.improvement_status = "archived"
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
            for model in (
                ImprovementFeedbackModel,
                ImprovementLinkModel,
                NormalizedFeedbackModel,
                AttributionModel,
                OptimizationPlanModel,
                ExecutionRecordModel,
                RegressionAssessmentModel,
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
