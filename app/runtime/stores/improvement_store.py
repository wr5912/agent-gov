from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy.orm import sessionmaker

from ..errors import BusinessRuleViolation, ConflictError, NotFoundError
from ..improvement_db import ImprovementItemModel
from ..runtime_db import utc_now
from ..state_machines import validate_transition


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

    def archive_improvement(self, improvement_id: str) -> ImprovementItemRecord:
        """归档改进事项：improvement_status 置为终态 archived（不改 stage），归档后不再推进阶段。"""
        with self._session_factory.begin() as db:
            row = db.get(ImprovementItemModel, improvement_id)
            if row is None:
                raise NotFoundError(f"ImprovementItem not found: {improvement_id}")
            row.improvement_status = "archived"
            row.updated_at = utc_now()
            return _record(row)


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
