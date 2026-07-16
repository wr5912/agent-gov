from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import claude_prompt_suggestions
from .agent_job_runner import AgentJobRunner
from .agent_profiles import MAIN_AGENT_PROFILE, AgentRuntimeProfile, read_requires_web_hitl
from .async_iterators import close_async_iterator
from .claude_runtime import RuntimeQueryState
from .claude_runtime_permissions import runtime_response_disposition
from .claude_sdk_interactive import query_with_interactive_client
from .json_types import JsonObject
from .message_utils import to_plain
from .response_disposition_control import permission_denial_reason, response_disposition_fields
from .runtime_db import utc_now
from .schemas import ChatRequest
from .session_turn_lease import SessionTurnLeaseHeartbeat

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
    persisted: bool = False  # 幂等落库标志：ResultMessage 处先落库，非取消终止时 finally 兜底
    persistence_attempted: bool = False
    finalized: bool = False  # 幂等收尾标志：ResultMessage 处即收尾，其余路径由 finally 兜底
    turn_heartbeat: SessionTurnLeaseHeartbeat | None = None


async def _new_stream_run(runtime: ClaudeRuntime, req: ChatRequest, profile: AgentRuntimeProfile) -> StreamRun:
    context = await runtime._new_runtime_request_context(req, profile=profile, agent_id=profile.agent_id)
    web_hitl_enabled = bool(runtime.settings.enable_claude_web_hitl and runtime.user_input_service is not None)
    context.telemetry_input["claude_web_hitl_enabled"] = web_hitl_enabled
    if profile.requires_web_hitl:
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
    selected_profile = profile or runtime.profiles[MAIN_AGENT_PROFILE]
    stream_run = await _new_stream_run(runtime, req, selected_profile)
    context = stream_run.request_context
    heartbeat = SessionTurnLeaseHeartbeat(
        runtime.session_store,
        session_id=context.session.session_id,
        run_id=context.run_id,
        run_generation=context.run_generation,
    )
    stream_run.turn_heartbeat = heartbeat
    async with heartbeat:
        source = _stream_claimed_run(stream_run)
        try:
            async for event in source:
                yield event
        finally:
            await close_async_iterator(source)


async def _stream_claimed_run(stream_run: StreamRun) -> AsyncIterator[JsonObject]:
    from claude_agent_sdk import ResultMessage

    runtime = stream_run.runtime
    req = stream_run.req
    selected_profile = stream_run.profile
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
                sdk_task = asyncio.create_task(
                    _run_sdk_query(
                        stream_run,
                        event_queue,
                        root_span,
                        generation,
                        claude_prompt_suggestions.query_with_prompt_suggestions,
                        claude_prompt_suggestions.PromptSuggestionClaudeClient,
                        ResultMessage,
                    )
                )
                source = _drain_stream_queue(stream_run, event_queue, sdk_task)
                try:
                    async for event in source:
                        yield event
                finally:
                    await close_async_iterator(source)
    await asyncio.to_thread(runtime._flush_langfuse)
    if stream_run.final_output is not None:
        await asyncio.to_thread(
            runtime._sync_langfuse_trace,
            context,
            stream_run.propagation,
            stream_run.final_output,
        )


