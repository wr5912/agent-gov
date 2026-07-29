from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING

from .claude_runtime import RuntimeQueryState
from .session_turn_lease import SessionTurnLeaseHeartbeat

if TYPE_CHECKING:
    from .agent_profiles import AgentRuntimeProfile
    from .claude_runtime import ClaudeRuntime, RuntimeRequestContext
    from .schemas import ChatRequest, ChatResponse


async def run_managed_claude_runtime(
    runtime: ClaudeRuntime,
    req: ChatRequest,
    *,
    profile: AgentRuntimeProfile | None,
    agent_version_id_override: str | None,
) -> ChatResponse:
    selected_profile = await asyncio.to_thread(
        runtime._resolve_runtime_profile,
        req,
        profile,
    )
    context = await runtime._new_runtime_request_context(
        req,
        profile=selected_profile,
        agent_version_id_override=agent_version_id_override,
        agent_id=selected_profile.agent_id,
    )
    heartbeat = SessionTurnLeaseHeartbeat(
        runtime.session_store,
        session_id=context.session.session_id,
        run_id=context.run_id,
        run_generation=context.run_generation,
    )
    owner_task: asyncio.Task[ChatResponse] | None = None
    try:
        async with heartbeat:
            owner_task = asyncio.create_task(
                runtime._run_claimed(
                    req,
                    context=context,
                    profile=selected_profile,
                    heartbeat=heartbeat,
                ),
                name=f"runtime-non-stream-{context.run_id}",
            )

            async def cancel_owner() -> None:
                await _cancel_owner_task(owner_task)

            runtime.active_runs.register(
                run_id=context.run_id,
                session_id=context.session.session_id,
                owner_task=owner_task,
                cancel_callback=cancel_owner,
            )
            return await owner_task
    except asyncio.CancelledError as exc:
        await _cancel_owner_task(owner_task)
        await _abort_unfinalized(
            runtime,
            req,
            context,
            terminal_status="cancelled",
            error=exc,
        )
        raise
    except Exception as exc:
        await _cancel_owner_task(owner_task)
        await _abort_unfinalized(
            runtime,
            req,
            context,
            terminal_status="failed",
            error=exc,
        )
        raise


async def _cancel_owner_task(owner_task: asyncio.Task[ChatResponse] | None) -> None:
    if owner_task is None or owner_task.done():
        return
    owner_task.cancel()
    with suppress(asyncio.CancelledError):
        await owner_task


async def _abort_unfinalized(
    runtime: ClaudeRuntime,
    req: ChatRequest,
    context: RuntimeRequestContext,
    *,
    terminal_status: str,
    error: BaseException,
) -> None:
    if context.finalized:
        return
    state = RuntimeQueryState(sdk_session_id=context.session.sdk_session_id)
    await asyncio.to_thread(
        runtime._abort_runtime_request,
        req,
        context,
        state,
        terminal_status=terminal_status,
        error=error,
    )
