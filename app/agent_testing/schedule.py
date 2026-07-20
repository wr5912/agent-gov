from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.runtime.agent_paths import InvalidAgentId, validate_agent_id
from app.runtime.json_types import JsonObject
from app.runtime.runtime_db_base import begin_sqlite_write_transaction, utc_now
from app.runtime.state_machines import AGENT_RUNNABLE_LIFECYCLE_STATES, validate_transition

from .models import AgentTestScheduleEventModel, AgentTestScheduleModel
from .service import AgentTestingError, AgentTestingService

DEFAULT_TEST_CRON = "0 2 * * *"
DEFAULT_TEST_TIMEZONE = "UTC"
MIN_TEST_SCHEDULE_INTERVAL_SECONDS = 15 * 60
SCHEDULER_POLL_SECONDS = 30
logger = logging.getLogger(__name__)


def next_schedule_time(cron_expression: str, timezone_name: str, *, after: datetime | None = None) -> datetime:
    expression = " ".join(cron_expression.split())
    if len(expression.split()) != 5:
        raise ValueError("cron_expression must use the five-field minute hour day month weekday format")
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown IANA timezone: {timezone_name}") from exc
    base = (after or datetime.now(timezone.utc)).astimezone(zone)
    try:
        first = croniter(expression, base).get_next(datetime)
    except Exception as exc:  # croniter exposes multiple parser-specific ValueError subclasses
        raise ValueError(f"Invalid cron_expression: {expression}") from exc
    if first.tzinfo is None:
        first = first.replace(tzinfo=zone)
    return first.astimezone(timezone.utc)


def validate_test_schedule(cron_expression: str, timezone_name: str, *, now: datetime | None = None) -> tuple[str, str, datetime]:
    expression = " ".join(cron_expression.split())
    clean_timezone = timezone_name.strip()
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    first = next_schedule_time(expression, clean_timezone, after=current)
    previous = first
    for _ in range(31):
        following = next_schedule_time(expression, clean_timezone, after=previous)
        if (following - previous).total_seconds() < MIN_TEST_SCHEDULE_INTERVAL_SECONDS:
            raise ValueError("Test schedules must be at least 15 minutes apart")
        previous = following
    return expression, clean_timezone, first


