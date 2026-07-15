from __future__ import annotations

from datetime import datetime, timedelta, timezone

DEFAULT_RUNTIME_OPERATION_LEASE_SECONDS = 15 * 60
RUNTIME_RECOVERY_INTERVAL_SECONDS = 60


def runtime_operation_heartbeat(*, now: str | None = None) -> str:
    return _parse_timestamp(now).isoformat() if now is not None else datetime.now(timezone.utc).isoformat()


def runtime_operation_is_stale(
    heartbeat_at: str | None,
    *,
    now: str | None = None,
    lease_seconds: int = DEFAULT_RUNTIME_OPERATION_LEASE_SECONDS,
) -> bool:
    if lease_seconds <= 0:
        raise ValueError("runtime operation lease_seconds must be positive")
    if not heartbeat_at:
        return True
    try:
        heartbeat = _parse_timestamp(heartbeat_at)
    except ValueError:
        return True
    current = _parse_timestamp(now) if now is not None else datetime.now(timezone.utc)
    return heartbeat <= current - timedelta(seconds=lease_seconds)


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("runtime operation timestamps must include a timezone offset")
    return parsed.astimezone(timezone.utc)
