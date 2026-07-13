from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import TypeAlias

from sqlalchemy.engine import Connection

from .json_types import JsonObject
from .runtime_db_base import utc_now

logger = logging.getLogger(__name__)

_SOURCE_KINDS = ("signal", "soc_event", "pending_correlation")
_CASE_LIST_COLUMNS = (
    "source_ids_json",
    "signal_ids_json",
    "event_ids_json",
    "pending_correlation_ids_json",
    "run_ids_json",
    "session_ids_json",
    "alert_ids_json",
    "case_ids_json",
)
_CASE_COLUMNS = {
    "feedback_case_id",
    "agent_id",
    "created_at",
    "status",
    "current_evidence_package_id",
    "current_attribution_job_id",
    *_CASE_LIST_COLUMNS,
}

_SourceRef: TypeAlias = tuple[str, str]
_SourcePayload: TypeAlias = dict[str, str]
_SourceCache: TypeAlias = dict[_SourceRef, _SourcePayload]
_SqlRow: TypeAlias = dict[str, object]
_DirectPositions: TypeAlias = dict[_SourceRef, int]


@dataclass(frozen=True)
class _SourceClaim:
    source_kind: str
    source_id: str
    agent_id: str
    is_direct: bool
    direct_position: int | None

    @property
    def ref(self) -> tuple[str, str]:
        return self.source_kind, self.source_id


@dataclass
class _CasePlan:
    case_id: str
    agent_id: str
    created_at: str
    status: str
    current_evidence_package_id: str | None
    current_attribution_job_id: str | None
    original: dict[str, list[str]]
    claims: dict[_SourceRef, _SourceClaim] = field(default_factory=dict)
    source_payloads: dict[_SourceRef, _SourcePayload] = field(default_factory=dict)
    pending_events: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _CaseProjection:
    agent_id: str
    values: dict[str, list[str]]
    claims: tuple[_SourceClaim, ...]


@dataclass(frozen=True)
class _OrderedClaims:
    signals: tuple[_SourceClaim, ...]
    events: tuple[_SourceClaim, ...]
    pending_correlations: tuple[_SourceClaim, ...]


_ClaimWinners: TypeAlias = dict[_SourceRef, str]
_CaseProjections: TypeAlias = dict[str, _CaseProjection]
_Conflict: TypeAlias = tuple[_SourceClaim, _CasePlan, _CasePlan]


def migrate_feedback_case_sources(connection: Connection) -> None:
    _backfill_soc_event_agent_id(connection)
    _create_feedback_case_source_tables(connection)
    if not _table_columns(connection, "feedback_cases"):
        return
    plans = _load_case_plans(connection)
    winners, conflicts = _select_claim_winners(plans)
    projections = _build_case_projections(connection, plans, winners)
    _replace_claims_and_reconcile_cases(connection, plans, projections, conflicts)
    _verify_claim_postconditions(connection, projections)
    _warn_feedback_case_source_conflicts(connection)


def _backfill_soc_event_agent_id(connection: Connection) -> None:
    event_columns = _table_columns(connection, "soc_events")
    if event_columns and "agent_id" not in event_columns:
        connection.exec_driver_sql("ALTER TABLE soc_events ADD COLUMN agent_id VARCHAR(128)")
        event_columns.add("agent_id")
    if not event_columns:
        return
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_soc_events_agent_id ON soc_events (agent_id)")
    run_columns = _table_columns(connection, "agent_runs")
    run_locator_columns = [name for name in ("matched_run_id", "run_id") if name in event_columns]
    if "payload_json" in run_columns and run_locator_columns:
        locator = "COALESCE(" + ", ".join(f"soc_events.{name}" for name in run_locator_columns) + ")"
        connection.exec_driver_sql(
            "UPDATE soc_events SET agent_id = ("
            "SELECT json_extract(agent_runs.payload_json, '$.agent_id') FROM agent_runs "
            f"WHERE agent_runs.run_id = {locator}) WHERE agent_id IS NULL OR agent_id = ''"
        )
    if "payload_json" in event_columns:
        connection.exec_driver_sql(
            "UPDATE soc_events SET payload_json = json_set(COALESCE(payload_json, '{}'), '$.agent_id', agent_id) WHERE agent_id IS NOT NULL AND agent_id != ''"
        )


