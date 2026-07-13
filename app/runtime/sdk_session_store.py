from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal, cast

from claude_agent_sdk import SessionKey, SessionStoreEntry
from sqlalchemy import and_, distinct, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .errors import SessionConflictError
from .json_types import JsonObject
from .runtime_db import (
    SdkSessionEntryModel,
    SessionRecordModel,
    SessionTurnIntentModel,
    utc_now,
)

StoreMode = Literal["committed", "turn", "import"]


@dataclass(frozen=True)
class SessionStoreBinding:
    project_key: str
    sdk_session_id: str
    run_id: str | None = None
    allow_project_key_alias: bool = False


class SqliteSdkSessionStore:
    """Opaque Claude SDK transcript storage with per-run staging visibility."""

    def __init__(
        self,
        session_factory: Any,
        *,
        mode: StoreMode,
        binding: SessionStoreBinding | None = None,
    ) -> None:
        if mode != "committed" and (binding is None or binding.run_id is None):
            raise ValueError(f"{mode} session store requires a run binding")
        self.Session = session_factory
        self.mode = mode
        self.binding = binding

    @classmethod
    def committed(cls, session_factory: Any) -> SqliteSdkSessionStore:
        return cls(session_factory, mode="committed")

    @classmethod
    def for_committed_session(
        cls,
        session_factory: Any,
        *,
        project_key: str,
        sdk_session_id: str,
    ) -> SqliteSdkSessionStore:
        return cls(
            session_factory,
            mode="committed",
            binding=SessionStoreBinding(
                project_key=_required_key_part(project_key, "project_key"),
                sdk_session_id=_required_key_part(sdk_session_id, "session_id"),
                allow_project_key_alias=True,
            ),
        )

    @classmethod
    def for_turn(
        cls,
        session_factory: Any,
        *,
        project_key: str,
        sdk_session_id: str,
        run_id: str,
    ) -> SqliteSdkSessionStore:
        return cls(
            session_factory,
            mode="turn",
            binding=SessionStoreBinding(
                project_key=_required_key_part(project_key, "project_key"),
                sdk_session_id=_required_key_part(sdk_session_id, "session_id"),
                run_id=_required_key_part(run_id, "run_id"),
            ),
        )

    @classmethod
    def for_import(
        cls,
        session_factory: Any,
        *,
        project_key: str,
        sdk_session_id: str,
        import_id: str,
    ) -> SqliteSdkSessionStore:
        return cls(
            session_factory,
            mode="import",
            binding=SessionStoreBinding(
                project_key=_required_key_part(project_key, "project_key"),
                sdk_session_id=_required_key_part(sdk_session_id, "session_id"),
                run_id=_required_key_part(import_id, "import_id"),
            ),
        )

    async def append(self, key: SessionKey, entries: list[SessionStoreEntry]) -> None:
        project_key, sdk_session_id, subpath = self._normalize_key(key)
        if self.mode == "committed":
            raise PermissionError("Committed SDK session store is read-only")
        if not entries:
            return
        opaque_entries = [_opaque_entry(entry) for entry in entries]
        assert self.binding is not None and self.binding.run_id is not None
        with self.Session.begin() as db:
            if self.mode == "turn":
                self._assert_active_turn(db, self.binding.run_id)
            for entry in opaque_entries:
                self._append_entry(
                    db,
                    project_key=project_key,
                    sdk_session_id=sdk_session_id,
                    subpath=subpath,
                    entry=entry,
                    run_id=self.binding.run_id,
                )

    async def load(self, key: SessionKey) -> list[SessionStoreEntry] | None:
        project_key, sdk_session_id, subpath = self._normalize_key(key)
        visible = [
            SdkSessionEntryModel.project_key == project_key,
            SdkSessionEntryModel.sdk_session_id == sdk_session_id,
            SdkSessionEntryModel.subpath == subpath,
            SdkSessionEntryModel.discarded_at.is_(None),
        ]
        if self.mode != "committed":
            assert self.binding is not None and self.binding.run_id is not None
            visible.append(
                or_(
                    SdkSessionEntryModel.committed_at.is_not(None),
                    and_(
                        SdkSessionEntryModel.committed_at.is_(None),
                        SdkSessionEntryModel.origin_run_id == self.binding.run_id,
                    ),
                )
            )
        else:
            visible.append(SdkSessionEntryModel.committed_at.is_not(None))
        with self.Session() as db:
            records = db.scalars(select(SdkSessionEntryModel).where(*visible).order_by(SdkSessionEntryModel.entry_id)).all()
            if not records:
                return None
            return [cast(SessionStoreEntry, deepcopy(record.entry_json)) for record in records]

    async def list_subkeys(self, key: dict[str, str]) -> list[str]:
        project_key, sdk_session_id, _ = self._normalize_key(key, allow_subpath=False)
        visible = [
            SdkSessionEntryModel.project_key == project_key,
            SdkSessionEntryModel.sdk_session_id == sdk_session_id,
            SdkSessionEntryModel.subpath != "",
            SdkSessionEntryModel.discarded_at.is_(None),
        ]
        if self.mode == "committed":
            visible.append(SdkSessionEntryModel.committed_at.is_not(None))
        else:
            assert self.binding is not None and self.binding.run_id is not None
            visible.append(
                or_(
                    SdkSessionEntryModel.committed_at.is_not(None),
                    and_(
                        SdkSessionEntryModel.committed_at.is_(None),
                        SdkSessionEntryModel.origin_run_id == self.binding.run_id,
                    ),
                )
            )
        with self.Session() as db:
            return list(db.scalars(select(distinct(SdkSessionEntryModel.subpath)).where(*visible).order_by(SdkSessionEntryModel.subpath)).all())

    def _normalize_key(
        self,
        key: dict[str, str],
        *,
        allow_subpath: bool = True,
    ) -> tuple[str, str, str]:
        project_key = _required_key_part(key.get("project_key"), "project_key")
        sdk_session_id = _required_key_part(key.get("session_id"), "session_id")
        raw_subpath = key.get("subpath")
        if raw_subpath == "":
            raise ValueError("SessionStore subpath must be omitted for the main transcript")
        if raw_subpath is not None and not isinstance(raw_subpath, str):
            raise TypeError("SessionStore subpath must be a string")
        subpath = raw_subpath or ""
        if not allow_subpath and subpath:
            raise ValueError("list_subkeys key must not include subpath")
        if self.binding is not None:
            if sdk_session_id != self.binding.sdk_session_id:
                raise SessionConflictError("SDK session store key does not match its run binding")
            if project_key != self.binding.project_key:
                if not self.binding.allow_project_key_alias:
                    raise SessionConflictError("SDK session store key does not match its run binding")
                project_key = self.binding.project_key
        return project_key, sdk_session_id, subpath

    def _assert_active_turn(self, db: Session, run_id: str) -> None:
        intent = db.get(SessionTurnIntentModel, run_id)
        if intent is None or intent.status != "running":
            raise SessionConflictError(f"SDK turn intent {run_id} is not running")
        session = db.get(SessionRecordModel, intent.session_id)
        if session is None or session.active_run_id != run_id or not session.active_run_expires_at or session.active_run_expires_at <= utc_now():
            raise SessionConflictError(f"Session {intent.session_id} active turn is no longer owned by run {run_id}")

    @staticmethod
    def _append_entry(
        db: Session,
        *,
        project_key: str,
        sdk_session_id: str,
        subpath: str,
        entry: JsonObject,
        run_id: str,
    ) -> None:
        entry_uuid = entry.get("uuid")
        statement = sqlite_insert(SdkSessionEntryModel).values(
            project_key=project_key,
            sdk_session_id=sdk_session_id,
            subpath=subpath,
            entry_uuid=entry_uuid,
            entry_json=entry,
            origin_run_id=run_id,
            committed_at=None,
            discarded_at=None,
        )
        if entry_uuid is None:
            db.execute(statement)
            return
        result = db.execute(statement.on_conflict_do_nothing())
        if result.rowcount == 1:
            return
        existing = db.scalar(
            select(SdkSessionEntryModel).where(
                SdkSessionEntryModel.project_key == project_key,
                SdkSessionEntryModel.sdk_session_id == sdk_session_id,
                SdkSessionEntryModel.subpath == subpath,
                SdkSessionEntryModel.entry_uuid == entry_uuid,
                SdkSessionEntryModel.discarded_at.is_(None),
            )
        )
        if existing is None or existing.entry_json != entry:
            raise SessionConflictError(f"SDK transcript UUID {entry_uuid} was reused with different content")


