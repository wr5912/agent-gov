from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Literal, cast

from pydantic import BaseModel

from .active_run_coordinator import (
    ActiveRunCoordinator,
    ActiveRunOwnerMissingError,
)
from .errors import FeedbackStoreError
from .runtime_db import SessionRecordModel, SessionTurnIntentModel
from .session_store import LocalSessionStore
from .state_machines import SESSION_TURN_INTENT_TERMINAL_STATES

logger = logging.getLogger(__name__)

RunTerminalStatus = Literal["succeeded", "failed", "cancelled", "interrupted"]
DEFAULT_RUN_CANCELLATION_TIMEOUT_SECONDS = 10.0
_TERMINAL_POLL_INTERVAL_SECONDS = 0.05


@dataclass(frozen=True)
class RunControlSnapshot:
    run_id: str
    session_id: str
    turn_status: str
    completed_at: str | None
    session_active_run_id: str | None

    @property
    def is_terminal(self) -> bool:
        return self.turn_status in SESSION_TURN_INTENT_TERMINAL_STATES

    @property
    def target_owns_session(self) -> bool:
        return self.session_active_run_id == self.run_id


class AgentRunCancelResponse(BaseModel):
    run_id: str
    session_id: str
    turn_status: RunTerminalStatus
    cancelled: bool
    completed_at: str | None = None
    session_active_run_id: str | None = None


class AgentRunNotFoundError(FeedbackStoreError):
    status_code = 404
    error_code = "AGENT_RUN_NOT_FOUND"


class AgentRunCancellationUnavailableError(FeedbackStoreError):
    status_code = 409
    error_code = "AGENT_RUN_CANCELLATION_UNAVAILABLE"


class AgentRunCancellationTimeoutError(FeedbackStoreError):
    status_code = 504
    error_code = "AGENT_RUN_CANCELLATION_TIMEOUT"


class RunCancellationService:
    def __init__(
        self,
        *,
        session_store: LocalSessionStore,
        coordinator: ActiveRunCoordinator,
        timeout_seconds: float = DEFAULT_RUN_CANCELLATION_TIMEOUT_SECONDS,
    ) -> None:
        self._session_store = session_store
        self._coordinator = coordinator
        self._timeout_seconds = timeout_seconds

    async def cancel(self, run_id: str) -> AgentRunCancelResponse:
        normalized = run_id.strip()
        started_at = time.monotonic()
        snapshot = await self._snapshot(normalized)
        if snapshot is None:
            raise AgentRunNotFoundError(f"Agent run not found: {normalized}")
        if snapshot.is_terminal:
            return self._terminal_response(snapshot)

        logger.info(
            "event=runtime.turn_cancel.requested run_id=%s session_id=%s",
            snapshot.run_id,
            snapshot.session_id,
        )
        try:
            await self._coordinator.cancel_and_wait(
                normalized,
                timeout_seconds=self._timeout_seconds,
            )
        except ActiveRunOwnerMissingError:
            latest = await self._snapshot(normalized)
            if latest is not None and latest.is_terminal:
                return self._terminal_response(latest)
            self._log_unavailable(snapshot, started_at)
            raise AgentRunCancellationUnavailableError(
                "The durable Agent run is still running but has no owner in this API process.",
                error_details={
                    "run_id": normalized,
                    "session_id": snapshot.session_id,
                    "retryable": True,
                },
            ) from None
        except TimeoutError:
            self._log_timeout(snapshot, started_at)
            raise self._timeout_error(snapshot) from None

        remaining = self._timeout_seconds - (time.monotonic() - started_at)
        latest = await self._wait_for_terminal(normalized, max(0.0, remaining))
        if latest is None:
            raise AgentRunNotFoundError(f"Agent run disappeared during cancellation: {normalized}")
        if not latest.is_terminal:
            self._log_timeout(latest, started_at)
            raise self._timeout_error(latest)
        response = self._terminal_response(latest)
        logger.info(
            "event=runtime.turn_cancel.completed run_id=%s session_id=%s turn_status=%s duration_ms=%s",
            latest.run_id,
            latest.session_id,
            latest.turn_status,
            round((time.monotonic() - started_at) * 1000),
        )
        return response

    async def _snapshot(self, run_id: str) -> RunControlSnapshot | None:
        return await asyncio.to_thread(
            read_run_control_snapshot,
            self._session_store,
            run_id,
        )

    async def _wait_for_terminal(
        self,
        run_id: str,
        timeout_seconds: float,
    ) -> RunControlSnapshot | None:
        deadline = time.monotonic() + timeout_seconds
        while True:
            snapshot = await self._snapshot(run_id)
            if snapshot is None or snapshot.is_terminal:
                return snapshot
            if time.monotonic() >= deadline:
                return snapshot
            await asyncio.sleep(min(_TERMINAL_POLL_INTERVAL_SECONDS, max(0.0, deadline - time.monotonic())))

    @staticmethod
    def _terminal_response(snapshot: RunControlSnapshot) -> AgentRunCancelResponse:
        if snapshot.target_owns_session:
            raise AgentRunCancellationUnavailableError(
                "The Agent run is terminal but still owns the session fence.",
                error_details={
                    "run_id": snapshot.run_id,
                    "session_id": snapshot.session_id,
                    "retryable": True,
                },
            )
        if snapshot.turn_status not in SESSION_TURN_INTENT_TERMINAL_STATES:
            raise AgentRunCancellationUnavailableError(
                f"Agent run has an unsupported terminal status: {snapshot.turn_status}",
                error_details={
                    "run_id": snapshot.run_id,
                    "session_id": snapshot.session_id,
                    "retryable": False,
                },
            )
        return AgentRunCancelResponse(
            run_id=snapshot.run_id,
            session_id=snapshot.session_id,
            turn_status=cast(RunTerminalStatus, snapshot.turn_status),
            cancelled=snapshot.turn_status == "cancelled",
            completed_at=snapshot.completed_at,
            session_active_run_id=snapshot.session_active_run_id,
        )

    @staticmethod
    def _timeout_error(snapshot: RunControlSnapshot) -> AgentRunCancellationTimeoutError:
        return AgentRunCancellationTimeoutError(
            "Agent run cancellation did not reach a durable terminal state before the timeout.",
            error_details={
                "run_id": snapshot.run_id,
                "session_id": snapshot.session_id,
                "retryable": True,
            },
        )

    @staticmethod
    def _log_timeout(snapshot: RunControlSnapshot, started_at: float) -> None:
        logger.warning(
            "event=runtime.turn_cancel.timeout run_id=%s session_id=%s duration_ms=%s",
            snapshot.run_id,
            snapshot.session_id,
            round((time.monotonic() - started_at) * 1000),
        )

    @staticmethod
    def _log_unavailable(snapshot: RunControlSnapshot, started_at: float) -> None:
        logger.error(
            "event=runtime.turn_cancel.unavailable run_id=%s session_id=%s duration_ms=%s",
            snapshot.run_id,
            snapshot.session_id,
            round((time.monotonic() - started_at) * 1000),
        )


def read_run_control_snapshot(
    session_store: LocalSessionStore,
    run_id: str,
) -> RunControlSnapshot | None:
    with session_store.Session() as db:
        intent = db.get(SessionTurnIntentModel, run_id)
        if intent is None:
            return None
        session = db.get(SessionRecordModel, intent.session_id)
        return RunControlSnapshot(
            run_id=intent.run_id,
            session_id=intent.session_id,
            turn_status=intent.status,
            completed_at=intent.completed_at,
            session_active_run_id=session.active_run_id if session is not None else None,
        )