def _sdk_tool_callback(stream_run: StreamRun, event_queue: asyncio.Queue[JsonObject | None]) -> Any:
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

    async def sdk_can_use_tool(tool_name: str, input_data: Any, sdk_context: Any) -> Any:
        response_disposition = runtime_response_disposition(stream_run.req)
        denial = permission_denial_reason(stream_run.profile.agent_id, tool_name, response_disposition)
        if denial is not None:
            return PermissionResultDeny(message=denial)
        service = stream_run.runtime.user_input_service
        if service is None or not stream_run.web_hitl_enabled:
            message = f"工具 {tool_name} 请求人工审批，但 ENABLE_CLAUDE_WEB_HITL 未开启或 HITL 服务不可用；请求已显式拒绝。"
            await event_queue.put({"event": "error", "data": {"errors": [message]}})
            return PermissionResultDeny(message=message)
        decision = await service.create_and_wait(
            event_queue=event_queue,
            business_agent_id=stream_run.profile.agent_id,
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
    sdk_client_factory: Any,
    result_message_type: type,
    finalize: Callable[[], Awaitable[None]],
) -> None:
    runtime = stream_run.runtime
    await asyncio.to_thread(runtime.model_provider_router.ensure_agent_runtime_ready)
    native_ask_configured = await asyncio.to_thread(read_requires_web_hitl, stream_run.profile.workspace_dir)
    can_use_tool = _sdk_tool_callback(stream_run, event_queue) if native_ask_configured else None
    options = await asyncio.to_thread(
        runtime._build_options,
        stream_run.req,
        stream_run.request_context.session,
        context=stream_run.request_context,
        profile=stream_run.profile,
        can_use_tool=can_use_tool,
    )
    messages = (
        query_with_interactive_client(
            prompt=stream_run.request_context.prompt,
            options=options,
            sdk_client_factory=sdk_client_factory,
        )
        if can_use_tool is not None
        else query_func(
            prompt=AgentJobRunner.single_prompt_stream(stream_run.request_context.prompt),
            options=options,
        )
    )
    try:
        async for msg in messages:
            if isinstance(msg, claude_prompt_suggestions.PromptSuggestionMessage):
                await event_queue.put(
                    {
                        "event": "prompt_suggestion",
                        "data": {
                            "suggestion": msg.suggestion,
                            "run_id": stream_run.request_context.run_id,
                            "session_id": stream_run.request_context.session.session_id,
                        },
                    }
                )
                continue
            event, text, plain, is_result, result_errors = runtime._track_query_message(
                msg,
                stream_run.query_state,
                result_message_type,
            )
            await event_queue.put({"event": "message", "data": {"event": event, "text": text, "raw": plain}})
            if is_result:
                if stream_run.query_state.mirror_errors:
                    await asyncio.to_thread(
                        _abort_stream_run,
                        stream_run,
                        terminal_status="failed",
                        error=stream_run.query_state.mirror_errors[-1],
                    )
                    await event_queue.put(
                        {
                            "event": "error",
                            "data": {"errors": list(stream_run.query_state.errors)},
                        }
                    )
                    continue
                # 落库先于 result 事件（-> response.completed），使 items/retrieve 在完成信号时刻即可查。
                await asyncio.to_thread(_persist_stream_run, stream_run)
                await _publish_result_event(stream_run, event_queue, result_errors)
                # 答案已完成，立刻收尾：Prompt Suggestion 是可选增强，不得把终态扣在手里。
                # 交互模式下 CLI 进程还活着、输出流不关闭，若在这里等它的尾随窗口，
                # 没有建议时每一轮都白等满 3 秒——「停止」按钮挂着、发不出下一句。
                # 建议若稍后到达，仍会作为迟到帧从这条尚未关闭的流送出（见下方 async for 继续消费，
                # 且 Responses 投影层已把 prompt_suggestion 豁免于 done 守卫）。
                await finalize()
    finally:
        await close_async_iterator(messages)


async def _publish_result_event(
    stream_run: StreamRun,
    event_queue: asyncio.Queue[JsonObject | None],
    result_errors: list[str],
) -> None:
    runtime = stream_run.runtime
    context = stream_run.request_context
    state = stream_run.query_state
    agent_activity = runtime.activity_extractor.agent_activity_payload(state.messages)
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
    sdk_client_factory: Any,
    result_message_type: type,
) -> None:
    cancelled = False

    async def finalize() -> None:
        """收尾（观测归档 -> 可能的 error -> done），恰好一次。

        由 ResultMessage 处即时调用，使终态不被可选的 Prompt Suggestion 扣住；
        error/无 ResultMessage/异常等路径由 finally 兜底调用。顺序保持
        「complete -> error -> done」不变：投影层会丢弃 done 之后的 error，
        若把 done 提前到 complete 之前，收尾期的错误就会被静默吞掉。
        """
        if stream_run.finalized:
            return
        stream_run.finalized = True
        try:
            await _complete_stream_run(stream_run, root_span, generation)
        except Exception as exc:
            if not stream_run.runtime._should_suppress_exception(exc, stream_run.query_state.errors):
                stream_run.query_state.errors.append(f"{exc.__class__.__name__}: {exc}")
                await event_queue.put({"event": "error", "data": {"errors": stream_run.query_state.errors}})
            if stream_run.runtime.user_input_service is not None:
                stream_run.runtime.user_input_service.clear_run_grants(stream_run.request_context.run_id)
        await event_queue.put({"event": "done", "data": "[DONE]"})

    try:
        await _emit_query_events(
            stream_run,
            event_queue,
            query_func,
            sdk_client_factory,
            result_message_type,
            finalize,
        )
    except asyncio.CancelledError as exc:
        cancelled = True
        await asyncio.to_thread(
            _abort_stream_run,
            stream_run,
            terminal_status="cancelled",
            error=exc,
        )
        raise
    except Exception as exc:
        if not stream_run.runtime._should_suppress_exception(exc, stream_run.query_state.errors):
            stream_run.query_state.errors.append(f"{exc.__class__.__name__}: {exc}")
            error_data: JsonObject = {"errors": list(stream_run.query_state.errors)}
            error_code = getattr(exc, "error_code", None)
            if isinstance(error_code, str) and error_code:
                error_data["error_code"] = error_code
                error_data["detail"] = str(exc)
                error_details = getattr(exc, "error_details", None)
                if isinstance(error_details, dict):
                    error_data.update(error_details)
                raw_output_json = getattr(exc, "raw_output_json", None)
                if isinstance(raw_output_json, dict):
                    error_data.update(raw_output_json)
            await event_queue.put({"event": "error", "data": error_data})
    finally:
        if cancelled:
            if stream_run.runtime.user_input_service is not None:
                stream_run.runtime.user_input_service.clear_run_grants(stream_run.request_context.run_id)
        else:
            # 正常路径已在 ResultMessage 处收过尾；这里兜底 error/无 ResultMessage 等路径。
            await finalize()
            await event_queue.put(None)