class AgentTestScheduleStore:
    def __init__(self, session_factory: sessionmaker) -> None:
        self.Session = session_factory

    def get_schedule(self, agent_id: str) -> JsonObject | None:
        with self.Session() as db:
            row = db.scalar(select(AgentTestScheduleModel).where(AgentTestScheduleModel.agent_id == agent_id))
            return _schedule_payload(row) if row else None

    def schedules_for_agents(self, agent_ids: list[str]) -> list[JsonObject]:
        ids = sorted(set(agent_ids))
        if not ids:
            return []
        with self.Session() as db:
            rows = list(db.scalars(select(AgentTestScheduleModel).where(AgentTestScheduleModel.agent_id.in_(ids))).all())
        return [_schedule_payload(row) for row in rows]

    def upsert_schedule(
        self,
        *,
        agent_id: str,
        enabled: bool,
        cron_expression: str,
        timezone_name: str,
        next_run_at: str | None,
    ) -> JsonObject:
        now = utc_now()
        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            row = db.scalar(select(AgentTestScheduleModel).where(AgentTestScheduleModel.agent_id == agent_id))
            if row is None:
                row = AgentTestScheduleModel(
                    schedule_id=f"atsc-{uuid.uuid4()}",
                    agent_id=agent_id,
                    enabled=enabled,
                    cron_expression=cron_expression,
                    timezone=timezone_name,
                    next_run_at=next_run_at,
                    created_at=now,
                    updated_at=now,
                )
                db.add(row)
            else:
                row.enabled = enabled
                row.cron_expression = cron_expression
                row.timezone = timezone_name
                row.next_run_at = next_run_at
                row.updated_at = now
        return self.get_schedule(agent_id) or {}

    def disable_schedule_for_agent(self, agent_id: str) -> bool:
        """停止后续触发，同时保留策略与历史事件供审计。"""

        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            row = db.scalar(select(AgentTestScheduleModel).where(AgentTestScheduleModel.agent_id == agent_id))
            if row is None:
                return False
            changed = bool(row.enabled or row.next_run_at is not None)
            row.enabled = False
            row.next_run_at = None
            row.updated_at = utc_now()
            return changed

    def claim_due_events(self, *, now: datetime | None = None) -> list[JsonObject]:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        current_iso = current.isoformat()
        created: list[JsonObject] = []
        with self.Session.begin() as db:
            begin_sqlite_write_transaction(db.connection())
            rows = list(
                db.scalars(
                    select(AgentTestScheduleModel)
                    .where(
                        AgentTestScheduleModel.enabled.is_(True),
                        AgentTestScheduleModel.next_run_at.is_not(None),
                        AgentTestScheduleModel.next_run_at <= current_iso,
                    )
                    .order_by(AgentTestScheduleModel.next_run_at, AgentTestScheduleModel.schedule_id)
                ).all()
            )
            for schedule in rows:
                scheduled_for = str(schedule.next_run_at)
                existing = db.scalar(
                    select(AgentTestScheduleEventModel).where(
                        AgentTestScheduleEventModel.schedule_id == schedule.schedule_id,
                        AgentTestScheduleEventModel.scheduled_for == scheduled_for,
                    )
                )
                if existing is None:
                    event = AgentTestScheduleEventModel(
                        schedule_event_id=f"atse-{uuid.uuid4()}",
                        schedule_id=schedule.schedule_id,
                        agent_id=schedule.agent_id,
                        scheduled_for=scheduled_for,
                        status="pending",
                        created_at=utc_now(),
                    )
                    db.add(event)
                    created.append(_event_payload(event))
                # A restart may miss many windows. Persist one occurrence, then jump to
                # the first future window instead of backfilling an unbounded queue.
                schedule.next_run_at = next_schedule_time(
                    schedule.cron_expression,
                    schedule.timezone,
                    after=current,
                ).isoformat()
                schedule.updated_at = utc_now()
        return created

    def pending_events(self, *, limit: int = 1000) -> list[JsonObject]:
        with self.Session() as db:
            rows = list(
                db.scalars(
                    select(AgentTestScheduleEventModel)
                    .where(AgentTestScheduleEventModel.status == "pending")
                    .order_by(AgentTestScheduleEventModel.scheduled_for, AgentTestScheduleEventModel.schedule_event_id)
                    .limit(limit)
                ).all()
            )
            return [_event_payload(row) for row in rows]

    def complete_event(
        self,
        schedule_event_id: str,
        *,
        status: str,
        resolved_commit_sha: str | None = None,
        test_run_id: str | None = None,
        detail: JsonObject | None = None,
    ) -> JsonObject:
        with self.Session.begin() as db:
            row = db.get(AgentTestScheduleEventModel, schedule_event_id)
            if row is None:
                raise LookupError(schedule_event_id)
            if row.status != "pending":
                return _event_payload(row)
            validate_transition("agent_test_schedule_event", row.status, status)
            row.status = status
            row.resolved_commit_sha = resolved_commit_sha
            row.test_run_id = test_run_id
            row.detail_json = detail or {}
            row.completed_at = utc_now()
        return self.get_event(schedule_event_id) or {}

    def get_event(self, schedule_event_id: str) -> JsonObject | None:
        with self.Session() as db:
            row = db.get(AgentTestScheduleEventModel, schedule_event_id)
            return _event_payload(row) if row else None

    def list_events(self, *, agent_id: str, limit: int = 100) -> list[JsonObject]:
        with self.Session() as db:
            rows = list(
                db.scalars(
                    select(AgentTestScheduleEventModel)
                    .where(AgentTestScheduleEventModel.agent_id == agent_id)
                    .order_by(AgentTestScheduleEventModel.created_at.desc(), AgentTestScheduleEventModel.schedule_event_id.desc())
                    .limit(limit)
                ).all()
            )
            return [_event_payload(row) for row in rows]


