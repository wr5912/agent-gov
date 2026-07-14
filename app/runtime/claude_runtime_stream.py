from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .agent_job_runner import AgentJobRunner
from .agent_profiles import MAIN_AGENT_PROFILE, AgentRuntimeProfile
from .claude_runtime import RuntimeQueryState
from .claude_runtime_permissions import runtime_response_disposition
from .json_types import JsonObject
from .message_utils import to_plain
from .response_disposition_control import permission_denial_reason, response_disposition_fields
from .runtime_db import utc_now
from .schemas import ChatRequest

if TYPE_CHECKING:
    from .claude_runtime import ClaudeRuntime, RuntimeRequestContext


@dataclass
class StreamRun:
    runtime: ClaudeRuntime
    req: ChatRequest
    profile: AgentRuntimeProfile
    request_context: RuntimeRequestContext
    query_state: RuntimeQueryState
    web_hitl_enabled: bool
    root_metadata: JsonObject
    propagation: JsonObject
    final_output: JsonObject | None = None
    persisted: bool = False  # 幂等落库标志：ResultMessage 处先落库，finally 兜底，保证恰好一次


def _new_stream_run(runtime: ClaudeRuntime, req: ChatRequest, profile: AgentRuntimeProfile) -> StreamRun:
    context = runtime._new_runtime_request_context(req, agent_id=profile.name)
    web_hitl_enabled = bool(runtime.settings.enable_claude_web_hitl and runtime.user_input_service is not None)
    context.telemetry_input["claude_web_hitl_enabled"] = web_hitl_enabled
    context.telemetry_input["permission_mode"] = "default"
    return StreamRun(
        runtime=runtime,
        req=req,
        profile=profile,
        request_context=context,
        query_state=RuntimeQueryState(sdk_session_id=context.session.sdk_session_id),
        web_hitl_enabled=web_hitl_enabled,
        root_metadata=runtime._runtime_observation_metadata(context, "stream", profile=profile),
        propagation=runtime._langfuse_propagation_attributes(req, context, "stream", profile=profile),
    )


async def stream_claude_runtime(
    runtime: ClaudeRuntime,
    req: ChatRequest,
    *,
    profile: AgentRuntimeProfile | None = None,
) -> AsyncIterator[JsonObject]:
    from claude_agent_sdk import ResultMessage, query

    selected_profile = profile or runtime.profiles[MAIN_AGENT_PROFILE]
    stream_run = _new_stream_run(runtime, req, selected_profile)
    context = stream_run.request_context
    with runtime.langfuse.propagate_attributes(**stream_run.propagation):
        with runtime.langfuse.start_observation(
            as_type="span",
            name=selected_profile.langfuse_observation_name,
            input=context.telemetry_input,
            metadata=stream_run.root_metadata,
        ) as root_span:
            context.langfuse_trace_id, context.langfuse_trace_url = runtime.langfuse.current_trace_ref()
            runtime.langfuse.set_trace_attributes(root_span, **stream_run.propagation)
            yield runtime._stream_session_event(req, context)
            with runtime.langfuse.start_observation(
                as_type="generation",
                name=f"{selected_profile.langfuse_observation_name}.claude_sdk_query",
                input=runtime._generation_input(req, context),
                model=req.model or runtime.settings.agent_model,
                metadata=stream_run.root_metadata,
            ) as generation:
                runtime.langfuse.set_trace_attributes(generation, **stream_run.propagation)
                event_queue: asyncio.Queue[JsonObject | None] = asyncio.Queue()
                sdk_task = asyncio.create_task(_run_sdk_query(stream_run, event_queue, root_span, generation, query, ResultMessage))
                async for event in _drain_stream_queue(stream_run, event_queue, sdk_task):
                    yield event
    runtime._flush_langfuse()
    if stream_run.final_output is not None:
        runtime._sync_langfuse_trace(context, stream_run.propagation, stream_run.final_output)


