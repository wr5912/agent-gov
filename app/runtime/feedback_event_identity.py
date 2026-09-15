"""Feedback event idempotency identity and timestamp normalization."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import TypedDict, cast

from .feedback_entities import FeedbackEntities, parse_entities
from .json_types import JsonObject

_RFC3339_TIMESTAMP = re.compile(
    r"^(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})"
    r"[Tt](?P<time>[0-9]{2}:[0-9]{2}:[0-9]{2})"
    r"(?:\.(?P<fraction>[0-9]+))?"
    r"(?P<offset>[Zz]|[+-][0-9]{2}:[0-9]{2})$"
)


class FeedbackEventImmutableRequest(TypedDict):
    event_id: str
    source_system: str
    event_type: str
    timestamp: str
    run_id: str | None
    session_id: str | None
    entities: FeedbackEntities
    actor_id: str | None
    before: JsonObject | None
    after: JsonObject | None
    auto_captured: bool
    confidence: str | None
    requires_review: bool
    comment: str | None
    metadata: JsonObject


FEEDBACK_EVENT_IMMUTABLE_FIELDS = tuple(FeedbackEventImmutableRequest.__annotations__)


def normalize_feedback_event_timestamp(value: str) -> str:
    """Return one lossless UTC representation for a timezone-aware RFC 3339 instant."""
    match = _RFC3339_TIMESTAMP.fullmatch(value)
    if match is None or match.group("offset") == "-00:00":
        raise ValueError("timestamp must be an RFC 3339 date-time with an explicit UTC offset")

    offset = match.group("offset")
    if offset.lower() == "z":
        offset = "+00:00"
    try:
        instant = datetime.fromisoformat(f"{match.group('date')}T{match.group('time')}{offset}")
    except ValueError as exc:
        raise ValueError("timestamp must be a valid RFC 3339 date-time") from exc

    utc_instant = instant.astimezone(timezone.utc)
    fraction = (match.group("fraction") or "").rstrip("0")
    suffix = f".{fraction}" if fraction else ""
    return f"{utc_instant:%Y-%m-%dT%H:%M:%S}{suffix}Z"


def _canonical_feedback_event_request(
    payload: Mapping[str, object],
    *,
    allow_legacy_timestamp: bool,
) -> FeedbackEventImmutableRequest:
    timestamp = payload.get("timestamp")
    if not isinstance(timestamp, str):
        raise ValueError("timestamp must be a string")
    try:
        timestamp = normalize_feedback_event_timestamp(timestamp)
    except ValueError:
        if not allow_legacy_timestamp:
            raise
    entities = parse_entities(payload.get("entities"))
    return FeedbackEventImmutableRequest(
        event_id=cast(str, payload.get("event_id")),
        source_system=cast(str, payload.get("source_system")),
        event_type=cast(str, payload.get("event_type")),
        timestamp=timestamp,
        run_id=cast(str | None, payload.get("run_id")),
        session_id=cast(str | None, payload.get("session_id")),
        entities={kind: sorted(identifiers) for kind, identifiers in sorted(entities.items())},
        actor_id=cast(str | None, payload.get("actor_id")),
        before=cast(JsonObject | None, payload.get("before")),
        after=cast(JsonObject | None, payload.get("after")),
        auto_captured=cast(bool, payload.get("auto_captured")),
        confidence=cast(str | None, payload.get("confidence")),
        requires_review=cast(bool, payload.get("requires_review")),
        comment=cast(str | None, payload.get("comment")),
        metadata=cast(JsonObject, payload.get("metadata")),
    )


def _request_fingerprint(canonical: Mapping[str, object]) -> str:
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def feedback_event_request_fingerprint(payload: Mapping[str, object]) -> str:
    """Hash only caller-owned immutable input after semantic normalization."""
    return _request_fingerprint(_canonical_feedback_event_request(payload, allow_legacy_timestamp=False))


def persisted_feedback_event_request_fingerprint(payload: Mapping[str, object]) -> str:
    """Bind a pre-contract row without making later backend enrichment fail."""
    return _request_fingerprint(_canonical_feedback_event_request(payload, allow_legacy_timestamp=True))


def legacy_feedback_event_request_compatible(
    persisted: Mapping[str, object],
    incoming: Mapping[str, object],
) -> bool:
    """Allow one legacy row to bind only when the new request cannot contradict it.

    Older resolved rows did not retain whether ``session_id`` and ``entities`` came
    from the caller or correlation.  All other caller fields remained immutable;
    correlation fields therefore accept only an omitted value or a value already
    present on the row.  Once accepted, the caller's complete request fingerprint
    is persisted and every later retry follows the strict path.
    """
    stored = _canonical_feedback_event_request(persisted, allow_legacy_timestamp=True)
    requested = _canonical_feedback_event_request(incoming, allow_legacy_timestamp=False)
    for field in FEEDBACK_EVENT_IMMUTABLE_FIELDS:
        if field not in {"session_id", "entities"} and stored[field] != requested[field]:
            return False
    requested_session = requested["session_id"]
    if requested_session is not None and requested_session != stored["session_id"]:
        return False
    return all(set(identifiers).issubset(stored["entities"].get(kind, [])) for kind, identifiers in requested["entities"].items())
