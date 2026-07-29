from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import suppress
from typing import Any

from fastapi.responses import StreamingResponse

from .async_iterators import close_async_iterator

logger = logging.getLogger(__name__)


class ManagedStreamingResponse(StreamingResponse):
    """StreamingResponse that owns and explicitly closes its body iterator."""

    def __init__(
        self,
        content: AsyncIterator[Any],
        *,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
        media_type: str | None = None,
    ) -> None:
        self._owned_content = content
        self._cleanup_task: asyncio.Task[None] | None = None
        self._closed = False
        super().__init__(
            content,
            status_code=status_code,
            headers=dict(headers or {}),
            media_type=media_type,
        )

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            cleanup = self._start_cleanup()
            with suppress(asyncio.CancelledError):
                await asyncio.shield(cleanup)

    def _start_cleanup(self) -> asyncio.Task[None]:
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(
                self._close_owned_content(),
                name="managed-stream-response-close",
            )
            self._cleanup_task.add_done_callback(self._log_cleanup_failure)
        return self._cleanup_task

    async def _close_owned_content(self) -> None:
        if self._closed:
            return
        self._closed = True
        await close_async_iterator(self._owned_content)

    @staticmethod
    def _log_cleanup_failure(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            logger.error(
                "event=runtime.managed_stream_cleanup_failed error_type=%s",
                error.__class__.__name__,
                exc_info=error,
            )
