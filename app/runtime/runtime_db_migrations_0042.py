from __future__ import annotations

import json
from typing import cast

from pydantic.types import JsonValue
from sqlalchemy.engine import Connection

from .json_types import JsonObject
from .runtime_db_base import begin_sqlite_write_transaction, utc_now

RETIRED_AGENT_JOB_ERROR_CODE = "AGENT_JOB_QUEUE_RETIRED"
RETIRED_AGENT_JOB_STATES = (
    "created",
    "queued",
    "running",
    "schema_validating",
    "evidence_packaging",
)


def migrate_0042_retire_persisted_agent_job_queue(connection: Connection) -> None:
    """Close non-terminal legacy queue rows without deleting their evidence."""
    columns = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(agent_jobs)")}
    if not {"job_id", "status", "completed_at", "error_json"} <= columns:
        return

    begin_sqlite_write_transaction(connection)
    completed_at = utc_now()
    placeholders = ", ".join("?" for _ in RETIRED_AGENT_JOB_STATES)
    rows = connection.exec_driver_sql(
        f"SELECT job_id, error_json FROM agent_jobs WHERE status IN ({placeholders})",
        RETIRED_AGENT_JOB_STATES,
    ).fetchall()
    for job_id, raw_error in rows:
        error_json: JsonObject = {
            "error_code": RETIRED_AGENT_JOB_ERROR_CODE,
            "message": "持久化 Agent job 队列已退役；该未完成历史任务不会再执行。",
            "created_at": completed_at,
            "job_id": str(job_id),
        }
        previous_error = _decode_json(raw_error)
        if previous_error is not None:
            error_json["previous_error"] = previous_error
        connection.exec_driver_sql(
            """
            UPDATE agent_jobs
            SET status = 'failed', completed_at = ?, error_json = ?
            WHERE job_id = ? AND status IN (?, ?, ?, ?, ?)
            """,
            (
                completed_at,
                json.dumps(error_json, ensure_ascii=False, sort_keys=True),
                str(job_id),
                *RETIRED_AGENT_JOB_STATES,
            ),
        )


def _decode_json(value: object) -> JsonValue:
    if value is None:
        return None
    if not isinstance(value, str):
        return cast(JsonValue, value)
    try:
        return cast(JsonValue, json.loads(value))
    except json.JSONDecodeError:
        return {"unparsed_error": value}