def _create_feedback_case_source_tables(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS feedback_case_sources (
            source_kind VARCHAR(32) NOT NULL,
            source_id VARCHAR(128) NOT NULL,
            case_id VARCHAR(128) NOT NULL
                REFERENCES feedback_cases(feedback_case_id) ON DELETE CASCADE,
            agent_id VARCHAR(128) NOT NULL,
            is_direct BOOLEAN NOT NULL DEFAULT 1,
            direct_position INTEGER,
            created_at VARCHAR(64) NOT NULL,
            PRIMARY KEY (source_kind, source_id)
        )
        """
    )
    claim_columns = _table_columns(connection, "feedback_case_sources")
    if "is_direct" not in claim_columns:
        connection.exec_driver_sql("ALTER TABLE feedback_case_sources ADD COLUMN is_direct BOOLEAN NOT NULL DEFAULT 1")
    if "direct_position" not in claim_columns:
        connection.exec_driver_sql("ALTER TABLE feedback_case_sources ADD COLUMN direct_position INTEGER")
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS feedback_case_source_conflicts (
            source_kind VARCHAR(32) NOT NULL,
            source_id VARCHAR(128) NOT NULL,
            retained_case_id VARCHAR(128) NOT NULL,
            conflicting_case_id VARCHAR(128) NOT NULL,
            retained_agent_id VARCHAR(128),
            conflicting_agent_id VARCHAR(128),
            detected_at VARCHAR(64) NOT NULL,
            PRIMARY KEY (source_kind, source_id, conflicting_case_id)
        )
        """
    )
    _create_indexes(connection, "feedback_case_sources", ("case_id", "agent_id", "created_at"))
    _create_indexes(connection, "feedback_case_source_conflicts", ("retained_case_id", "conflicting_case_id"))


def _load_case_plans(connection: Connection) -> list[_CasePlan]:
    missing = _CASE_COLUMNS - _table_columns(connection, "feedback_cases")
    if missing:
        raise RuntimeError(f"0036 cannot reconcile FeedbackCase schema; missing columns: {sorted(missing)}")
    columns = ["feedback_case_id", "agent_id", "created_at", "status", "current_evidence_package_id", "current_attribution_job_id", *_CASE_LIST_COLUMNS]
    rows = connection.exec_driver_sql(f"SELECT {', '.join(columns)} FROM feedback_cases ORDER BY created_at, feedback_case_id").mappings()
    cache: _SourceCache = {}
    return [_case_plan_from_row(connection, dict(row), cache) for row in rows]


def _case_plan_from_row(
    connection: Connection,
    row: Mapping[str, object],
    cache: _SourceCache,
) -> _CasePlan:
    case_id = _required_string(row.get("feedback_case_id"), "FeedbackCase id")
    original = {column: _strict_json_string_list(row.get(column), case_id=case_id, column=column) for column in _CASE_LIST_COLUMNS}
    plan = _CasePlan(
        case_id=case_id,
        agent_id=str(row.get("agent_id") or ""),
        created_at=str(row.get("created_at") or utc_now()),
        status=str(row.get("status") or ""),
        current_evidence_package_id=_optional_string(row.get("current_evidence_package_id")),
        current_attribution_job_id=_optional_string(row.get("current_attribution_job_id")),
        original=original,
    )
    typed_ids = {
        "signal": original["signal_ids_json"],
        "soc_event": original["event_ids_json"],
        "pending_correlation": original["pending_correlation_ids_json"],
    }
    if not any(typed_ids.values()):
        if original["source_ids_json"] or not _is_prior_conflict_loser(connection, case_id):
            raise RuntimeError(f"FeedbackCase {case_id} has no provable source claims")
        return plan
    direct_positions = _resolve_direct_positions(case_id, original["source_ids_json"], typed_ids)
    _populate_plan_claims(connection, plan, typed_ids, direct_positions, cache)
    owners = {claim.agent_id for claim in plan.claims.values() if claim.agent_id}
    if len(owners) != 1 or any(not claim.agent_id for claim in plan.claims.values()):
        raise RuntimeError(f"FeedbackCase {case_id} does not have one provable non-empty source owner")
    _require_public_registered_owner(connection, next(iter(owners)))
    return plan