def _persist_stream_run(stream_run: StreamRun) -> None:
    """幂等落库：抽取 output + session save + record_run。

    在 ResultMessage 处（response.completed 之前）先调，使 items/retrieve 在完成信号时刻即可查；
    ``_complete_stream_run`` 的 finally 再兜底调（error/无 ResultMessage 路径）；客户端取消不落库。
    ``stream_run.persisted`` 保证一次流式请求恰好落库一次（session save 同时置 sdk_session_id+agent_id，
    满足 items 的 owning-agent 强校验，不会出现 sdk_session_id 已置而 agent_id 为空的 500 中间态）。
    """
    if stream_run.persisted or stream_run.persistence_attempted:
        return
    stream_run.persistence_attempted = True
    runtime = stream_run.runtime
    answer, agent_activity, output = runtime._runtime_output_from_state(
        stream_run.req,
        stream_run.request_context,
        stream_run.query_state,
    )
    stream_run.final_output = output
    if stream_run.turn_heartbeat is not None:
        stream_run.turn_heartbeat.stop()
    runtime._complete_runtime_request(stream_run.req, stream_run.request_context, stream_run.query_state, answer, agent_activity)
    stream_run.persisted = True


def _abort_stream_run(
    stream_run: StreamRun,
    *,
    terminal_status: str,
    error: BaseException | str,
) -> None:
    if stream_run.request_context.finalized or stream_run.request_context.finalization_attempted:
        return
    if stream_run.turn_heartbeat is not None:
        stream_run.turn_heartbeat.stop()
    answer, _, output = stream_run.runtime._runtime_output_from_state(
        stream_run.req,
        stream_run.request_context,
        stream_run.query_state,
    )
    stream_run.final_output = output
    stream_run.runtime._abort_runtime_request(
        stream_run.req,
        stream_run.request_context,
        stream_run.query_state,
        terminal_status=terminal_status,
        error=error,
    )
    stream_run.persisted = True


async def _complete_stream_run(
    stream_run: StreamRun,
    root_span: Any,
    generation: Any,
) -> None:
    await asyncio.to_thread(_complete_stream_run_sync, stream_run, root_span, generation)


def _complete_stream_run_sync(
    stream_run: StreamRun,
    root_span: Any,
    generation: Any,
) -> None:
    runtime = stream_run.runtime
    if stream_run.query_state.result_observed and not stream_run.query_state.mirror_errors:
        _persist_stream_run(stream_run)
    else:
        _abort_stream_run(
            stream_run,
            terminal_status="failed",
            error=(stream_run.query_state.mirror_errors[-1] if stream_run.query_state.mirror_errors else "SDK query ended without ResultMessage"),
        )
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
    try:
        if service is not None:
            await service.cancel_run(stream_run.request_context.run_id, decision="client_cancelled")
    finally:
        sdk_task.cancel()
        with suppress(asyncio.CancelledError):
            await sdk_task