def _sdk_tool_callback(stream_run: StreamRun, event_queue: asyncio.Queue[JsonObject | None]) -> Any:
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

    async def sdk_can_use_tool(tool_name: str, input_data: Any, sdk_context: Any) -> Any:
        response_disposition = runtime_response_disposition(stream_run.req)
        denial = permission_denial_reason(stream_run.profile.name, tool_name, response_disposition)
        if denial is not None:
            return PermissionResultDeny(message=denial)
        service = stream_run.runtime.user_input_service
        if service is None or not stream_run.web_hitl_enabled:
            message = f"工具 {tool_name} 请求人工审批，但 ENABLE_CLAUDE_WEB_HITL 未开启或 HITL 服务不可用；请求已显式拒绝。"
            await event_queue.put({"event": "error", "data": {"errors": [message]}})
            return PermissionResultDeny(message=message)
        decision = await service.create_and_wait(
            event_queue=event_queue,
            business_agent_id=stream_run.profile.name,
            run_id=stream_run.request_context.run_id,
            api_session_id=stream_run.request_context.session.session_id,
            sdk_session_id=stream_run.query_state.sdk_session_id,
            tool_name=tool_name,
            input_data=input_data,
            context=sdk_context,
            response_disposition=response_disposition,
        )
        if decision.action == "allow_once":
            if decision.updated_input is not None:
                return PermissionResultAllow(updated_input=decision.updated_input)
            return PermissionResultAllow()
        if decision.action == "answer_question":
            return PermissionResultAllow(updated_input=decision.updated_input or decision.ask_user_question_input or {})
        return PermissionResultDeny(message=decision.message or "User denied Claude tool request.")

    return sdk_can_use_tool


async def _emit_query_events(
    stream_run: StreamRun,
    event_queue: asyncio.Queue[JsonObject | None],
    query_func: Any,
    result_message_type: type,
) -> None:
    runtime = stream_run.runtime
    runtime.model_provider_router.ensure_agent_runtime_ready()
    can_use_tool = _sdk_tool_callback(stream_run, event_queue)
    options = runtime._build_options(
        stream_run.req,
        stream_run.request_context.session,
        profile=stream_run.profile,
        execution_mode="stream",
        can_use_tool=can_use_tool,
    )
    # 只有实际存在 Web HITL 决策面时才保持输入流；否则兼容先消费完 prompt 再产出结果的传输实现。
    input_done = asyncio.Event() if stream_run.web_hitl_enabled else None
    prompt_stream = (
        _single_prompt_stream_until_done(stream_run.request_context.prompt, input_done)
        if input_done is not None
        else AgentJobRunner.single_prompt_stream(stream_run.request_context.prompt)
    )
    try:
        async for msg in query_func(prompt=prompt_stream, options=options):
            event, text, plain, is_result, result_errors = runtime._track_query_message(
                msg,
                stream_run.query_state,
                result_message_type,
            )
            await event_queue.put({"event": "message", "data": {"event": event, "text": text, "raw": plain}})
            if is_result:
                if input_done is not None:
                    input_done.set()
                # 落库先于 result 事件（-> response.completed），使 items/retrieve 在完成信号时刻即可查。
                _persist_stream_run(stream_run)
                await _publish_result_event(stream_run, event_queue, result_errors)
    finally:
        if input_done is not None:
            input_done.set()


async def _single_prompt_stream_until_done(prompt: str, done: asyncio.Event) -> AsyncIterator[JsonObject]:
    async for item in AgentJobRunner.single_prompt_stream(prompt):
        yield item
    await done.wait()


async def _publish_result_event(
    stream_run: StreamRun,
    event_queue: asyncio.Queue[JsonObject | None],
    result_errors: list[str],
) -> None:
    runtime = stream_run.runtime
    context = stream_run.request_context
    state = stream_run.query_state
    agent_activity = runtime.activity_extractor.agent_activity_payload(stream_run.req, state.messages)
    data: JsonObject = {
        "session_id": context.session.session_id,
        "sdk_session_id": state.sdk_session_id,
        "run_id": context.run_id,
        "agent_version_id": context.agent_version_id,
        "langfuse_trace_id": context.langfuse_trace_id,
        "langfuse_trace_url": context.langfuse_trace_url,
        "alert_id": stream_run.req.alert_id,
        "case_id": stream_run.req.case_id,
        "agent_activity": agent_activity,
        "usage": to_plain(state.usage),
        "total_cost_usd": state.total_cost_usd,
        "stop_reason": state.stop_reason,
        "errors": result_errors,
    }
    data.update(response_disposition_fields(runtime_response_disposition(stream_run.req)))
    await event_queue.put(
        {
            "event": "result",
            "data": data,
        }
    )