def _resolve_direct_positions(
    case_id: str,
    source_ids: list[str],
    typed_ids: dict[str, list[str]],
) -> _DirectPositions:
    if len(source_ids) != len(set(source_ids)):
        raise RuntimeError(f"FeedbackCase {case_id} has duplicate direct source ids")
    positions: _DirectPositions = {}
    for position, source_id in enumerate(source_ids):
        candidates = [(kind, source_id) for kind in _SOURCE_KINDS if source_id in typed_ids[kind]]
        if len(candidates) != 1:
            raise RuntimeError(f"FeedbackCase {case_id} direct source id {source_id!r} is ambiguous or untyped")
        positions[candidates[0]] = position
    for kind in ("signal", "pending_correlation"):
        missing = [source_id for source_id in typed_ids[kind] if (kind, source_id) not in positions]
        if missing:
            raise RuntimeError(f"FeedbackCase {case_id} has non-direct {kind} sources: {missing}")
    return positions


def _populate_plan_claims(
    connection: Connection,
    plan: _CasePlan,
    typed_ids: dict[str, list[str]],
    direct_positions: _DirectPositions,
    cache: _SourceCache,
) -> None:
    for kind in ("signal", "pending_correlation"):
        for source_id in typed_ids[kind]:
            _add_plan_claim(connection, plan, kind, source_id, direct_positions, cache)
    linked_events = set(plan.pending_events.values())
    for event_id in typed_ids["soc_event"]:
        if ("soc_event", event_id) not in direct_positions and event_id not in linked_events:
            raise RuntimeError(f"FeedbackCase {plan.case_id} has an event that is neither direct nor linked from pending: {event_id}")
    event_ids = _unique_strings([*typed_ids["soc_event"], *plan.pending_events.values()])
    for event_id in event_ids:
        _add_plan_claim(connection, plan, "soc_event", event_id, direct_positions, cache)


def _add_plan_claim(
    connection: Connection,
    plan: _CasePlan,
    kind: str,
    source_id: str,
    direct_positions: _DirectPositions,
    cache: _SourceCache,
) -> None:
    ref = (kind, source_id)
    payload = _load_source_payload(connection, kind, source_id, cache)
    if kind == "pending_correlation":
        plan.pending_events[source_id] = payload["event_id"]
    plan.source_payloads[ref] = payload
    direct_position = direct_positions.get(ref)
    plan.claims[ref] = _SourceClaim(
        source_kind=kind,
        source_id=source_id,
        agent_id=payload["agent_id"],
        is_direct=direct_position is not None,
        direct_position=direct_position,
    )


def _select_claim_winners(
    plans: list[_CasePlan],
) -> tuple[_ClaimWinners, list[_Conflict]]:
    by_ref: dict[_SourceRef, list[_CasePlan]] = {}
    for plan in plans:
        for ref in plan.claims:
            by_ref.setdefault(ref, []).append(plan)
    winners: _ClaimWinners = {}
    conflicts: list[_Conflict] = []
    for ref, claimants in by_ref.items():
        retained = claimants[0]
        winners[ref] = retained.case_id
        for conflicting in claimants[1:]:
            conflicts.append((retained.claims[ref], retained, conflicting))
    for plan in plans:
        for pending_id, event_id in plan.pending_events.items():
            pending_winner = winners.get(("pending_correlation", pending_id))
            event_winner = winners.get(("soc_event", event_id))
            if pending_winner != event_winner:
                raise RuntimeError(f"FeedbackCase {plan.case_id} pending/event claim group would split: {pending_id}/{event_id}")
    return winners, conflicts


