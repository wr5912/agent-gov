from __future__ import annotations

from sqlalchemy import select

from app.runtime.runtime_db import AgentReleaseModel
from app.services.agent_governance_errors import AgentGovernanceError
from app.services.agent_publication import PublicationIntent


def parse_publication_intent(value: object) -> PublicationIntent:
    try:
        return PublicationIntent.from_payload(value)
    except ValueError as exc:
        raise AgentGovernanceError(409, "Agent change set has an invalid publication intent") from exc


def release_row_for_change_set(db: object, change_set_id: str) -> AgentReleaseModel | None:
    rows = list(
        db.scalars(
            select(AgentReleaseModel).where(AgentReleaseModel.change_set_id == change_set_id).order_by(AgentReleaseModel.created_at.desc()).limit(2)
        ).all()
    )
    if len(rows) > 1:
        raise AgentGovernanceError(409, "Agent change set has multiple release records")
    return rows[0] if rows else None
