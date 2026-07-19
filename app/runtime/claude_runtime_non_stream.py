from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from .agent_job_runner import AgentJobRunner
from .agent_profiles import read_requires_web_hitl
from .async_iterators import close_async_iterator
from .claude_runtime_permissions import non_stream_permission_callback
from .claude_sdk_interactive import query_with_interactive_client

if TYPE_CHECKING:
    from .agent_profiles import AgentRuntimeProfile
    from .claude_runtime import ClaudeRuntime, RuntimeQueryState, RuntimeRequestContext
    from .schemas import ChatRequest, ChatResponse
    from .session_turn_lease import SessionTurnLeaseHeartbeat


async def _execute_non_stream_query(
    runtime: ClaudeRuntime,
    req: ChatRequest,
    *,
    context: RuntimeRequestContext,
    profile: AgentRuntimeProfile,
    state: RuntimeQueryState,
) -> None:
    from claude_agent_sdk import ClaudeSDKClient, ResultMessage, query

    await asyncio.to_thread(runtime.model_provider_router.ensure_agent_runtime_ready)
    native_ask_configured = await asyncio.to_thread(read_requires_web_hitl, profile.workspace_dir)
    can_use_tool = non_stream_permission_callback() if native_ask_configured else None
    options = await asyncio.to_thread(
        runtime._build_options,
        req,
        context.session,
        context=context,
        profile=profile,
        can_use_tool=can_use_tool,
    )
    messages = (
        query_with_interactive_client(
            prompt=context.prompt,
            options=options,
            sdk_client_factory=ClaudeSDKClient,
        )
        if can_use_tool is not None
        else query(
            prompt=AgentJobRunner.single_prompt_stream(context.prompt),
            options=options,
        )
    )
    try:
        async for msg in messages:
            runtime._track_query_message(msg, state, ResultMessage)
    finally:
        await close_async_iterator(messages)
    runtime._ensure_query_terminal_error(state)


async def run_claimed_claude_runtime(
    runtime: ClaudeRuntime,
    req: ChatRequest,
    *,
    context: RuntimeRequestContext,
    profile: AgentRuntimeProfile,
    heartbeat: SessionTurnLeaseHeartbeat,
) -> ChatResponse:
    from .claude_runtime import RuntimeQueryState

    context.telemetry_input["claude_web_hitl_enabled"] = False
    if profile.requires_web_hitl:
        context.telemetry_input["permission_mode"] = "default"
    state = RuntimeQueryState(sdk_session_id=context.session.sdk_session_id)
    root_metadata = runtime._runtime_observation_metadata(context, "non_stream", profile=profile)
    propagation = runtime._langfuse_propagation_attributes(req, context, "non_stream", profile=profile)
    with runtime.langfuse.propagate_attributes(**propagation):
        with runtime.langfuse.start_observation(
            as_type="span",
            name=profile.langfuse_observation_name,
            input=context.telemetry_input,
            metadata=root_metadata,
        ) as root_span:
            context.langfuse_trace_id, context.langfuse_trace_url = runtime.langfuse.current_trace_ref()
            runtime.langfuse.set_trace_attributes(root_span, **propagation)
            with runtime.langfuse.start_observation(
                as_type="generation",
                name=f"{profile.langfuse_observation_name}.claude_sdk_query",
                input=runtime._generation_input(req, context),
                model=req.model or runtime.settings.agent_model,
                metadata=root_metadata,
            ) as generation:
                runtime.langfuse.set_trace_attributes(generation, **propagation)
                try:
                    await _execute_non_stream_query(
                        runtime,
                        req,
                        context=context,
                        profile=profile,
                        state=state,
                    )
                except asyncio.CancelledError as exc:
                    await asyncio.to_thread(
                        runtime._abort_runtime_request,
                        req,
                        context,
                        state,
                        terminal_status="cancelled",
                        error=exc,
                    )
                    raise
                except Exception as exc:
                    if not runtime._should_suppress_exception(exc, state.errors):
                        state.errors.append(f"{exc.__class__.__name__}: {exc}")

                answer, agent_activity, output = runtime._runtime_output_from_state(req, context, state)
                await asyncio.to_thread(
                    runtime._update_runtime_observations,
                    root_span,
                    generation,
                    context,
                    state,
                    output,
                    propagation,
                )
    await asyncio.to_thread(runtime._flush_langfuse)
    await asyncio.to_thread(runtime._sync_langfuse_trace, context, propagation, output)
    heartbeat.stop()
    if state.result_observed and not state.mirror_errors:
        await asyncio.to_thread(runtime._complete_runtime_request, req, context, state, answer, agent_activity)
    else:
        await asyncio.to_thread(
            runtime._abort_runtime_request,
            req,
            context,
            state,
            terminal_status="failed",
            error=state.mirror_errors[-1] if state.mirror_errors else state.errors[-1],
        )
    return runtime._run_response(context, state, answer, agent_activity)