def _build_case_projections(
    connection: Connection,
    plans: list[_CasePlan],
    winners: _ClaimWinners,
) -> _CaseProjections:
    projections: _CaseProjections = {}
    for plan in plans:
        retained = tuple(claim for ref, claim in plan.claims.items() if winners.get(ref) == plan.case_id)
        projection = _project_case(plan, retained)
        if _projection_changes_case(plan, projection):
            dependencies = _case_dependencies(connection, plan)
            if dependencies:
                joined = ", ".join(dependencies)
                raise RuntimeError(f"FeedbackCase {plan.case_id} cannot be reconciled; downstream dependencies: {joined}")
        projections[plan.case_id] = projection
    return projections


def _project_case(plan: _CasePlan, retained: tuple[_SourceClaim, ...]) -> _CaseProjection:
    retained_direct = sorted(
        (claim for claim in retained if claim.is_direct),
        key=lambda claim: (claim.direct_position or 0, claim.source_kind, claim.source_id),
    )
    normalized_positions = {claim.ref: position for position, claim in enumerate(retained_direct)}
    retained = tuple(replace(claim, direct_position=normalized_positions[claim.ref]) if claim.is_direct else claim for claim in retained)
    by_kind = _order_claims_like_runtime(plan, retained)
    direct = sorted((claim for claim in retained if claim.is_direct), key=lambda claim: (claim.direct_position or 0, claim.source_kind, claim.source_id))
    signal_payloads = [plan.source_payloads[claim.ref] for claim in by_kind.signals]
    event_payloads = [plan.source_payloads[claim.ref] for claim in by_kind.events]
    pending_payloads = [plan.source_payloads[claim.ref] for claim in by_kind.pending_correlations]
    records = [*signal_payloads, *event_payloads, *pending_payloads]
    run_ids = [payload.get("run_id") or payload.get("matched_run_id") or "" for payload in signal_payloads]
    run_ids += [payload.get("run_id") or payload.get("matched_run_id") or "" for payload in event_payloads]
    run_ids += [payload.get("resolved_run_id") or "" for payload in pending_payloads]
    values = {
        "source_ids_json": _unique_strings([claim.source_id for claim in direct]),
        "signal_ids_json": [claim.source_id for claim in by_kind.signals],
        "event_ids_json": [claim.source_id for claim in by_kind.events],
        "pending_correlation_ids_json": [claim.source_id for claim in by_kind.pending_correlations],
        "run_ids_json": _unique_strings(run_ids),
        "session_ids_json": _unique_strings([payload.get("session_id", "") for payload in records]),
        "alert_ids_json": _unique_strings([payload.get("alert_id", "") for payload in records]),
        "case_ids_json": _unique_strings([payload.get("case_id", "") for payload in records]),
    }
    owners = {claim.agent_id for claim in retained}
    if retained and len(owners) != 1:
        raise RuntimeError(f"FeedbackCase {plan.case_id} retained claims do not have one owner")
    return _CaseProjection(agent_id=next(iter(owners)) if owners else plan.agent_id, values=values, claims=retained)


def _order_claims_like_runtime(
    plan: _CasePlan,
    claims: tuple[_SourceClaim, ...],
) -> _OrderedClaims:
    def direct_key(claim: _SourceClaim) -> tuple[int, str, str]:
        position = claim.direct_position if claim.direct_position is not None else 2**31
        return position, claim.source_kind, claim.source_id

    signals = tuple(sorted((claim for claim in claims if claim.source_kind == "signal"), key=direct_key))
    pending = tuple(sorted((claim for claim in claims if claim.source_kind == "pending_correlation"), key=direct_key))
    pending_positions = {plan.pending_events[claim.source_id]: claim.direct_position for claim in pending if claim.source_id in plan.pending_events}

    def event_key(claim: _SourceClaim) -> tuple[int, int, str]:
        if claim.is_direct and claim.direct_position is not None:
            return claim.direct_position, 0, claim.source_id
        return int(pending_positions.get(claim.source_id) or 0), 1, claim.source_id

    events = tuple(sorted((claim for claim in claims if claim.source_kind == "soc_event"), key=event_key))
    return _OrderedClaims(signals=signals, events=events, pending_correlations=pending)


