from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from sqlalchemy import select

from .json_types import JsonObject
from .runtime_db import SessionRecordModel, make_session_factory, runtime_db_path_from_data_dir, utc_now


@dataclass
class LocalSession:
    session_id: str
    sdk_session_id: Optional[str] = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    title: Optional[str] = None
    turns: int = 0
    metadata: JsonObject = field(default_factory=dict)


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
            existing = self.get(session_id)
            if existing:
                return existing
            session = LocalSession(session_id=session_id, metadata=metadata or {})
            self.save(session)
            return session
        return self.create(metadata=metadata)

    def get(self, session_id: str) -> Optional[LocalSession]:
        with self.Session() as db:
            record = db.get(SessionRecordModel, session_id)
            return self._to_session(record) if record else None

    def save(self, session: LocalSession) -> None:
        session.updated_at = utc_now()
        with self.Session.begin() as db:
            existing = db.get(SessionRecordModel, session.session_id)
            if existing:
                existing.sdk_session_id = session.sdk_session_id
                existing.updated_at = session.updated_at
                existing.title = session.title
                existing.turns = session.turns
                existing.metadata_json = session.metadata
            else:
                db.add(
                    SessionRecordModel(
                        session_id=session.session_id,
                        sdk_session_id=session.sdk_session_id,
                        created_at=session.created_at,
                        updated_at=session.updated_at,
                        title=session.title,
                        turns=session.turns,
                        metadata_json=session.metadata,
                    )
                )

    def list(self) -> list[LocalSession]:
        with self.Session() as db:
            records = db.scalars(select(SessionRecordModel).order_by(SessionRecordModel.updated_at.desc())).all()
            return [self._to_session(record) for record in records]

    def delete(self, session_id: str) -> bool:
        with self.Session.begin() as db:
            record = db.get(SessionRecordModel, session_id)
            if not record:
                return False
            db.delete(record)
            return True

    def _to_session(self, record: SessionRecordModel) -> LocalSession:
        return LocalSession(
            session_id=record.session_id,
            sdk_session_id=record.sdk_session_id,
            created_at=record.created_at,
            updated_at=record.updated_at,
            title=record.title,
            turns=record.turns,
            metadata=record.metadata_json or {},
        )
