from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from typing import Any

from .agent_job_runner import AgentJobRunner
from .async_iterators import close_async_iterator
from .json_types import JsonObject


async def query_with_interactive_client(
    *,
    prompt: str,
    options: Any,
    sdk_client_factory: Any,
) -> AsyncIterator[Any]:
    """Run one SDK turn with prompt EOF independent from control-channel EOF.

    ``query()`` couples exhaustion of its prompt iterable to stdin closure. A
    ``can_use_tool`` response uses that same stdin, so delaying prompt EOF until
    ``ResultMessage`` creates a cycle for consumers that produce results only
    after EOF. ``ClaudeSDKClient`` separates these lifetimes: its connection gets
    a control-only stream, while ``client.query`` consumes the finite user prompt.
    """

    control_done = asyncio.Event()

    async def control_lifetime() -> AsyncIterator[JsonObject]:
        await control_done.wait()
        if False:  # pragma: no cover - marks this as an empty async iterator
            yield {}

    client = sdk_client_factory(options=options)
    connected = False
    responses: AsyncIterator[Any] | None = None
    try:
        await client.connect(control_lifetime())
        connected = True
        await client.query(AgentJobRunner.single_prompt_stream(prompt))
        response_stream: AsyncIterator[Any] = client.receive_response()
        responses = response_stream
        async for msg in response_stream:
            yield msg
    finally:
        active_error = sys.exception()
        cleanup_error: BaseException | None = None
        control_done.set()
        if responses is not None:
            try:
                await close_async_iterator(responses)
            except BaseException as exc:
                cleanup_error = exc
        if connected:
            try:
                await client.disconnect()
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
        if active_error is None and cleanup_error is not None:
            raise cleanup_error