def _projection_changes_case(plan: _CasePlan, projection: _CaseProjection) -> bool:
    return projection.agent_id != plan.agent_id or any(projection.values[column] != plan.original[column] for column in _CASE_LIST_COLUMNS)


def _case_dependencies(connection: Connection, plan: _CasePlan) -> list[str]:
    dependencies: list[str] = []
    if plan.status != "pending_evidence":
        dependencies.append(f"status={plan.status or '<empty>'}")
    if plan.current_evidence_package_id:
        dependencies.append("current_evidence_package")
    if plan.current_attribution_job_id:
        dependencies.append("current_attribution_job")
    checks = (
        ("evidence_packages", "feedback_case_id", "evidence_package"),
        ("improvement_feedback_case_assignments", "feedback_case_id", "improvement_assignment"),
        ("improvement_feedbacks", "case_id", "improvement_feedback"),
        ("eval_cases", "source_feedback_case_id", "legacy_eval_case"),
    )
    for table, column, label in checks:
        if _column_reference_exists(connection, table, column, plan.case_id):
            dependencies.append(label)
    if _agent_job_reference_exists(connection, plan.case_id):
        dependencies.append("agent_job")
    if _json_array_reference_exists(connection, "improvement_items", "source_feedback_refs_json", plan.case_id):
        dependencies.append("improvement_item")
    return dependencies


def _replace_claims_and_reconcile_cases(
    connection: Connection,
    plans: list[_CasePlan],
    projections: _CaseProjections,
    conflicts: list[_Conflict],
) -> None:
    connection.exec_driver_sql("DELETE FROM feedback_case_sources")
    for plan in plans:
        projection = projections[plan.case_id]
        for claim in projection.claims:
            connection.exec_driver_sql(
                "INSERT INTO feedback_case_sources "
                "(source_kind, source_id, case_id, agent_id, is_direct, direct_position, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    claim.source_kind,
                    claim.source_id,
                    plan.case_id,
                    claim.agent_id,
                    claim.is_direct,
                    claim.direct_position,
                    plan.created_at,
                ),
            )
        if _projection_changes_case(plan, projection):
            _update_case_projection(connection, plan.case_id, projection)
    for claim, retained, conflicting in conflicts:
        connection.exec_driver_sql(
            "INSERT INTO feedback_case_source_conflicts "
            "(source_kind, source_id, retained_case_id, conflicting_case_id, retained_agent_id, "
            "conflicting_agent_id, detected_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(source_kind, source_id, conflicting_case_id) DO UPDATE SET "
            "retained_case_id=excluded.retained_case_id, retained_agent_id=excluded.retained_agent_id, "
            "conflicting_agent_id=excluded.conflicting_agent_id, detected_at=excluded.detected_at",
            (
                claim.source_kind,
                claim.source_id,
                retained.case_id,
                conflicting.case_id,
                retained.agent_id or None,
                conflicting.agent_id or None,
                utc_now(),
            ),
        )


def _update_case_projection(connection: Connection, case_id: str, projection: _CaseProjection) -> None:
    assignments = ["agent_id = ?", *(f"{column} = ?" for column in _CASE_LIST_COLUMNS)]
    values: list[object] = [projection.agent_id]
    values.extend(json.dumps(projection.values[column], ensure_ascii=False) for column in _CASE_LIST_COLUMNS)
    values.append(case_id)
    connection.exec_driver_sql(
        f"UPDATE feedback_cases SET {', '.join(assignments)} WHERE feedback_case_id = ?",
        tuple(values),
    )


