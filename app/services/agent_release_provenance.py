"""从既有反馈归属与发布记录派生双向来源引用，不复制 Git 资产。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.runtime.improvement_db import ImprovementFeedbackCaseAssignmentModel
from app.runtime.json_types import JsonObject
from app.runtime.protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from app.runtime.runtime_db import AgentReleaseModel


@dataclass(frozen=True)
class ReleasedVersionReference:
    release_id: str
    agent_id: str
    status: str
    change_set_id: str | None
    commit_sha: str


@dataclass(frozen=True)
class ReleaseFeedbackSources:
    by_release_id: dict[str, list[str]]

    def for_release(self, release_id: str) -> list[str]:
        return self.by_release_id[release_id]


def released_versions_for_feedback_case(
    session_factory: sessionmaker,
    feedback_case_id: str,
) -> list[ReleasedVersionReference]:
    """只接受同 Agent 的事项归属与已存在 release 两端均成立的关联。"""

    with session_factory() as db:
        assignment = db.get(ImprovementFeedbackCaseAssignmentModel, feedback_case_id)
        if assignment is None:
            return []
        rows = db.scalars(
            select(AgentReleaseModel)
            .where(AgentReleaseModel.agent_id == assignment.agent_id)
            .order_by(AgentReleaseModel.created_at, AgentReleaseModel.release_id)
        ).all()
        return [
            ReleasedVersionReference(
                release_id=row.release_id,
                agent_id=row.agent_id,
                status=row.status,
                change_set_id=row.change_set_id,
                commit_sha=row.commit_sha,
            )
            for row in rows
            if (row.payload_json or {}).get("source_improvement_id") == assignment.improvement_id
        ]


def source_feedback_case_ids_by_release(
    session_factory: sessionmaker,
    releases: Sequence[JsonObject],
) -> ReleaseFeedbackSources:
    """一次查询投影列表/详情的反向反馈来源；无来源声明的发布不猜测关联。"""

    sources = {source_id for release in releases if isinstance(source_id := release.get("source_improvement_id"), str) and source_id}
    result: dict[str, list[str]] = {str(release["release_id"]): [] for release in releases}
    if not sources:
        return ReleaseFeedbackSources(result)
    with session_factory() as db:
        assignments = db.scalars(select(ImprovementFeedbackCaseAssignmentModel).where(ImprovementFeedbackCaseAssignmentModel.improvement_id.in_(sources))).all()
    by_source: dict[tuple[str, str], list[str]] = {}
    for assignment in assignments:
        by_source.setdefault((assignment.agent_id, assignment.improvement_id), []).append(assignment.feedback_case_id)
    for release in releases:
        source_id = release.get("source_improvement_id")
        if not isinstance(source_id, str) or not source_id:
            continue
        agent_id = str(release.get("agent_id") or DEFAULT_BUSINESS_AGENT_ID)
        result[str(release["release_id"])] = sorted(set(by_source.get((agent_id, source_id), [])))
    return ReleaseFeedbackSources(result)
