#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict


class MetadataAudit(TypedDict):
    key: str
    value: str


class ReleaseAudit(TypedDict):
    commit_sha: str
    pr_number: int | None
    aid_identifiers: str | None
    status: str
    workflow_url: str | None
    workflow_run_id: int | None
    release_id: str | None
    discovered_at: str
    updated_at: str


class CommitLinkAudit(TypedDict):
    commit_sha: str
    pr_number: int
    aid_identifier: str
    merged_by: str
    resolved_at: str


class EventAudit(TypedDict):
    id: int
    commit_sha: str | None
    event_type: str
    created_at: str


class OutboxAudit(TypedDict):
    id: int
    dedupe_key: str
    kind: str
    status: str
    attempts: int
    created_at: str
    updated_at: str


class LegacyAuditSnapshot(TypedDict):
    schema_version: int
    retired_at: str
    source_state_present: bool
    metadata: list[MetadataAudit]
    releases: list[ReleaseAudit]
    commit_links: list[CommitLinkAudit]
    events: list[EventAudit]
    outbox: list[OutboxAudit]


_SNAPSHOT_KEYS = frozenset(LegacyAuditSnapshot.__required_keys__)
_SECTION_KEYS = (
    ("metadata", frozenset(MetadataAudit.__required_keys__)),
    ("releases", frozenset(ReleaseAudit.__required_keys__)),
    ("commit_links", frozenset(CommitLinkAudit.__required_keys__)),
    ("events", frozenset(EventAudit.__required_keys__)),
    ("outbox", frozenset(OutboxAudit.__required_keys__)),
)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)


def _metadata_audit(connection: sqlite3.Connection) -> list[MetadataAudit]:
    if not _table_exists(connection, "metadata"):
        return []
    rows = connection.execute("SELECT key, value FROM metadata ORDER BY key").fetchall()
    return [{"key": str(row[0]), "value": str(row[1])} for row in rows if str(row[0]).startswith(("active:", "cursor:"))]


def _release_audit(connection: sqlite3.Connection) -> list[ReleaseAudit]:
    if not _table_exists(connection, "releases"):
        return []
    rows = connection.execute(
        """
        SELECT commit_sha, pr_number, aid_identifiers, status, workflow_url,
               workflow_run_id, release_id, discovered_at, updated_at
        FROM releases
        ORDER BY discovered_at, commit_sha
        """
    ).fetchall()
    return [
        {
            "commit_sha": str(row[0]),
            "pr_number": _optional_int(row[1]),
            "aid_identifiers": _optional_str(row[2]),
            "status": str(row[3]),
            "workflow_url": _optional_str(row[4]),
            "workflow_run_id": _optional_int(row[5]),
            "release_id": _optional_str(row[6]),
            "discovered_at": str(row[7]),
            "updated_at": str(row[8]),
        }
        for row in rows
    ]


def _commit_link_audit(connection: sqlite3.Connection) -> list[CommitLinkAudit]:
    if not _table_exists(connection, "commit_links"):
        return []
    rows = connection.execute(
        """
        SELECT commit_sha, pr_number, aid_identifier, merged_by, resolved_at
        FROM commit_links
        ORDER BY resolved_at, commit_sha
        """
    ).fetchall()
    return [
        {
            "commit_sha": str(row[0]),
            "pr_number": int(row[1]),
            "aid_identifier": str(row[2]),
            "merged_by": str(row[3]),
            "resolved_at": str(row[4]),
        }
        for row in rows
    ]


def _event_audit(connection: sqlite3.Connection) -> list[EventAudit]:
    if not _table_exists(connection, "events"):
        return []
    rows = connection.execute("SELECT id, commit_sha, event_type, created_at FROM events ORDER BY id").fetchall()
    return [
        {
            "id": int(row[0]),
            "commit_sha": _optional_str(row[1]),
            "event_type": str(row[2]),
            "created_at": str(row[3]),
        }
        for row in rows
    ]


def _outbox_audit(connection: sqlite3.Connection) -> list[OutboxAudit]:
    if not _table_exists(connection, "outbox"):
        return []
    rows = connection.execute(
        """
        SELECT id, dedupe_key, kind, status, attempts, created_at, updated_at
        FROM outbox
        ORDER BY id
        """
    ).fetchall()
    return [
        {
            "id": int(row[0]),
            "dedupe_key": str(row[1]),
            "kind": str(row[2]),
            "status": str(row[3]),
            "attempts": int(row[4]),
            "created_at": str(row[5]),
            "updated_at": str(row[6]),
        }
        for row in rows
    ]


def build_snapshot(source: Path) -> LegacyAuditSnapshot:
    retired_at = datetime.now(timezone.utc).isoformat()
    if not source.is_file():
        return LegacyAuditSnapshot(
            schema_version=1,
            retired_at=retired_at,
            source_state_present=False,
            metadata=[],
            releases=[],
            commit_links=[],
            events=[],
            outbox=[],
        )

    source_uri = f"{source.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as connection:
        connection.execute("PRAGMA query_only = ON")
        return LegacyAuditSnapshot(
            schema_version=1,
            retired_at=retired_at,
            source_state_present=True,
            metadata=_metadata_audit(connection),
            releases=_release_audit(connection),
            commit_links=_commit_link_audit(connection),
            events=_event_audit(connection),
            outbox=_outbox_audit(connection),
        )


def _require_exact_keys(
    value: object,
    expected: frozenset[str],
    *,
    label: str,
) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{label} does not match the allowlisted audit schema")


def validate_snapshot(path: Path) -> None:
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"legacy audit snapshot is unreadable: {path}") from exc
    _require_exact_keys(payload, _SNAPSHOT_KEYS, label="snapshot")
    assert isinstance(payload, dict)
    if payload["schema_version"] != 1:
        raise ValueError("legacy audit snapshot schema_version must be 1")
    if not isinstance(payload["retired_at"], str) or not isinstance(
        payload["source_state_present"],
        bool,
    ):
        raise ValueError("legacy audit snapshot metadata has invalid types")

    for section_name, allowed_keys in _SECTION_KEYS:
        items = payload[section_name]
        if not isinstance(items, list):
            raise ValueError(f"legacy audit snapshot {section_name} must be a list")
        for index, item in enumerate(items):
            _require_exact_keys(
                item,
                allowed_keys,
                label=f"{section_name}[{index}]",
            )
            assert isinstance(item, dict)
            if any(isinstance(value, (dict, list)) for value in item.values()):
                raise ValueError(f"legacy audit snapshot {section_name}[{index}] contains a nested value")
            if section_name == "metadata":
                key = item["key"]
                if not isinstance(key, str) or not key.startswith(("active:", "cursor:")):
                    raise ValueError("legacy audit snapshot metadata contains a non-audit key")


def main() -> int:
    parser = argparse.ArgumentParser(description="Write a field-allowlisted audit snapshot of the retired release controller.")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--validate", type=Path)
    args = parser.parse_args()

    if args.validate is not None:
        if args.source is not None or args.output is not None:
            parser.error("--validate cannot be combined with --source or --output")
        try:
            validate_snapshot(args.validate)
        except ValueError as exc:
            parser.error(str(exc))
        return 0
    if args.source is None or args.output is None:
        parser.error("--source and --output are required when not validating")
    try:
        with args.output.open("x", encoding="utf-8") as output_handle:
            output_handle.write(json.dumps(build_snapshot(args.source), ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    except FileExistsError:
        parser.error("--output must not already exist")
    args.output.chmod(0o600)
    try:
        validate_snapshot(args.output)
    except ValueError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