def _verify_claim_postconditions(connection: Connection, projections: _CaseProjections) -> None:
    expected = sum(len(projection.claims) for projection in projections.values())
    actual = int(connection.exec_driver_sql("SELECT COUNT(*) FROM feedback_case_sources").scalar_one())
    invalid = int(
        connection.exec_driver_sql(
            "SELECT COUNT(*) FROM feedback_case_sources WHERE agent_id IS NULL OR agent_id = '' "
            "OR (is_direct = 1 AND direct_position IS NULL) OR (is_direct = 0 AND direct_position IS NOT NULL)"
        ).scalar_one()
    )
    if actual != expected or invalid:
        raise RuntimeError("0036 FeedbackCase claim reconciliation failed its postconditions")


def _load_source_payload(
    connection: Connection,
    kind: str,
    source_id: str,
    cache: _SourceCache,
) -> _SourcePayload:
    ref = (kind, source_id)
    if ref in cache:
        return cache[ref]
    if kind == "signal":
        payload = _load_owned_source(connection, "feedback_signals", "signal_id", source_id)
    elif kind == "soc_event":
        payload = _load_owned_source(connection, "soc_events", "event_id", source_id)
    else:
        payload = _load_pending_source(connection, source_id, cache)
    cache[ref] = payload
    return payload


def _load_owned_source(connection: Connection, table: str, key_column: str, source_id: str) -> _SourcePayload:
    row = _dynamic_row(connection, table, key_column, source_id)
    if row is None:
        raise RuntimeError(f"FeedbackCase source does not exist: {table}:{source_id}")
    payload = _json_object(row.get("payload_json"))
    owner = _row_or_payload_string(row, payload, "agent_id")
    if not owner:
        raise RuntimeError(f"FeedbackCase source owner is empty: {table}:{source_id}")
    return {
        "agent_id": owner,
        "run_id": _row_or_payload_string(row, payload, "run_id"),
        "matched_run_id": _row_or_payload_string(row, payload, "matched_run_id"),
        "session_id": _row_or_payload_string(row, payload, "session_id"),
        "alert_id": _row_or_payload_string(row, payload, "alert_id"),
        "case_id": _row_or_payload_string(row, payload, "case_id"),
    }


def _load_pending_source(
    connection: Connection,
    pending_id: str,
    cache: _SourceCache,
) -> _SourcePayload:
    row = _dynamic_row(connection, "pending_correlations", "pending_id", pending_id)
    if row is None:
        raise RuntimeError(f"FeedbackCase pending source does not exist: {pending_id}")
    payload = _json_object(row.get("payload_json"))
    if _row_or_payload_string(row, payload, "status") != "resolved":
        raise RuntimeError(f"FeedbackCase pending source is not resolved: {pending_id}")
    event_id = _row_or_payload_string(row, payload, "event_id")
    if not event_id:
        raise RuntimeError(f"FeedbackCase pending source has no event: {pending_id}")
    event = _load_source_payload(connection, "soc_event", event_id, cache)
    resolved_run_id = _row_or_payload_string(row, payload, "resolved_run_id")
    if resolved_run_id:
        run_owner = _agent_run_owner(connection, resolved_run_id)
        if not run_owner or run_owner != event["agent_id"]:
            raise RuntimeError(f"FeedbackCase pending/event owners disagree: {pending_id}/{event_id}")
    return {
        "agent_id": event["agent_id"],
        "event_id": event_id,
        "resolved_run_id": resolved_run_id,
        "session_id": _row_or_payload_string(row, payload, "session_id"),
        "alert_id": _row_or_payload_string(row, payload, "alert_id"),
        "case_id": _row_or_payload_string(row, payload, "case_id"),
    }


def _agent_run_owner(connection: Connection, run_id: str) -> str:
    row = _dynamic_row(connection, "agent_runs", "run_id", run_id)
    if row is None:
        return ""
    payload = _json_object(row.get("payload_json"))
    return _row_or_payload_string(row, payload, "agent_id")