class AgentTestScheduleService:
    def __init__(
        self,
        *,
        store: AgentTestScheduleStore,
        testing: AgentTestingService,
        agent_exists: Callable[[str], bool],
        agent_status: Callable[[str], str | None],
        poll_seconds: int = SCHEDULER_POLL_SECONDS,
    ) -> None:
        self.store = store
        self.testing = testing
        self._agent_exists = agent_exists
        self._agent_status = agent_status
        self._poll_seconds = poll_seconds

    def read_schedule(self, agent_id: str) -> JsonObject:
        safe_agent_id = self._require_agent(agent_id)
        return self.store.get_schedule(safe_agent_id) or _default_schedule(safe_agent_id)

    def update_schedule(
        self,
        agent_id: str,
        *,
        enabled: bool,
        cron_expression: str,
        timezone_name: str,
        now: datetime | None = None,
    ) -> JsonObject:
        safe_agent_id = self._require_agent(agent_id)
        try:
            expression, clean_timezone, next_run = validate_test_schedule(cron_expression, timezone_name, now=now)
        except ValueError as exc:
            raise AgentTestingError(422, "AGENT_TEST_SCHEDULE_INVALID", str(exc)) from exc
        return self.store.upsert_schedule(
            agent_id=safe_agent_id,
            enabled=enabled,
            cron_expression=expression,
            timezone_name=clean_timezone,
            next_run_at=next_run.isoformat() if enabled else None,
        )

    def list_events(self, agent_id: str, *, limit: int) -> list[JsonObject]:
        safe_agent_id = self._require_agent(agent_id)
        return self.store.list_events(agent_id=safe_agent_id, limit=limit)

    def recover(self) -> JsonObject:
        pending = self.store.pending_events()
        for event in pending:
            self._process_event(event)
        due = self.tick()
        return {"pending_events": len(pending), "due_events": due}

    def tick(self, *, now: datetime | None = None) -> int:
        due_events = self.store.claim_due_events(now=now)
        # 每轮都继续排空持久化 pending，避免启动恢复单批上限之后的事件永久滞留。
        # 新到期事件即使排在旧 backlog 之后，也必须在本轮纳入处理。
        pending_by_id = {str(event["schedule_event_id"]): event for event in self.store.pending_events()}
        for event in due_events:
            pending_by_id.setdefault(str(event["schedule_event_id"]), event)
        for event in pending_by_id.values():
            self._process_event(event)
        return len(due_events)

    def disable_agent_schedule(self, agent_id: str) -> bool:
        """生命周期/删除编排调用：停用策略但保留历史审计。"""

        try:
            safe_agent_id = validate_agent_id(agent_id)
        except InvalidAgentId as exc:
            raise AgentTestingError(422, "AGENT_ID_INVALID", str(exc)) from exc
        return self.store.disable_schedule_for_agent(safe_agent_id)

    async def run_forever(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.tick)
            except Exception:
                logger.exception("Agent test scheduler tick failed")
            await asyncio.sleep(self._poll_seconds)

    def _process_event(self, event: JsonObject) -> None:
        event_id = str(event["schedule_event_id"])
        schedule_id = str(event["schedule_id"])
        scheduled_for = str(event["scheduled_for"])
        agent_id = str(event["agent_id"])
        existing = self.testing.store.run_for_schedule_occurrence(schedule_id=schedule_id, scheduled_for=scheduled_for)
        if existing is not None:
            self.store.complete_event(
                event_id,
                status="enqueued",
                resolved_commit_sha=str(existing["commit_sha"]),
                test_run_id=str(existing["test_run_id"]),
                detail={"recovered": True},
            )
            return
        configured = self.store.get_schedule(agent_id)
        if configured is None or configured.get("schedule_id") != schedule_id or not configured.get("enabled"):
            self.store.complete_event(
                event_id,
                status="skipped",
                detail={"error_code": "AGENT_TEST_SCHEDULE_DISABLED"},
            )
            return
        if not self._agent_exists(agent_id):
            self.store.disable_schedule_for_agent(agent_id)
            self.store.complete_event(
                event_id,
                status="skipped",
                detail={"error_code": "AGENT_TEST_SCHEDULE_AGENT_NOT_FOUND", "schedule_disabled": True},
            )
            return
        lifecycle = self._agent_status(agent_id)
        if lifecycle not in AGENT_RUNNABLE_LIFECYCLE_STATES:
            terminal = lifecycle == "archived"
            if terminal:
                self.store.disable_schedule_for_agent(agent_id)
            self.store.complete_event(
                event_id,
                status="skipped",
                detail={
                    "error_code": "AGENT_TEST_SCHEDULE_AGENT_NOT_RUNNABLE",
                    "agent_status": lifecycle,
                    "schedule_disabled": terminal,
                },
            )
            return
        self._trigger_event(
            event_id=event_id,
            schedule_id=schedule_id,
            scheduled_for=scheduled_for,
            agent_id=agent_id,
        )

    def _trigger_event(
        self,
        *,
        event_id: str,
        schedule_id: str,
        scheduled_for: str,
        agent_id: str,
    ) -> None:
        commit_sha: str | None = None
        try:
            commit_sha = self.testing.resolve_current_commit(agent_id)
            active = self.testing.store.active_run_for_agent_commit(agent_id=agent_id, commit_sha=commit_sha)
            if active is not None:
                self.store.complete_event(
                    event_id,
                    status="coalesced",
                    resolved_commit_sha=commit_sha,
                    test_run_id=str(active["test_run_id"]),
                    detail={"reason": "active_run_for_same_agent_commit"},
                )
                return
            run = self.testing.create_run(
                agent_id=agent_id,
                commit_sha=commit_sha,
                change_set_id=None,
                source="scheduled",
                schedule_id=schedule_id,
                scheduled_for=scheduled_for,
            )
        except AgentTestingError as exc:
            if exc.error_code == "AGENT_TEST_RUN_ALREADY_ACTIVE":
                active = self.testing.store.active_run_for_agent_commit(agent_id=agent_id, commit_sha=commit_sha) if commit_sha else None
                self.store.complete_event(
                    event_id,
                    status="coalesced",
                    resolved_commit_sha=commit_sha,
                    test_run_id=str(active["test_run_id"]) if active else None,
                    detail={"error_code": exc.error_code, "detail": exc.detail},
                )
                return
            skipped_codes = {
                "AGENT_NOT_FOUND",
                "AGENT_COMMIT_UNAVAILABLE",
                "AGENT_COMMIT_NOT_FOUND",
                "AGENT_TEST_SUITE_NOT_RUNNABLE",
            }
            self.store.complete_event(
                event_id,
                status="skipped" if exc.error_code in skipped_codes else "failed",
                resolved_commit_sha=commit_sha,
                detail={"error_code": exc.error_code, "detail": exc.detail},
            )
            return
        except Exception as exc:  # keep the background loop alive while preserving a durable failure
            self.store.complete_event(
                event_id,
                status="failed",
                resolved_commit_sha=commit_sha,
                detail={"error_code": "AGENT_TEST_SCHEDULE_TRIGGER_FAILED", "detail": str(exc)},
            )
            return
        self.store.complete_event(
            event_id,
            status="enqueued",
            resolved_commit_sha=commit_sha,
            test_run_id=str(run["test_run_id"]),
        )

    def _require_agent(self, agent_id: str) -> str:
        try:
            safe_agent_id = validate_agent_id(agent_id)
        except InvalidAgentId as exc:
            raise AgentTestingError(422, "AGENT_ID_INVALID", str(exc)) from exc
        if not self._agent_exists(safe_agent_id):
            raise AgentTestingError(404, "AGENT_NOT_FOUND", f"Business Agent not found: {safe_agent_id}")
        return safe_agent_id


def _default_schedule(agent_id: str) -> JsonObject:
    return {
        "schedule_id": None,
        "agent_id": agent_id,
        "enabled": False,
        "cron_expression": DEFAULT_TEST_CRON,
        "timezone": DEFAULT_TEST_TIMEZONE,
        "next_run_at": None,
        "created_at": None,
        "updated_at": None,
    }


def _schedule_payload(row: AgentTestScheduleModel) -> JsonObject:
    return {
        "schedule_id": row.schedule_id,
        "agent_id": row.agent_id,
        "enabled": bool(row.enabled),
        "cron_expression": row.cron_expression,
        "timezone": row.timezone,
        "next_run_at": row.next_run_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _event_payload(row: AgentTestScheduleEventModel) -> JsonObject:
    return {
        "schedule_event_id": row.schedule_event_id,
        "schedule_id": row.schedule_id,
        "agent_id": row.agent_id,
        "scheduled_for": row.scheduled_for,
        "status": row.status,
        "resolved_commit_sha": row.resolved_commit_sha,
        "test_run_id": row.test_run_id,
        "detail": dict(row.detail_json or {}),
        "created_at": row.created_at,
        "completed_at": row.completed_at,
    }
