from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from sqlalchemy.engine import Connection

from .runtime_db_base import begin_sqlite_write_transaction, utc_now

LEGACY_EVALUATION_TABLES = (
    "eval_case_governance_events",
    "eval_case_revisions",
    "eval_cases",
    "scenario_packs",
    "archived_legacy_eval_run_items",
    "archived_legacy_eval_runs",
)
LEGACY_REGRESSION_ASSET_TYPE = "regression"


def migrate_0040_archive_and_remove_legacy_evaluation_chain(connection: Connection) -> None:
    """Archive raw legacy rows, then remove the superseded EvalCase chain atomically."""
    begin_sqlite_write_transaction(connection)
    _create_archive_table(connection)
    archived_at = utc_now()

    for table_name in LEGACY_EVALUATION_TABLES:
        if not _table_columns(connection, table_name):
            continue
        _archive_table_rows(
            connection,
            table_name=table_name,
            archived_at=archived_at,
            reason="replaced_by_typed_test_dataset_and_eval_run",
        )

    governance_asset_columns = _table_columns(connection, "governance_assets")
    if "asset_type" in governance_asset_columns:
        _archive_table_rows(
            connection,
            table_name="governance_assets",
            archived_at=archived_at,
            reason="replaced_by_typed_test_dataset",
            where_sql="asset_type = ?",
            parameters=(LEGACY_REGRESSION_ASSET_TYPE,),
        )
        connection.exec_driver_sql(
            "DELETE FROM governance_assets WHERE asset_type = ?",
            (LEGACY_REGRESSION_ASSET_TYPE,),
        )

    if _table_columns(connection, "agent_jobs"):
        _archive_table_rows(
            connection,
            table_name="agent_jobs",
            archived_at=archived_at,
            reason="legacy_eval_case_generation_job_removed",
            where_sql="job_type = ?",
            parameters=("eval_case_generation",),
        )
        connection.exec_driver_sql(
            "DELETE FROM agent_jobs WHERE job_type = ?",
            ("eval_case_generation",),
        )

    for table_name in LEGACY_EVALUATION_TABLES:
        connection.exec_driver_sql(f'DROP TABLE IF EXISTS "{table_name}"')


def _create_archive_table(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS archived_legacy_evaluation_rows (
            archive_id VARCHAR(128) NOT NULL PRIMARY KEY,
            source_table VARCHAR(128) NOT NULL,
            source_key VARCHAR(2048) NOT NULL,
            row_json JSON NOT NULL,
            archived_at VARCHAR(64) NOT NULL,
            reason VARCHAR(512) NOT NULL
        )
        """
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_archived_legacy_evaluation_rows_source ON archived_legacy_evaluation_rows (source_table, source_key)"
    )


def _archive_table_rows(
    connection: Connection,
    *,
    table_name: str,
    archived_at: str,
    reason: str,
    where_sql: str = "",
    parameters: tuple[object, ...] = (),
) -> None:
    columns = _table_columns(connection, table_name)
    if not columns:
        return
    primary_key_columns = _primary_key_columns(connection, table_name)
    predicate = f" WHERE {where_sql}" if where_sql else ""
    rows = connection.exec_driver_sql(
        f'SELECT * FROM "{table_name}"{predicate}',
        parameters,
    ).mappings()
    for row in rows:
        raw_row = {str(key): _json_safe(value) for key, value in row.items()}
        row_json = _canonical_json(raw_row)
        source_key_payload = (
            {column: raw_row.get(column) for column in primary_key_columns}
            if primary_key_columns
            else {"row_sha256": hashlib.sha256(row_json.encode("utf-8")).hexdigest()}
        )
        source_key = _canonical_json(source_key_payload)
        archive_id = "legacy-eval-" + hashlib.sha256(f"{table_name}\0{source_key}".encode()).hexdigest()
        connection.exec_driver_sql(
            """
            INSERT OR IGNORE INTO archived_legacy_evaluation_rows (
                archive_id, source_table, source_key, row_json, archived_at, reason
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (archive_id, table_name, source_key, row_json, archived_at, reason),
        )
        archived = connection.exec_driver_sql(
            "SELECT source_table, source_key, row_json FROM archived_legacy_evaluation_rows WHERE archive_id = ?",
            (archive_id,),
        ).one()
        if tuple(archived) != (table_name, source_key, row_json):
            raise RuntimeError(f"Legacy evaluation archive conflict: {table_name} {source_key}")


def _table_columns(connection: Connection, table_name: str) -> set[str]:
    return {str(row[1]) for row in connection.exec_driver_sql(f'PRAGMA table_info("{table_name}")').fetchall()}


def _primary_key_columns(connection: Connection, table_name: str) -> list[str]:
    rows = connection.exec_driver_sql(f'PRAGMA table_info("{table_name}")').fetchall()
    return [str(row[1]) for row in sorted(rows, key=lambda item: int(item[5])) if int(row[5]) > 0]


def _json_safe(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode("ascii")}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
