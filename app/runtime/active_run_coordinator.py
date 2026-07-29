from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

CancelCallback = Callable[[], Awaitable[None]]


class ActiveRunOwnerMissingError(LookupError):
    """Raised when a durable running turn has no owner in this process."""


@dataclass
class _ActiveRunHandle:
    run_id: str
    session_id: str
    owner_task: asyncio.Task[object]
    cancel_callback: CancelCallback
    completed: asyncio.Future[None]
    cancellation_task: asyncio.Task[None] | None = None


class ActiveRunCoordinator:
    """Process-local cancellation dispatch for durable Runtime turns.

    The coordinator deliberately does not expose a status store. SessionStore
    intents remain authoritative; this registry only owns live asyncio tasks.
    """

    def __init__(self) -> None:
        self._handles: dict[str, _ActiveRunHandle] = {}

    def register(
        self,
        *,
        run_id: str,
        session_id: str,
        owner_task: asyncio.Task[object],
        cancel_callback: CancelCallback,
    ) -> None:
        if run_id in self._handles:
            raise RuntimeError(f"Runtime run {run_id} already has a live owner")
        completed = asyncio.get_running_loop().create_future()
        handle = _ActiveRunHandle(
            run_id=run_id,
            session_id=session_id,
            owner_task=owner_task,
            cancel_callback=cancel_callback,
            completed=completed,
        )
        self._handles[run_id] = handle
        owner_task.add_done_callback(lambda task, registered=handle: self._owner_completed(registered, task))

    def has_owner(self, run_id: str) -> bool:
        return run_id in self._handles

    async def cancel_and_wait(self, run_id: str, *, timeout_seconds: float) -> None:
        handle = self._handles.get(run_id)
        if handle is None:
            raise ActiveRunOwnerMissingError(run_id)
        if handle.cancellation_task is None:
            handle.cancellation_task = asyncio.create_task(
                self._dispatch_cancel(handle),
                name=f"runtime-cancel-{run_id}",
            )
        async with asyncio.timeout(timeout_seconds):
            await asyncio.shield(handle.completed)

    async def cancel_all(self, *, timeout_seconds: float) -> list[str]:
        run_ids = list(self._handles)
        if not run_ids:
            return []
        results = await asyncio.gather(
            *(self.cancel_and_wait(run_id, timeout_seconds=timeout_seconds) for run_id in run_ids),
            return_exceptions=True,
        )
        incomplete: list[str] = []
        for run_id, result in zip(run_ids, results, strict=True):
            if isinstance(result, BaseException):
                incomplete.append(run_id)
        return incomplete

    async def _dispatch_cancel(self, handle: _ActiveRunHandle) -> None:
        try:
            await handle.cancel_callback()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "event=runtime.turn_cancel.dispatch_failed run_id=%s session_id=%s",
                handle.run_id,
                handle.session_id,
            )

    def _owner_completed(
        self,
        handle: _ActiveRunHandle,
        owner_task: asyncio.Task[object],
    ) -> None:
        current = self._handles.get(handle.run_id)
        if current is not handle:
            return
        self._handles.pop(handle.run_id, None)
        if not handle.completed.done():
            handle.completed.set_result(None)
        if owner_task.cancelled():
            return
        try:
            owner_task.exception()
        except asyncio.CancelledError:
            return
