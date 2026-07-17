from __future__ import annotations

from collections.abc import Collection

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.runtime.runtime_db import AgentChangeSetModel


def has_open_change_sets(
    session_factory: sessionmaker,
    *,
    agent_id: str,
    terminal_states: Collection[str],
) -> bool:
    with session_factory() as db:
        change_set_id = db.scalar(
            select(AgentChangeSetModel.change_set_id)
            .where(
                AgentChangeSetModel.agent_id == agent_id,
                AgentChangeSetModel.status.not_in(tuple(terminal_states)),
            )
            .limit(1)
        )
    return change_set_id is not None
