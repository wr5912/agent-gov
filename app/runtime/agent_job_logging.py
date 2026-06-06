from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .json_types import JsonObject


def log_agent_job_event(
    logger: logging.Logger,
    level: int,
    event: str,
    job: JsonObject | None,
    *,
    worker_instance: str | None = None,
    error_code: str | None = None,
    exc_info: bool = False,
) -> None:
    """Emit one stable, low-cardinality Agent job lifecycle log line."""

    payload = job if isinstance(job, dict) else {}
    logger.log(
        level,
        (
            "event=%s job_id=%s job_type=%s scope_kind=%s scope_id=%s "
            "profile_name=%s status=%s duration_ms=%s timeout_seconds=%s "
            "worker_instance=%s error_code=%s"
        ),
        event,
        _log_value(payload.get("job_id")),
        _log_value(payload.get("job_type")),
        _log_value(payload.get("scope_kind")),
        _log_value(payload.get("scope_id")),
        _log_value(payload.get("profile_name")),
        _log_value(payload.get("status")),
        _duration_ms(payload),
        _log_value(payload.get("timeout_seconds")),
        _log_value(worker_instance),
        _log_value(error_code or _error_code(payload)),
        exc_info=exc_info,
    )


def _duration_ms(job: JsonObject) -> str:
    start = _parse_datetime(job.get("started_at")) or _parse_datetime(job.get("created_at"))
    if not start:
        return "-"
    end = _parse_datetime(job.get("completed_at")) or datetime.now(timezone.utc)
    return str(max(0, int((end - start).total_seconds() * 1000)))


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _error_code(job: JsonObject) -> str | None:
    error_json = job.get("error_json")
    if isinstance(error_json, dict) and isinstance(error_json.get("error_code"), str):
        return error_json["error_code"]
    return None


def _log_value(value: Any) -> str:
    if value is None or value == "":
        return "-"
    return str(value).replace("\n", " ")