async def _run_sdk_query(
    stream_run: StreamRun,
    event_queue: asyncio.Queue[JsonObject | None],
    root_span: Any,
    generation: Any,
    query_func: Any,
    result_message_type: type,
) -> None:
    try:
        try:
            await _emit_query_events(stream_run, event_queue, query_func, result_message_type)
        except Exception as exc:
            if not stream_run.runtime._should_retry_without_sdk_resume(exc, stream_run.request_context, stream_run.query_state):
                raise
            stream_run.runtime._clear_stale_sdk_session(stream_run.request_context)
            stream_run.query_state = RuntimeQueryState(sdk_session_id=None)
            await _emit_query_events(stream_run, event_queue, query_func, result_message_type)
    except Exception as exc:
        if not stream_run.runtime._should_suppress_exception(exc, stream_run.query_state.errors):
            stream_run.query_state.errors.append(f"{exc.__class__.__name__}: {exc}")
            await event_queue.put({"event": "error", "data": {"errors": stream_run.query_state.errors}})
    finally:
        try:
            await _complete_stream_run(stream_run, root_span, generation)
        except Exception as exc:
            if not stream_run.runtime._should_suppress_exception(exc, stream_run.query_state.errors):
                stream_run.query_state.errors.append(f"{exc.__class__.__name__}: {exc}")
                await event_queue.put({"event": "error", "data": {"errors": stream_run.query_state.errors}})
            if stream_run.runtime.user_input_service is not None:
                stream_run.runtime.user_input_service.clear_run_grants(stream_run.request_context.run_id)
        await event_queue.put({"event": "done", "data": "[DONE]"})
        await event_queue.put(None)


def _persist_stream_run(stream_run: StreamRun) -> None:
    """幂等落库：抽取 output + session save + record_run。

    在 ResultMessage 处（response.completed 之前）先调，使 items/retrieve 在完成信号时刻即可查；
    ``_complete_stream_run`` 的 finally 再兜底调（error/无 ResultMessage 路径）。
    ``stream_run.persisted`` 保证一次流式请求恰好落库一次（session save 同时置 sdk_session_id+agent_id，
    满足 items 的 owning-agent 强校验，不会出现 sdk_session_id 已置而 agent_id 为空的 500 中间态）。
    """
    if stream_run.persisted:
        return
    runtime = stream_run.runtime
    answer, agent_activity, output = runtime._runtime_output_from_state(
        stream_run.req,
        stream_run.request_context,
        stream_run.query_state,
    )
    stream_run.final_output = output
    runtime._complete_runtime_request(stream_run.req, stream_run.request_context, stream_run.query_state, answer, agent_activity)
    stream_run.persisted = True


async def _complete_stream_run(
    stream_run: StreamRun,
    root_span: Any,
    generation: Any,
) -> None:
    runtime = stream_run.runtime
    _persist_stream_run(stream_run)  # 幂等：is_result 已落则跳过；error/无 result 路径在此兜底
    runtime._update_runtime_observations(
        root_span,
        generation,
        stream_run.request_context,
        stream_run.query_state,
        stream_run.final_output or {},
        stream_run.propagation,
    )
    if runtime.user_input_service is not None:
        runtime.user_input_service.clear_run_grants(stream_run.request_context.run_id)


async def _drain_stream_queue(
    stream_run: StreamRun,
    event_queue: asyncio.Queue[JsonObject | None],
    sdk_task: asyncio.Task[None],
) -> AsyncIterator[JsonObject]:
    try:
        while True:
            try:
                item = await asyncio.wait_for(event_queue.get(), timeout=15)
            except asyncio.TimeoutError:
                if sdk_task.done() and event_queue.empty():
                    if not sdk_task.cancelled():
                        exc = sdk_task.exception()
                        if exc is not None:
                            yield {"event": "error", "data": {"errors": [f"{exc.__class__.__name__}: {exc}"]}}
                    break
                yield {"event": "heartbeat", "data": {"run_id": stream_run.request_context.run_id, "timestamp": utc_now()}}
                continue
            if item is None:
                break
            yield item
    finally:
        await _cancel_stream_task(stream_run, sdk_task)


async def _cancel_stream_task(stream_run: StreamRun, sdk_task: asyncio.Task[None]) -> None:
    if sdk_task.done():
        return
    service = stream_run.runtime.user_input_service
    if service is not None:
        await service.cancel_run(stream_run.request_context.run_id, decision="client_cancelled")
    sdk_task.cancel()
    with suppress(asyncio.CancelledError):
        await sdk_task