def promote_staged_entries(db: Session, *, run_id: str, committed_at: str | None = None) -> int:
    result = db.execute(
        update(SdkSessionEntryModel)
        .where(
            SdkSessionEntryModel.origin_run_id == run_id,
            SdkSessionEntryModel.committed_at.is_(None),
            SdkSessionEntryModel.discarded_at.is_(None),
        )
        .values(committed_at=committed_at or utc_now())
    )
    return int(result.rowcount)


def discard_staged_entries(db: Session, *, run_id: str, discarded_at: str | None = None) -> int:
    result = db.execute(
        update(SdkSessionEntryModel)
        .where(
            SdkSessionEntryModel.origin_run_id == run_id,
            SdkSessionEntryModel.committed_at.is_(None),
            SdkSessionEntryModel.discarded_at.is_(None),
        )
        .values(discarded_at=discarded_at or utc_now())
    )
    return int(result.rowcount)


def _required_key_part(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"SessionStore {name} must be a non-empty string")
    return value


def _opaque_entry(entry: SessionStoreEntry) -> JsonObject:
    if not isinstance(entry, dict):
        raise TypeError("SessionStore entry must be a JSON object")
    entry_type = entry.get("type")
    if not isinstance(entry_type, str) or not entry_type:
        raise ValueError("SessionStore entry requires a non-empty type")
    entry_uuid = entry.get("uuid")
    if entry_uuid is not None and (not isinstance(entry_uuid, str) or not entry_uuid):
        raise ValueError("SessionStore entry uuid must be a non-empty string")
    try:
        normalized = json.loads(json.dumps(entry, ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise TypeError("SessionStore entry must be JSON serializable") from exc
    if not isinstance(normalized, dict):  # pragma: no cover - entry 已在上方限定为对象
        raise TypeError("SessionStore entry must serialize to a JSON object")
    return cast(JsonObject, normalized)
