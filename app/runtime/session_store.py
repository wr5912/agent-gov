from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from . import session_turn_lease
from .errors import SessionConflictError
from .json_types import JsonObject
from .records.source_records import AgentRunRecord, upsert_agent_run_record
from .runtime_db import SessionRecordModel, make_session_factory, runtime_db_path_from_data_dir, utc_now


@dataclass
class LocalSession:
    session_id: str
    sdk_session_id: Optional[str] = None
    agent_id: Optional[str] = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    title: Optional[str] = None
    turns: int = 0
    metadata: JsonObject = field(default_factory=dict)
    active_run_id: Optional[str] = None
    active_run_expires_at: Optional[str] = None


class LocalSessionStore:
    """SQLite-backed session store for API-visible session mappings."""

    def __init__(self, root: Path) -> None:
        # Kept as ``root`` for API compatibility with the previous file store. The
        # actual persistent state is DATA_DIR/runtime.sqlite3.
        self.root = root
        self.data_dir = root.parent
        self.db_path = runtime_db_path_from_data_dir(self.data_dir)
        self.Session = make_session_factory(self.db_path)

    def create(self, metadata: Optional[JsonObject] = None) -> LocalSession:
        session = LocalSession(session_id=str(uuid.uuid4()), metadata=metadata or {})
        self.save(session)
        return session

    def get_or_create(self, session_id: Optional[str], metadata: Optional[JsonObject] = None) -> LocalSession:
        if session_id:
            return self._insert_if_absent(session_id, metadata=metadata)
        return self.create(metadata=metadata)

    def get_or_create_owned(
        self,
        session_id: Optional[str],
        *,
        agent_id: str,
        metadata: Optional[JsonObject] = None,
    ) -> LocalSession:
        """Atomically create/claim a session for one backend-selected Agent."""
        normalized_agent_id = agent_id.strip()
        if not normalized_agent_id:
            raise ValueError("agent_id is required to claim a session")
        if not session_id:
            session_id = str(uuid.uuid4())
        self._insert_if_absent(session_id, metadata=metadata, agent_id=normalized_agent_id)
        now = utc_now()
        with self.Session.begin() as db:
            db.execute(
                update(SessionRecordModel)
                .where(
                    SessionRecordModel.session_id == session_id,
                    SessionRecordModel.agent_id.is_(None),
                    SessionRecordModel.turns == 0,
                    SessionRecordModel.sdk_session_id.is_(None),
                )
                .values(agent_id=normalized_agent_id, updated_at=now)
            )
            record = db.get(SessionRecordModel, session_id)
            if record is None:
                raise SessionConflictError(f"Session {session_id} disappeared while claiming ownership")
            if record.agent_id is None:
                raise SessionConflictError(f"Session {session_id} has no unambiguous business agent owner")
            if record.agent_id != normalized_agent_id:
                raise SessionConflictError(f"Session {session_id} belongs to a different business agent")
            return self._to_session(record)

    def get(self, session_id: str) -> Optional[LocalSession]:
        with self.Session() as db:
            record = db.get(SessionRecordModel, session_id)
            return self._to_session(record) if record else None

    def save(self, session: LocalSession) -> None:
        expected_updated_at = session.updated_at
        next_updated_at = utc_now()
        insert_stmt = sqlite_insert(SessionRecordModel).values(
            session_id=session.session_id,
            sdk_session_id=session.sdk_session_id,
            agent_id=session.agent_id,
            created_at=session.created_at,
            updated_at=next_updated_at,
            title=session.title,
            turns=session.turns,
            metadata_json=session.metadata,
            active_run_id=session.active_run_id,
            active_run_expires_at=session.active_run_expires_at,
        )
        excluded = insert_stmt.excluded
        upsert_stmt = insert_stmt.on_conflict_do_update(
            index_elements=[SessionRecordModel.session_id],
            set_={
                "sdk_session_id": excluded.sdk_session_id,
                "agent_id": excluded.agent_id,
                "updated_at": excluded.updated_at,
                "title": excluded.title,
                "turns": excluded.turns,
                "metadata_json": excluded.metadata_json,
            },
            where=and_(
                SessionRecordModel.updated_at == expected_updated_at,
                SessionRecordModel.turns == session.turns,
                SessionRecordModel.active_run_id.is_(None),
                or_(
                    SessionRecordModel.agent_id == excluded.agent_id,
                    and_(
                        SessionRecordModel.agent_id.is_(None),
                        SessionRecordModel.turns == 0,
                        SessionRecordModel.sdk_session_id.is_(None),
                    ),
                ),
            ),
        )
        with self.Session.begin() as db:
            result = db.execute(upsert_stmt)
            if result.rowcount != 1:
                self._raise_conflict(db.get(SessionRecordModel, session.session_id), session)
        session.updated_at = next_updated_at

    def claim_turn(
        self,
        session: LocalSession,
        *,
        run_id: str,
        agent_id: str,
        lease_seconds: float | None = None,
    ) -> LocalSession:
        """CAS-claim one active SDK turn so concurrent turns and deletion fail closed."""
        clean_run_id = run_id.strip()
        if not clean_run_id:
            raise ValueError("run_id is required to claim a session turn")
        now = utc_now()
        effective_lease_seconds = session_turn_lease.DEFAULT_SESSION_TURN_LEASE_SECONDS if lease_seconds is None else lease_seconds
        expires_at = session_turn_lease.turn_lease_expires_at(effective_lease_seconds)
        statement = (
            update(SessionRecordModel)
            .where(
                SessionRecordModel.session_id == session.session_id,
                SessionRecordModel.updated_at == session.updated_at,
                SessionRecordModel.turns == session.turns,
                SessionRecordModel.agent_id == agent_id,
                or_(
                    SessionRecordModel.active_run_id.is_(None),
                    and_(
                        SessionRecordModel.active_run_expires_at.is_not(None),
                        SessionRecordModel.active_run_expires_at <= now,
                    ),
                ),
            )
            .values(active_run_id=clean_run_id, active_run_expires_at=expires_at, updated_at=now)
        )
        with self.Session.begin() as db:
            result = db.execute(statement)
            record = db.get(SessionRecordModel, session.session_id)
            if result.rowcount != 1 or record is None:
                if record is not None and self._record_has_active_run(record):
                    raise SessionConflictError(f"Session {session.session_id} already has an active turn")
                self._raise_conflict(record, session, agent_id=agent_id)
            return self._to_session(record)

    def renew_turn(
        self,
        session_id: str,
        *,
        run_id: str,
        lease_seconds: float | None = None,
    ) -> str:
        """使用 run_id fencing 续租；所有权丢失时必须显式失败。"""
        clean_run_id = run_id.strip()
        if not clean_run_id:
            raise ValueError("run_id is required to renew a session turn")
        effective_lease_seconds = session_turn_lease.DEFAULT_SESSION_TURN_LEASE_SECONDS if lease_seconds is None else lease_seconds
        if effective_lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive when renewing a session turn")
        expires_at = session_turn_lease.turn_lease_expires_at(effective_lease_seconds)
        statement = (
            update(SessionRecordModel)
            .where(
                SessionRecordModel.session_id == session_id,
                SessionRecordModel.active_run_id == clean_run_id,
            )
            .values(active_run_expires_at=expires_at)
        )
        with self.Session.begin() as db:
            result = db.execute(statement)
            if result.rowcount != 1:
                raise SessionConflictError(f"Session {session_id} active turn is no longer owned by run {clean_run_id}")
        return expires_at

    def complete_turn(
        self,
        session: LocalSession,
        *,
        run_id: str,
        agent_id: str,
        sdk_session_id: Optional[str],
        title: str,
        run_record: AgentRunRecord | None = None,
    ) -> LocalSession:
        """CAS one completed turn and optionally persist its run in the same transaction."""
        next_updated_at = utc_now()
        statement = (
            update(SessionRecordModel)
            .where(
                SessionRecordModel.session_id == session.session_id,
                SessionRecordModel.updated_at == session.updated_at,
                SessionRecordModel.turns == session.turns,
                SessionRecordModel.agent_id == agent_id,
                SessionRecordModel.active_run_id == run_id,
            )
            .values(
                sdk_session_id=sdk_session_id if sdk_session_id is not None else session.sdk_session_id,
                updated_at=next_updated_at,
                title=session.title or title,
                turns=SessionRecordModel.turns + 1,
                active_run_id=None,
                active_run_expires_at=None,
            )
        )
        with self.Session.begin() as db:
            result = db.execute(statement)
            if result.rowcount != 1:
                self._raise_conflict(db.get(SessionRecordModel, session.session_id), session, agent_id=agent_id)
            record = db.get(SessionRecordModel, session.session_id)
            if record is None:  # pragma: no cover - guarded by rowcount
                raise SessionConflictError(f"Session {session.session_id} disappeared while completing a turn")
            if run_record is not None:
                if run_record.session_id != session.session_id:
                    raise ValueError("Agent run session_id must match the completed session")
                upsert_agent_run_record(db, run_record)
            return self._to_session(record)

    def release_turn(self, session_id: str, *, run_id: str) -> bool:
        """Release an unfinished claim after an exception or client cancellation."""
        with self.Session.begin() as db:
            result = db.execute(
                update(SessionRecordModel)
                .where(
                    SessionRecordModel.session_id == session_id,
                    SessionRecordModel.active_run_id == run_id,
                )
                .values(active_run_id=None, active_run_expires_at=None, updated_at=utc_now())
            )
            return result.rowcount == 1

    def clear_sdk_session(
        self,
        session: LocalSession,
        *,
        agent_id: str,
        run_id: Optional[str] = None,
    ) -> LocalSession:
        """Invalidate one known SDK mapping without overwriting concurrent session fields."""
        if session.sdk_session_id is None:
            return session
        next_updated_at = utc_now()
        lease_condition = SessionRecordModel.active_run_id == run_id if run_id is not None else SessionRecordModel.active_run_id.is_(None)
        statement = (
            update(SessionRecordModel)
            .where(
                SessionRecordModel.session_id == session.session_id,
                SessionRecordModel.updated_at == session.updated_at,
                SessionRecordModel.turns == session.turns,
                SessionRecordModel.agent_id == agent_id,
                SessionRecordModel.sdk_session_id == session.sdk_session_id,
                lease_condition,
            )
            .values(sdk_session_id=None, updated_at=next_updated_at)
        )
        with self.Session.begin() as db:
            result = db.execute(statement)
            if result.rowcount != 1:
                self._raise_conflict(db.get(SessionRecordModel, session.session_id), session, agent_id=agent_id)
            record = db.get(SessionRecordModel, session.session_id)
            if record is None:  # pragma: no cover - guarded by rowcount
                raise SessionConflictError(f"Session {session.session_id} disappeared while invalidating SDK state")
            return self._to_session(record)

    def list(self) -> list[LocalSession]:
        with self.Session() as db:
            records = db.scalars(select(SessionRecordModel).order_by(SessionRecordModel.updated_at.desc())).all()
            return [self._to_session(record) for record in records]

    def delete(self, session_id: str) -> bool:
        with self.Session.begin() as db:
            record = db.get(SessionRecordModel, session_id)
            if not record:
                return False
            if self._record_has_active_run(record):
                raise SessionConflictError(f"Session {session_id} has an active turn and cannot be deleted")
            db.delete(record)
            return True

    def _insert_if_absent(
        self,
        session_id: str,
        *,
        metadata: Optional[JsonObject],
        agent_id: Optional[str] = None,
    ) -> LocalSession:
        now = utc_now()
        statement = (
            sqlite_insert(SessionRecordModel)
            .values(
                session_id=session_id,
                sdk_session_id=None,
                agent_id=agent_id,
                created_at=now,
                updated_at=now,
                title=None,
                turns=0,
                metadata_json=dict(metadata or {}),
                active_run_id=None,
                active_run_expires_at=None,
            )
            .on_conflict_do_nothing(index_elements=[SessionRecordModel.session_id])
        )
        with self.Session.begin() as db:
            db.execute(statement)
            record = db.get(SessionRecordModel, session_id)
            if record is None:  # pragma: no cover - insert/select are one transaction
                raise SessionConflictError(f"Session {session_id} could not be created")
            return self._to_session(record)

    @staticmethod
    def _raise_conflict(
        record: Optional[SessionRecordModel],
        session: LocalSession,
        *,
        agent_id: Optional[str] = None,
    ) -> None:
        if record is None:
            raise SessionConflictError(f"Session {session.session_id} was deleted concurrently")
        expected_agent_id = agent_id if agent_id is not None else session.agent_id
        if record.agent_id is None and (record.turns > 0 or record.sdk_session_id is not None):
            raise SessionConflictError(f"Session {session.session_id} has no unambiguous business agent owner")
        if record.agent_id and record.agent_id != expected_agent_id:
            raise SessionConflictError(f"Session {session.session_id} belongs to a different business agent")
        raise SessionConflictError(f"Session {session.session_id} changed concurrently; retry with the latest conversation state")

    @staticmethod
    def _record_has_active_run(record: SessionRecordModel) -> bool:
        if not record.active_run_id:
            return False
        if not record.active_run_expires_at:
            return True
        return record.active_run_expires_at > utc_now()

    def _to_session(self, record: SessionRecordModel) -> LocalSession:
        return LocalSession(
            session_id=record.session_id,
            sdk_session_id=record.sdk_session_id,
            agent_id=record.agent_id,
            created_at=record.created_at,
            updated_at=record.updated_at,
            title=record.title,
            turns=record.turns,
            metadata=record.metadata_json or {},
            active_run_id=record.active_run_id if self._record_has_active_run(record) else None,
            active_run_expires_at=(record.active_run_expires_at if self._record_has_active_run(record) else None),
        )
