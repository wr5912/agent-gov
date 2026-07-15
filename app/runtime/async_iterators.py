from __future__ import annotations

from collections.abc import Awaitable
from typing import Protocol, runtime_checkable


@runtime_checkable
class SupportsAsyncClose(Protocol):
    def aclose(self) -> Awaitable[None]: ...


async def close_async_iterator(source: object) -> None:
    """Close an async iterator when its implementation exposes ``aclose``."""
    if isinstance(source, SupportsAsyncClose):
        await source.aclose()
