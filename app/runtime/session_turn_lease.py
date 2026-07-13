from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .session_store import LocalSessionStore


DEFAULT_SESSION_TURN_LEASE_SECONDS = 3600.0
DEFAULT_SESSION_TURN_HEARTBEAT_INTERVAL_SECONDS = 300.0


def turn_lease_expires_at(lease_seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()


class SessionTurnLeaseHeartbeat:
    """定期续租当前 turn；fencing 失败时取消 owner task 并 fail closed。"""

    def __init__(
        self,
        store: LocalSessionStore,
        *,
        session_id: str,
        run_id: str,
        run_generation: int | None = None,
        heartbeat_interval_seconds: float | None = None,
        lease_seconds: float | None = None,
    ) -> None:
        self._store = store
        self._session_id = session_id
        self._run_id = run_id
        self._run_generation = run_generation
        self._heartbeat_interval_seconds = DEFAULT_SESSION_TURN_HEARTBEAT_INTERVAL_SECONDS if heartbeat_interval_seconds is None else heartbeat_interval_seconds
        self._lease_seconds = DEFAULT_SESSION_TURN_LEASE_SECONDS if lease_seconds is None else lease_seconds
        if self._heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat_interval_seconds must be positive")
        if self._lease_seconds <= self._heartbeat_interval_seconds:
            raise ValueError("lease_seconds must be greater than heartbeat_interval_seconds")
        self._stopped = asyncio.Event()
        self._owner_task: asyncio.Task[object] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._failure: Exception | None = None

    async def __aenter__(self) -> SessionTurnLeaseHeartbeat:
        owner_task = asyncio.current_task()
        if owner_task is None:  # pragma: no cover - an async context always has a task in supported runtimes
            raise RuntimeError("Session turn heartbeat requires an owner task")
        self._owner_task = owner_task
        self._heartbeat_task = asyncio.create_task(
            self._run(),
            name=f"session-turn-heartbeat:{self._session_id}:{self._run_id}",
        )
        return self

    async def __aexit__(self, exc_type: object, exc: BaseException | None, traceback: object) -> bool:
        self.stop()
        if self._heartbeat_task is not None:
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                if self._failure is not None:
                    raise self._failure from exc
                raise
        if self._failure is not None:
            raise self._failure from exc
        return False

    def stop(self) -> None:
        self._stopped.set()

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.wait_for(
                    self._stopped.wait(),
                    timeout=self._heartbeat_interval_seconds,
                )
                return
            except TimeoutError:
                pass
            try:
                kwargs = {
                    "run_id": self._run_id,
                    "lease_seconds": self._lease_seconds,
                }
                if self._run_generation is not None:
                    kwargs["run_generation"] = self._run_generation
                self._store.renew_turn(self._session_id, **kwargs)
            except Exception as exc:
                self._failure = exc
                owner_task = self._owner_task
                if owner_task is not None and not owner_task.done():
                    owner_task.cancel()
                return
