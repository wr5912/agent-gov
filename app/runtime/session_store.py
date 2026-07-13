from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from . import session_turn_lease
from .agent_admission import claim_runtime_admission, lease_expires_at
from .errors import SessionConflictError
from .json_types import JsonObject
from .records.source_records import AgentRunRecord, upsert_agent_run_record
from .runtime_db import SessionRecordModel, SessionTurnIntentModel, make_session_factory, runtime_db_path_from_data_dir, utc_now
from .sdk_session_store import discard_staged_entries, promote_staged_entries
from .session_turn_persistence import (
    TurnIntentSpec,
    add_running_turn_intent,
    assert_aborted_persisted_turn,
    assert_completed_persisted_turn,
)
from .session_turn_persistence import (
    abort_persisted_turn as abort_persisted_turn_transaction,
)
from .session_turn_persistence import (
    complete_persisted_turn as complete_persisted_turn_transaction,
)
from .session_turn_persistence import (
    reconcile_expired_turns as reconcile_expired_turn_transactions,
)


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
    active_run_generation: int = 0
    sdk_project_key: Optional[str] = None
    sdk_store_ready_at: Optional[str] = None
    sdk_store_migration_error: Optional[str] = None


@dataclass(frozen=True)
class SdkStoreImportClaim:
    session_id: str
    sdk_session_id: str
    sdk_project_key: str
    token: str
    expires_at: str

    @property
    def marker(self) -> str:
        return f"migration_running:{self.token}:{self.expires_at}"


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
            active_run_generation=session.active_run_generation,
            sdk_project_key=session.sdk_project_key,
            sdk_store_ready_at=session.sdk_store_ready_at,
            sdk_store_migration_error=session.sdk_store_migration_error,
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
                "sdk_project_key": excluded.sdk_project_key,
                "sdk_store_ready_at": excluded.sdk_store_ready_at,
                "sdk_store_migration_error": excluded.sdk_store_migration_error,
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

    def begin_persisted_turn(
        self,
        session: LocalSession,
        *,
        run_id: str,
        agent_id: str,
        attempted_sdk_session_id: str,
        sdk_project_key: str,
        request: JsonObject,
        created_at: str,
        lease_seconds: float | None = None,
    ) -> LocalSession:
        """原子获取 Agent/session admission，并创建唯一 running intent。"""
        clean_run_id = run_id.strip()
        clean_sdk_session_id = attempted_sdk_session_id.strip()
        clean_project_key = sdk_project_key.strip()
        if not clean_run_id or not clean_sdk_session_id or not clean_project_key:
            raise ValueError("run_id, attempted_sdk_session_id, and sdk_project_key are required")

        # 先幂等收口旧的过期 intent；新的 claim+intent 本身仍在下方同一事务完成。
        reconcile_expired_turn_transactions(self.Session, session_id=session.session_id)
        effective_lease_seconds = session_turn_lease.DEFAULT_SESSION_TURN_LEASE_SECONDS if lease_seconds is None else lease_seconds
        if effective_lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive for persisted turns")
        expires_at = session_turn_lease.turn_lease_expires_at(effective_lease_seconds)
        now = utc_now()
        with self.Session.begin() as db:
            record = db.get(SessionRecordModel, session.session_id)
            if record is None:
                self._raise_conflict(record, session, agent_id=agent_id)
            assert record is not None
            if record.agent_id != agent_id:
                self._raise_conflict(record, session, agent_id=agent_id)
            if record.turns != session.turns or record.sdk_session_id != session.sdk_session_id:
                self._raise_conflict(record, session, agent_id=agent_id)
            if record.active_run_id is not None:
                prior_intent = db.get(SessionTurnIntentModel, record.active_run_id)
                legacy_expired = (
                    record.active_run_expires_at is not None
                    and record.active_run_expires_at <= now
                    and (prior_intent is None or prior_intent.status != "running")
                )
                if not legacy_expired:
                    raise SessionConflictError(f"Session {session.session_id} already has an active turn")
                record.active_run_id = None
                record.active_run_expires_at = None
                record.active_run_generation = 0

            generation = claim_runtime_admission(db, agent_id=agent_id, now=now)
            record.active_run_id = clean_run_id
            record.active_run_expires_at = expires_at
            record.active_run_generation = generation
            record.updated_at = now
            add_running_turn_intent(
                db,
                TurnIntentSpec(
                    run_id=clean_run_id,
                    session_id=record.session_id,
                    agent_id=agent_id,
                    source_sdk_session_id=record.sdk_session_id,
                    attempted_sdk_session_id=clean_sdk_session_id,
                    sdk_project_key=clean_project_key,
                    base_turns=record.turns,
                    request=dict(request),
                    created_at=created_at,
                ),
            )
            return self._to_session(record)

    def renew_turn(
        self,
        session_id: str,
        *,
        run_id: str,
        run_generation: int | None = None,
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
        conditions = [
            SessionRecordModel.session_id == session_id,
            SessionRecordModel.active_run_id == clean_run_id,
        ]
        if run_generation is not None:
            conditions.append(SessionRecordModel.active_run_generation == run_generation)
        statement = update(SessionRecordModel).where(*conditions).values(active_run_expires_at=expires_at)
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

    def finalize_persisted_turn(
        self,
        *,
        session_id: str,
        run_id: str,
        run_generation: int,
        sdk_session_id: str,
        title: str,
        run_record: AgentRunRecord,
        terminal_status: str,
        completed_at: str,
    ) -> LocalSession:
        if terminal_status not in {"succeeded", "failed"}:
            raise ValueError("ResultMessage turn status must be succeeded or failed")
        with self.Session.begin() as db:
            record = db.get(SessionRecordModel, session_id)
            if record is None:
                raise SessionConflictError(f"Session {session_id} was deleted concurrently")
            intent = db.get(SessionTurnIntentModel, run_id)
            if intent is not None and intent.status == terminal_status:
                assert_completed_persisted_turn(
                    db,
                    session=record,
                    run_id=run_id,
                    sdk_session_id=sdk_session_id,
                    run_record=run_record,
                    terminal_status=terminal_status,  # type: ignore[arg-type]
                    completed_at=completed_at,
                )
                return self._to_session(record)
            complete_persisted_turn_transaction(
                db,
                session=record,
                run_id=run_id,
                run_generation=run_generation,
                sdk_session_id=sdk_session_id,
                title=title,
                run_record=run_record,
                terminal_status=terminal_status,  # type: ignore[arg-type]
                completed_at=completed_at,
            )
            return self._to_session(record)

    def abort_persisted_turn(
        self,
        *,
        session_id: str,
        run_id: str,
        run_generation: int,
        run_record: AgentRunRecord,
        terminal_status: str,
        error: JsonObject,
        completed_at: str,
    ) -> LocalSession:
        if terminal_status not in {"failed", "cancelled"}:
            raise ValueError("Aborted turn status must be failed or cancelled")
        with self.Session.begin() as db:
            record = db.get(SessionRecordModel, session_id)
            if record is None:
                raise SessionConflictError(f"Session {session_id} was deleted concurrently")
            intent = db.get(SessionTurnIntentModel, run_id)
            if intent is not None and intent.status == terminal_status:
                assert_aborted_persisted_turn(
                    db,
                    session=record,
                    run_id=run_id,
                    run_record=run_record,
                    terminal_status=terminal_status,  # type: ignore[arg-type]
                    error=error,
                    completed_at=completed_at,
                )
                return self._to_session(record)
            abort_persisted_turn_transaction(
                db,
                session=record,
                run_id=run_id,
                run_generation=run_generation,
                run_record=run_record,
                terminal_status=terminal_status,  # type: ignore[arg-type]
                error=error,
                completed_at=completed_at,
            )
            return self._to_session(record)

    def reconcile_expired_turns(self, *, session_id: str | None = None, limit: int = 100) -> list[str]:
        return reconcile_expired_turn_transactions(self.Session, session_id=session_id, limit=limit)

    def begin_sdk_store_import(
        self,
        *,
        session_id: str,
        sdk_session_id: str,
        sdk_project_key: str,
        lease_seconds: float = 3600.0,
        now: str | None = None,
    ) -> SdkStoreImportClaim | None:
        """在 SQLite 写锁下获取唯一 legacy import claim。"""
        current = now or utc_now()
        expires_at = lease_expires_at(lease_seconds, now=current)
        with self.Session.begin() as db:
            db.execute(update(SessionRecordModel).where(SessionRecordModel.session_id == session_id).values(updated_at=SessionRecordModel.updated_at))
            record = db.get(SessionRecordModel, session_id)
            if record is None:
                raise SessionConflictError(f"Session {session_id} was deleted concurrently")
            if record.sdk_session_id != sdk_session_id:
                raise SessionConflictError(f"Session {session_id} SDK mapping changed during migration")
            if record.sdk_store_ready_at is not None:
                if record.sdk_project_key != sdk_project_key:
                    raise SessionConflictError(f"Session {session_id} SDK project key changed")
                return None
            if record.active_run_id:
                raise SessionConflictError(f"Session {session_id} has an active turn and cannot be migrated")
            current_marker = record.sdk_store_migration_error or ""
            active_claim = _parse_sdk_store_import_marker(current_marker)
            if active_claim is not None:
                old_token, old_expires_at = active_claim
                if old_expires_at > current:
                    raise SessionConflictError(f"Session {session_id} SDK transcript migration is already running")
                discard_staged_entries(db, run_id=old_token)
            elif current_marker.startswith("migration_running:"):
                raise SessionConflictError(f"Session {session_id} has an invalid SDK migration claim")

            claim = SdkStoreImportClaim(
                session_id=session_id,
                sdk_session_id=sdk_session_id,
                sdk_project_key=sdk_project_key,
                token=str(uuid.uuid4()),
                expires_at=expires_at,
            )
            record.sdk_project_key = sdk_project_key
            record.sdk_store_migration_error = claim.marker
            record.updated_at = current
            return claim

    def complete_sdk_store_import(
        self,
        *,
        claim: SdkStoreImportClaim,
        now: str | None = None,
    ) -> LocalSession:
        completed_at = now or utc_now()
        with self.Session.begin() as db:
            record = db.get(SessionRecordModel, claim.session_id)
            if (
                record is None
                or record.sdk_session_id != claim.sdk_session_id
                or record.sdk_project_key != claim.sdk_project_key
                or record.sdk_store_migration_error != claim.marker
                or record.active_run_id is not None
                or claim.expires_at <= completed_at
            ):
                raise SessionConflictError(f"Session {claim.session_id} SDK migration fence was lost")
            promoted = promote_staged_entries(db, run_id=claim.token, committed_at=completed_at)
            if promoted <= 0:
                raise SessionConflictError(f"Session {claim.session_id} SDK migration produced no transcript entries")
            record.sdk_store_ready_at = completed_at
            record.sdk_store_migration_error = None
            record.updated_at = completed_at
            return self._to_session(record)

    def fail_sdk_store_import(
        self,
        *,
        claim: SdkStoreImportClaim,
        error: str,
    ) -> bool:
        with self.Session.begin() as db:
            # A fenced/stale importer still owns only its token's staging rows. Always
            # discard those rows, while refusing to mutate a newer session claim.
            discard_staged_entries(db, run_id=claim.token)
            record = db.get(SessionRecordModel, claim.session_id)
            if record is None or record.sdk_store_migration_error != claim.marker:
                return False
            record.sdk_store_ready_at = None
            record.sdk_store_migration_error = error[:2000]
            record.updated_at = utc_now()
            return True

    def release_turn(self, session_id: str, *, run_id: str) -> bool:
        """Release an unfinished claim after an exception or client cancellation."""
        with self.Session.begin() as db:
            intent = db.get(SessionTurnIntentModel, run_id)
            if intent is not None and intent.status == "running":
                return False
            result = db.execute(
                update(SessionRecordModel)
                .where(
                    SessionRecordModel.session_id == session_id,
                    SessionRecordModel.active_run_id == run_id,
                )
                .values(
                    active_run_id=None,
                    active_run_expires_at=None,
                    active_run_generation=0,
                    updated_at=utc_now(),
                )
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
            .values(
                sdk_session_id=None,
                sdk_project_key=None,
                sdk_store_ready_at=None,
                sdk_store_migration_error=None,
                updated_at=next_updated_at,
            )
        )
        import_claim = _parse_sdk_store_import_marker(session.sdk_store_migration_error or "")
        with self.Session.begin() as db:
            result = db.execute(statement)
            if result.rowcount != 1:
                self._raise_conflict(db.get(SessionRecordModel, session.session_id), session, agent_id=agent_id)
            if import_claim is not None:
                discard_staged_entries(db, run_id=import_claim[0])
            record = db.get(SessionRecordModel, session.session_id)
            if record is None:  # pragma: no cover - guarded by rowcount
                raise SessionConflictError(f"Session {session.session_id} disappeared while invalidating SDK state")
            return self._to_session(record)

    def list(self) -> list[LocalSession]:
        with self.Session() as db:
            records = db.scalars(select(SessionRecordModel).order_by(SessionRecordModel.updated_at.desc())).all()
            return [self._to_session(record) for record in records]

    def delete(self, session_id: str) -> bool:
        reconcile_expired_turn_transactions(self.Session, session_id=session_id)
        with self.Session.begin() as db:
            record = db.get(SessionRecordModel, session_id)
            if not record:
                return False
            if record.active_run_id:
                raise SessionConflictError(f"Session {session_id} has an active turn and cannot be deleted")
            import_claim = _parse_sdk_store_import_marker(record.sdk_store_migration_error or "")
            if import_claim is not None:
                discard_staged_entries(db, run_id=import_claim[0])
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
                active_run_generation=0,
                sdk_project_key=None,
                sdk_store_ready_at=None,
                sdk_store_migration_error=None,
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
            active_run_generation=(record.active_run_generation if self._record_has_active_run(record) else 0),
            sdk_project_key=record.sdk_project_key,
            sdk_store_ready_at=record.sdk_store_ready_at,
            sdk_store_migration_error=record.sdk_store_migration_error,
        )


def _parse_sdk_store_import_marker(marker: str) -> tuple[str, str] | None:
    prefix = "migration_running:"
    if not marker.startswith(prefix):
        return None
    token, separator, expires_at = marker[len(prefix) :].partition(":")
    if not separator or not token or not expires_at:
        return None
    return token, expires_at
