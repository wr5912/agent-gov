from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from contextlib import suppress
from typing import TypeVar

from starlette.requests import ClientDisconnect, Request

_T = TypeVar("_T")
_DISCONNECT_POLL_INTERVAL_SECONDS = 0.05


async def run_while_request_connected(
    request: Request,
    operation: Awaitable[_T],
) -> _T:
    """Cancel and drain an owned operation when its non-stream HTTP client leaves."""
    operation_task = asyncio.create_task(operation)
    disconnect_task = asyncio.create_task(
        _wait_for_disconnect(request),
        name="http-client-disconnect-watch",
    )
    try:
        done, _ = await asyncio.wait(
            {operation_task, disconnect_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if operation_task in done:
            return await operation_task
        raise ClientDisconnect()
    finally:
        disconnect_task.cancel()
        disconnect_task.add_done_callback(_consume_task_outcome)
        if not operation_task.done():
            operation_task.cancel()
        with suppress(asyncio.CancelledError, ClientDisconnect):
            await operation_task


async def _wait_for_disconnect(request: Request) -> None:
    while not await request.is_disconnected():
        await asyncio.sleep(_DISCONNECT_POLL_INTERVAL_SECONDS)


def _consume_task_outcome(task: asyncio.Task[None]) -> None:
    with suppress(asyncio.CancelledError):
        task.exception()
