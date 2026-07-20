from __future__ import annotations

import uuid
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Iterable
from typing import Any

from sqlalchemy import and_, delete, exists, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased, sessionmaker

from app.runtime.json_types import JsonObject
from app.runtime.runtime_db_base import begin_sqlite_write_transaction, utc_now
from app.runtime.state_machines import validate_transition

from .models import AgentTestRunItemModel, AgentTestRunModel, AgentWorkspaceImportRecordModel


class AgentTestRunNotFound(LookupError):
    pass


class AgentTestRunAlreadyActive(RuntimeError):
    def __init__(self, test_run_id: str) -> None:
        super().__init__(test_run_id)
        self.test_run_id = test_run_id


class AgentTestingStore:
    def __init__(self, session_factory: sessionmaker) -> None:
        self.Session = session_factory

    def create_run(
        self,
        *,
        agent_id: str,
        commit_sha: str,
        change_set_id: str | None,
        source: str,
        command: list[str],
        suite: JsonObject,
        suite_digest: str | None,
        schedule_id: str | None = None,
        scheduled_for: str | None = None,
    ) -> JsonObject:
        now = utc_now()
        row = AgentTestRunModel(
            test_run_id=f"atr-{uuid.uuid4()}",
            agent_id=agent_id,
            commit_sha=commit_sha,
            change_set_id=change_set_id,
            schedule_id=schedule_id,
            scheduled_for=scheduled_for,
            source=source,
            status="queued",
            cancel_requested=False,
            created_at=now,
            suite_digest=suite_digest,
            command_json=command,
            suite_json=suite,
        )
        try:
            with self.Session.begin() as db:
                begin_sqlite_write_transaction(db.connection())
                active_stmt = _active_run_stmt(
                    agent_id=agent_id,
                    commit_sha=commit_sha,
                    change_set_id=change_set_id,
                    any_change_set=source == "scheduled",
                )
                active = db.scalar(active_stmt)
                if active is not None:
                    raise AgentTestRunAlreadyActive(active.test_run_id)
                db.add(row)
        except IntegrityError as exc:
            active = self.active_run_for_target(
                agent_id=agent_id,
                commit_sha=commit_sha,
                change_set_id=change_set_id,
            )
            if active is not None:
                raise AgentTestRunAlreadyActive(str(active["test_run_id"])) from exc
            raise
        return self.get_run(row.test_run_id) or {}

    def active_run_for_target(
        self,
        *,
        agent_id: str,
        commit_sha: str,
        change_set_id: str | None,
    ) -> JsonObject | None:
        stmt = _active_run_stmt(
            agent_id=agent_id,
            commit_sha=commit_sha,
            change_set_id=change_set_id,
            any_change_set=False,
        )
        with self.Session() as db:
            row = db.scalar(stmt)
            return _run_payload(row, ()) if row else None

    def active_run_for_agent_commit(self, *, agent_id: str, commit_sha: str) -> JsonObject | None:
        with self.Session() as db:
            row = db.scalar(
                select(AgentTestRunModel)
                .where(
                    AgentTestRunModel.agent_id == agent_id,
                    AgentTestRunModel.commit_sha == commit_sha,
                    AgentTestRunModel.status.in_(("queued", "running")),
                )
                .order_by(AgentTestRunModel.created_at.asc(), AgentTestRunModel.test_run_id.asc())
                .limit(1)
            )
            return _run_payload(row, ()) if row else None

    def run_for_schedule_occurrence(self, *, schedule_id: str, scheduled_for: str) -> JsonObject | None:
        with self.Session() as db:
            row = db.scalar(
                select(AgentTestRunModel)
                .where(
                    AgentTestRunModel.schedule_id == schedule_id,
                    AgentTestRunModel.scheduled_for == scheduled_for,
                )
                .order_by(AgentTestRunModel.created_at.asc(), AgentTestRunModel.test_run_id.asc())
                .limit(1)
            )
            return _run_payload(row, ()) if row else None

    def get_run(self, test_run_id: str) -> JsonObject | None:
        with self.Session() as db:
            row = db.get(AgentTestRunModel, test_run_id)
            if row is None:
                return None
            items = list(
                db.scalars(select(AgentTestRunItemModel).where(AgentTestRunItemModel.test_run_id == test_run_id).order_by(AgentTestRunItemModel.nodeid)).all()
            )
            return _run_payload(row, items)

    def list_runs(self, *, agent_id: str | None = None, change_set_id: str | None = None, limit: int = 100) -> list[JsonObject]:
        stmt = select(AgentTestRunModel).order_by(AgentTestRunModel.created_at.desc()).limit(limit)
        if agent_id:
            stmt = stmt.where(AgentTestRunModel.agent_id == agent_id)
        if change_set_id:
            stmt = stmt.where(AgentTestRunModel.change_set_id == change_set_id)
        with self.Session() as db:
            rows = list(db.scalars(stmt).all())
            return [_run_payload(row, ()) for row in rows]

    def list_run_history(
        self,
        *,
        agent_id: str | None = None,
        status: str | None = None,
        source: str | None = None,
        commit_sha: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> tuple[list[JsonObject], str | None]:
        stmt = select(AgentTestRunModel).order_by(AgentTestRunModel.created_at.desc(), AgentTestRunModel.test_run_id.desc())
        if agent_id:
            stmt = stmt.where(AgentTestRunModel.agent_id == agent_id)
        if status:
            stmt = stmt.where(AgentTestRunModel.status == status)
        if source:
            stmt = stmt.where(AgentTestRunModel.source == source)
        if commit_sha:
            stmt = stmt.where(AgentTestRunModel.commit_sha == commit_sha)
        if cursor:
            created_at, test_run_id = _decode_history_cursor(cursor)
            stmt = stmt.where(
                or_(
                    AgentTestRunModel.created_at < created_at,
                    and_(AgentTestRunModel.created_at == created_at, AgentTestRunModel.test_run_id < test_run_id),
                )
            )
        with self.Session() as db:
            rows = list(db.scalars(stmt.limit(limit + 1)).all())
        has_more = len(rows) > limit
        visible = rows[:limit]
        next_cursor = _encode_history_cursor(visible[-1].created_at, visible[-1].test_run_id) if has_more and visible else None
        return [_run_summary_payload(row) for row in visible], next_cursor

    def latest_run_summaries(self, agent_ids: Iterable[str]) -> list[JsonObject]:
        ids = sorted(set(agent_ids))
        if not ids:
            return []
        newer = aliased(AgentTestRunModel)
        newer_exists = exists(
            select(newer.test_run_id).where(
                newer.agent_id == AgentTestRunModel.agent_id,
                or_(
                    newer.created_at > AgentTestRunModel.created_at,
                    and_(
                        newer.created_at == AgentTestRunModel.created_at,
                        newer.test_run_id > AgentTestRunModel.test_run_id,
                    ),
                ),
            )
        )
        with self.Session() as db:
            rows = list(
                db.scalars(
                    select(AgentTestRunModel).where(
                        AgentTestRunModel.agent_id.in_(ids),
                        ~newer_exists,
                    )
                ).all()
            )
        return [_run_summary_payload(row) for row in rows]

    def claim_run(self, test_run_id: str) -> JsonObject | None:
        now = utc_now()
        validate_transition("agent_test_run", "queued", "running")
        with self.Session.begin() as db:
            claimed = db.execute(
                update(AgentTestRunModel)
                .where(AgentTestRunModel.test_run_id == test_run_id, AgentTestRunModel.status == "queued")
                .values(status="running", started_at=now)
            ).rowcount
        return self.get_run(test_run_id) if claimed else None

    def request_cancel(self, test_run_id: str) -> JsonObject:
        with self.Session.begin() as db:
            row = db.get(AgentTestRunModel, test_run_id)
            if row is None:
                raise AgentTestRunNotFound(test_run_id)
            if row.status == "queued":
                validate_transition("agent_test_run", row.status, "cancelled")
                row.status = "cancelled"
                row.cancel_requested = True
                row.completed_at = utc_now()
            elif row.status == "running":
                row.cancel_requested = True
        payload = self.get_run(test_run_id)
        if payload is None:  # pragma: no cover - row cannot disappear in one process
            raise AgentTestRunNotFound(test_run_id)
        return payload

    def cancel_requested(self, test_run_id: str) -> bool:
        with self.Session() as db:
            row = db.get(AgentTestRunModel, test_run_id)
            return bool(row and row.cancel_requested)

    def finish_run(
        self,
        test_run_id: str,
        *,
        status: str,
        report: JsonObject,
        items: Iterable[JsonObject],
        stdout: str,
        stderr: str,
        error: JsonObject | None = None,
    ) -> JsonObject:
        with self.Session.begin() as db:
            row = db.get(AgentTestRunModel, test_run_id)
            if row is None:
                raise AgentTestRunNotFound(test_run_id)
            if row.status != "running":
                return _run_payload(row, ())
            validate_transition("agent_test_run", row.status, status)
            row.status = status
            row.completed_at = utc_now()
            row.report_json = report
            row.stdout_text = stdout
            row.stderr_text = stderr
            row.error_json = error or {}
            db.execute(delete(AgentTestRunItemModel).where(AgentTestRunItemModel.test_run_id == test_run_id))
            for item in items:
                db.add(
                    AgentTestRunItemModel(
                        test_run_item_id=f"atri-{uuid.uuid4()}",
                        test_run_id=test_run_id,
                        nodeid=str(item.get("nodeid") or "unknown"),
                        outcome=str(item.get("outcome") or "unknown"),
                        phase=str(item.get("phase") or "call"),
                        duration_seconds=_optional_float(item.get("duration_seconds")),
                        detail=str(item["detail"]) if item.get("detail") is not None else None,
                    )
                )
        return self.get_run(test_run_id) or {}

    def reconcile_interrupted_runs(self) -> list[str]:
        now = utc_now()
        with self.Session.begin() as db:
            run_ids = [str(value) for value in db.scalars(select(AgentTestRunModel.test_run_id).where(AgentTestRunModel.status == "running")).all()]
            if run_ids:
                validate_transition("agent_test_run", "running", "interrupted")
                db.execute(
                    update(AgentTestRunModel)
                    .where(AgentTestRunModel.test_run_id.in_(run_ids))
                    .values(
                        status="interrupted",
                        completed_at=now,
                        error_json={"error_code": "AGENT_TEST_RUN_INTERRUPTED", "message": "API service restarted during pytest execution."},
                    )
                )
        return run_ids

    def queued_run_ids(self) -> list[str]:
        with self.Session() as db:
            return [
                str(value)
                for value in db.scalars(
                    select(AgentTestRunModel.test_run_id).where(AgentTestRunModel.status == "queued").order_by(AgentTestRunModel.created_at)
                ).all()
            ]

    def latest_passed_for_commit(self, *, agent_id: str, commit_sha: str) -> JsonObject | None:
        with self.Session() as db:
            row = db.scalar(
                select(AgentTestRunModel)
                .where(
                    AgentTestRunModel.agent_id == agent_id,
                    AgentTestRunModel.commit_sha == commit_sha,
                    AgentTestRunModel.status == "passed",
                )
                .order_by(AgentTestRunModel.completed_at.desc())
                .limit(1)
            )
            return _run_payload(row, ()) if row else None

    def record_import(
        self,
        *,
        agent_id: str,
        action: str,
        package_sha256: str,
        tree_sha256: str,
        commit_sha: str,
        suite: JsonObject,
    ) -> str:
        import_id = f"awi-{uuid.uuid4()}"
        raw_diagnostics = suite.get("diagnostics")
        diagnostics = raw_diagnostics if isinstance(raw_diagnostics, list) else []
        warnings = [item for item in diagnostics if isinstance(item, dict) and item.get("level") == "warning"]
        now = utc_now()
        with self.Session.begin() as db:
            db.add(
                AgentWorkspaceImportRecordModel(
                    import_id=import_id,
                    agent_id=agent_id,
                    action=action,
                    status="accepted",
                    package_sha256=package_sha256,
                    tree_sha256=tree_sha256,
                    commit_sha=commit_sha,
                    created_at=now,
                    completed_at=now,
                    suite_json=suite,
                    warnings_json=warnings,
                )
            )
        return import_id

    def record_import_failure(
        self,
        *,
        agent_id: str,
        action: str,
        package_sha256: str | None,
        tree_sha256: str | None,
        error: JsonObject,
    ) -> str:
        import_id = f"awi-{uuid.uuid4()}"
        now = utc_now()
        with self.Session.begin() as db:
            db.add(
                AgentWorkspaceImportRecordModel(
                    import_id=import_id,
                    agent_id=agent_id,
                    action=action,
                    status="failed",
                    package_sha256=package_sha256,
                    tree_sha256=tree_sha256,
                    commit_sha=None,
                    created_at=now,
                    completed_at=now,
                    suite_json={},
                    warnings_json=[],
                    error_json=error,
                )
            )
        return import_id


def _run_payload(row: AgentTestRunModel, items: Iterable[AgentTestRunItemModel]) -> JsonObject:
    report = dict(row.report_json or {})
    invocations = report.get("invocations")
    return {
        "test_run_id": row.test_run_id,
        "agent_id": row.agent_id,
        "commit_sha": row.commit_sha,
        "change_set_id": row.change_set_id,
        "schedule_id": row.schedule_id,
        "scheduled_for": row.scheduled_for,
        "source": row.source,
        "status": row.status,
        "cancel_requested": row.cancel_requested,
        "created_at": row.created_at,
        "started_at": row.started_at,
        "completed_at": row.completed_at,
        "duration_seconds": _optional_float(report.get("duration_seconds")),
        "exit_code": _optional_int(report.get("exit_code")),
        "suite_digest": row.suite_digest,
        "command": list(row.command_json or []),
        "suite": dict(row.suite_json or {}),
        "report": report,
        "items": [
            {
                "nodeid": item.nodeid,
                "outcome": item.outcome,
                "phase": item.phase,
                "duration_seconds": item.duration_seconds,
                "detail": item.detail,
            }
            for item in items
        ],
        "invocations": [dict(item) for item in invocations if isinstance(item, dict)] if isinstance(invocations, list) else [],
        "stdout": row.stdout_text or "",
        "stderr": row.stderr_text or "",
        "error": dict(row.error_json or {}),
    }


def _active_run_stmt(
    *,
    agent_id: str,
    commit_sha: str,
    change_set_id: str | None,
    any_change_set: bool,
) -> Any:
    stmt = (
        select(AgentTestRunModel)
        .where(
            AgentTestRunModel.agent_id == agent_id,
            AgentTestRunModel.commit_sha == commit_sha,
            AgentTestRunModel.status.in_(("queued", "running")),
        )
        .order_by(AgentTestRunModel.created_at.asc(), AgentTestRunModel.test_run_id.asc())
        .limit(1)
    )
    if any_change_set:
        return stmt
    if change_set_id is None:
        return stmt.where(AgentTestRunModel.change_set_id.is_(None))
    return stmt.where(AgentTestRunModel.change_set_id == change_set_id)


def _run_summary_payload(row: AgentTestRunModel) -> JsonObject:
    report = dict(row.report_json or {})
    return {
        "test_run_id": row.test_run_id,
        "agent_id": row.agent_id,
        "commit_sha": row.commit_sha,
        "change_set_id": row.change_set_id,
        "schedule_id": row.schedule_id,
        "scheduled_for": row.scheduled_for,
        "source": row.source,
        "status": row.status,
        "created_at": row.created_at,
        "started_at": row.started_at,
        "completed_at": row.completed_at,
        "duration_seconds": _optional_float(report.get("duration_seconds")),
        "exit_code": _optional_int(report.get("exit_code")),
        "suite_digest": row.suite_digest,
    }


def _encode_history_cursor(created_at: str, test_run_id: str) -> str:
    raw = f"{created_at}\0{test_run_id}".encode()
    return urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_history_cursor(cursor: str) -> tuple[str, str]:
    try:
        raw = urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        created_at, test_run_id = raw.split("\0", 1)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Invalid Agent test run history cursor") from exc
    if not created_at or not test_run_id:
        raise ValueError("Invalid Agent test run history cursor")
    return created_at, test_run_id


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
