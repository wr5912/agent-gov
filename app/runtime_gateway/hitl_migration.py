"""One-time current-epoch migration for legacy HITL payload privacy."""

from __future__ import annotations

from typing import cast

from sqlalchemy import select, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker

from app.runtime.runtime_db_base import begin_sqlite_write_transaction, utc_now

from .contracts import RuntimeToolCallFingerprint
from .hitl import (
    HITLValidationError,
    parse_tool_call_fingerprint,
    tool_call_fingerprint,
)
from .models import RuntimePendingActionModel, RuntimeReceiptModel

HITL_FINGERPRINT_DATA_MIGRATION = "agentscope-hitl-fingerprint-v1"

_HITL_RECEIPT_KINDS = {
    "REQUIRE_USER_CONFIRM": "human",
    "REQUIRE_EXTERNAL_EXECUTION": "external",
}


def migrate_hitl_fingerprint_rows(session_factory: sessionmaker) -> bool:
    """Irreversibly reduce and physically purge legacy raw HITL rows once.

    The schema bootstrap lock serializes callers.  Logical reduction commits
    without a marker, then SQLite rewrites the current database image and
    truncates its WAL.  Only a successful physical purge permits the final,
    separate marker transaction.  Any crash before that marker is therefore
    safely retried, including a crash after the logical rows were transformed.
    """

    with session_factory() as db:
        if _migration_was_applied(db):
            return False

    _migrate_logical_rows(session_factory)
    _rewrite_and_checkpoint(session_factory)
    _record_migration_marker(session_factory)
    return True


def _migrate_logical_rows(session_factory: sessionmaker) -> None:
    with session_factory.begin() as db:
        begin_sqlite_write_transaction(db.connection())
        if _migration_was_applied(db):
            return
        _migrate_receipts(db)
        _migrate_pending_actions(db)


def _rewrite_and_checkpoint(session_factory: sessionmaker) -> None:
    engine = cast(Engine, session_factory.kw.get("bind"))
    if not isinstance(engine, Engine) or engine.dialect.name != "sqlite":
        raise RuntimeError(
            "HITL fingerprint migration requires a bound SQLite engine",
        )
    with engine.connect().execution_options(
        isolation_level="AUTOCOMMIT",
    ) as connection:
        connection.exec_driver_sql("PRAGMA secure_delete=ON")
        _truncate_wal(connection)
        connection.exec_driver_sql("VACUUM")
        _truncate_wal(connection)


def _truncate_wal(connection: Connection) -> None:
    result = connection.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)").one()
    busy, remaining_frames = (int(result[0]), int(result[1]))
    if busy != 0 or remaining_frames != 0:
        raise RuntimeError(
            "HITL fingerprint migration could not exclusively truncate SQLite WAL",
        )


def _record_migration_marker(session_factory: sessionmaker) -> None:
    with session_factory.begin() as db:
        begin_sqlite_write_transaction(db.connection())
        if _migration_was_applied(db):
            return
        db.execute(
            text(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (:version, :applied_at)",
            ),
            {
                "version": HITL_FINGERPRINT_DATA_MIGRATION,
                "applied_at": utc_now(),
            },
        )


def _migration_was_applied(db: Session) -> bool:
    return (
        db.execute(
            text(
                "SELECT version FROM schema_migrations WHERE version = :version",
            ),
            {"version": HITL_FINGERPRINT_DATA_MIGRATION},
        ).scalar_one_or_none()
        is not None
    )


def _migrate_receipts(db: Session) -> None:
    receipts = db.scalars(
        select(RuntimeReceiptModel).where(
            RuntimeReceiptModel.event_type.in_(tuple(_HITL_RECEIPT_KINDS)),
        ),
    ).all()
    for receipt in receipts:
        kind = _HITL_RECEIPT_KINDS[receipt.event_type]
        receipt.payload_json = _migrate_hitl_payload(
            receipt.payload_json,
            kind=kind,
        )


def _migrate_pending_actions(db: Session) -> None:
    actions = db.scalars(select(RuntimePendingActionModel)).all()
    for action in actions:
        fingerprint = _migrate_fingerprint(
            action.tool_call_json,
            kind=action.kind,
            expected_id=action.tool_call_id,
            expected_name=action.tool_call_name,
        )
        action.tool_call_json = fingerprint.model_dump(mode="json")


def _migrate_hitl_payload(payload: object, *, kind: str) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise HITLValidationError("Legacy HITL receipt payload must be an object")
    tool_calls = payload.get("tool_calls")
    if not isinstance(tool_calls, list) or not tool_calls:
        raise HITLValidationError("Legacy HITL receipt is missing tool_calls")
    return {
        "tool_calls": [_migrate_fingerprint(tool_call, kind=kind).model_dump(mode="json") for tool_call in tool_calls],
    }


def _migrate_fingerprint(
    value: object,
    *,
    kind: str,
    expected_id: str | None = None,
    expected_name: str | None = None,
) -> RuntimeToolCallFingerprint:
    if isinstance(value, dict) and "tool_call_id" in value:
        return parse_tool_call_fingerprint(
            value,
            expected_id=expected_id,
            expected_name=expected_name,
        )
    fingerprint = tool_call_fingerprint(
        value,
        default_state=_legacy_default_state(kind),
    )
    if expected_id is not None and fingerprint.tool_call_id != expected_id:
        raise HITLValidationError(
            "Persisted HITL tool call id does not match its ledger identity",
        )
    if expected_name is not None and fingerprint.tool_call_name != expected_name:
        raise HITLValidationError(
            "Persisted HITL tool name does not match its ledger identity",
        )
    return fingerprint


def _legacy_default_state(kind: str) -> str:
    if kind == "human":
        return "asking"
    if kind == "external":
        return "pending"
    raise HITLValidationError("Pending action kind is invalid")