def _require_public_registered_owner(connection: Connection, agent_id: str) -> None:
    columns = _table_columns(connection, "agent_registry")
    if "agent_id" not in columns:
        raise RuntimeError("0036 cannot prove FeedbackCase source owners without agent_registry")
    predicates = ["agent_id = ?"]
    if "deleted_at" in columns:
        predicates.append("(deleted_at IS NULL OR deleted_at = '')")
    if "provision_state" in columns:
        predicates.append("(provision_state IS NULL OR provision_state = 'ready')")
    row = connection.exec_driver_sql(
        f"SELECT 1 FROM agent_registry WHERE {' AND '.join(predicates)} LIMIT 1",
        (agent_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"FeedbackCase source owner is not a registered business agent: {agent_id}")


def _dynamic_row(connection: Connection, table: str, key_column: str, key: str) -> _SqlRow | None:
    columns = _table_columns(connection, table)
    if key_column not in columns:
        return None
    wanted = [
        key_column,
        *(
            name
            for name in ("agent_id", "run_id", "matched_run_id", "session_id", "alert_id", "case_id", "event_id", "status", "payload_json")
            if name in columns and name != key_column
        ),
    ]
    row = (
        connection.exec_driver_sql(
            f"SELECT {', '.join(wanted)} FROM {table} WHERE {key_column} = ?",
            (key,),
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


def _is_prior_conflict_loser(connection: Connection, case_id: str) -> bool:
    row = connection.exec_driver_sql(
        "SELECT 1 FROM feedback_case_source_conflicts WHERE conflicting_case_id = ? LIMIT 1",
        (case_id,),
    ).fetchone()
    return row is not None


def _column_reference_exists(connection: Connection, table: str, column: str, value: str) -> bool:
    if column not in _table_columns(connection, table):
        return False
    return connection.exec_driver_sql(f"SELECT 1 FROM {table} WHERE {column} = ? LIMIT 1", (value,)).fetchone() is not None


def _agent_job_reference_exists(connection: Connection, case_id: str) -> bool:
    columns = _table_columns(connection, "agent_jobs")
    if {"scope_kind", "scope_id"}.issubset(columns):
        row = connection.exec_driver_sql(
            "SELECT 1 FROM agent_jobs WHERE scope_kind = 'feedback_case' AND scope_id = ? LIMIT 1",
            (case_id,),
        ).fetchone()
        if row is not None:
            return True
    if "input_json" in columns:
        row = connection.exec_driver_sql(
            "SELECT 1 FROM agent_jobs WHERE instr(CAST(input_json AS TEXT), json_quote(?)) > 0 LIMIT 1",
            (case_id,),
        ).fetchone()
        return row is not None
    return False


def _json_array_reference_exists(connection: Connection, table: str, column: str, value: str) -> bool:
    if column not in _table_columns(connection, table):
        return False
    row = connection.exec_driver_sql(
        f"SELECT 1 FROM {table} WHERE instr(CAST({column} AS TEXT), json_quote(?)) > 0 LIMIT 1",
        (value,),
    ).fetchone()
    return row is not None


def _strict_json_string_list(value: object, *, case_id: str, column: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise RuntimeError(f"FeedbackCase {case_id} has invalid {column}") from exc
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise RuntimeError(f"FeedbackCase {case_id} has invalid {column}")
    return list(value)


def _json_object(value: object) -> JsonObject:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return dict(value) if isinstance(value, dict) else {}


def _row_or_payload_string(row: _SqlRow, data: JsonObject, key: str) -> str:
    return str(row.get(key) or data.get(key) or "").strip()


def _required_string(value: object, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise RuntimeError(f"0036 cannot reconcile empty {label}")
    return normalized


def _optional_string(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _create_indexes(connection: Connection, table: str, columns: tuple[str, ...]) -> None:
    for column in columns:
        connection.exec_driver_sql(f"CREATE INDEX IF NOT EXISTS ix_{table}_{column} ON {table} ({column})")


def _table_columns(connection: Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()}


def _warn_feedback_case_source_conflicts(connection: Connection) -> None:
    count = int(connection.exec_driver_sql("SELECT COUNT(*) FROM feedback_case_source_conflicts").scalar_one())
    if count:
        logger.warning(
            "0036 FeedbackCase reconciliation retained deterministic claims, excluded safe losers, and recorded %s conflicts",
            count,
        )
