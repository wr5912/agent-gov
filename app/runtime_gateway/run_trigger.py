"""Runtime chat 的唯一准入、触发、重放与取消边界。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from fastapi.responses import JSONResponse

from app.runtime.json_types import JsonObject

from ._execution_support import (
    _iter_sse_events,
    _ObservedExecution,
    _requires_interactive_continuation,
)
from ._router_operations import (
    PROVEN_UNSCHEDULED_CHAT_STATUSES,
    _interrupt_active_run,
)
from .client import (
    AgentScopeRuntimeClient,
    RuntimeHeaders,
    RuntimeUpstreamError,
    copy_response_headers,
)
from .contracts import TERMINAL_RUN_STATUSES, AgentRunResponse, ConfirmationScope
from .store import RuntimeRunStore, RuntimeStateConflict


@dataclass(frozen=True)
class RuntimeChatTriggerResult:
    """可持久重放的公开响应及其唯一 AgentGov run。"""

    run: AgentRunResponse
    status_code: int
    body: bytes
    content_type: str
    headers: RuntimeHeaders
    replayed: bool


@dataclass(frozen=True)
class _EncodedChatResponse:
    status_code: int
    body: bytes
    content_type: str
    headers: RuntimeHeaders


async def admit_and_trigger_chat(
    *,
    client: AgentScopeRuntimeClient,
    store: RuntimeRunStore,
    session_id: str,
    runtime_agent_id: str,
    input_value: object,
    alert_id: str | None,
    case_id: str | None,
    metadata: JsonObject,
    client_operation_id: str,
    confirmation_scope: ConfirmationScope = ConfirmationScope.ONCE,
    expected_run_id: str | None = None,
) -> RuntimeChatTriggerResult:
    """幂等准入、触发原生 ``/chat/``，并在返回前持久化响应。"""

    governed_input = deepcopy(input_value)
    admission = store.admit_run(
        session_id=session_id,
        runtime_agent_id=runtime_agent_id,
        input_value=governed_input,
        alert_id=alert_id,
        case_id=case_id,
        metadata=metadata,
        client_operation_id=client_operation_id,
        confirmation_scope=confirmation_scope,
        expected_run_id=expected_run_id,
    )
    run = admission.run
    if not admission.should_trigger_upstream:
        replay = admission.replay_response
        if replay is None:
            raise RuntimeStateConflict(
                "Runtime operation response is not durably available; recover by client operation lookup",
            )
        return RuntimeChatTriggerResult(
            run=run,
            status_code=replay.status_code,
            body=replay.body,
            content_type=replay.content_type,
            headers=replay.headers,
            replayed=True,
        )
    if admission.operation_key is None:
        raise RuntimeStateConflict(
            "Runtime chat operation is missing its durable identity",
        )
    response = await _trigger_chat_operation(
        client=client,
        store=store,
        run=run,
        input_value=governed_input,
        operation_key=admission.operation_key,
    )
    return RuntimeChatTriggerResult(
        run=store.get_run(run.run_id),
        status_code=response.status_code,
        body=response.body,
        content_type=response.content_type,
        headers=response.headers,
        replayed=False,
    )


async def _trigger_chat_operation(
    *,
    client: AgentScopeRuntimeClient,
    store: RuntimeRunStore,
    run: AgentRunResponse,
    input_value: object,
    operation_key: str,
) -> _EncodedChatResponse:
    try:
        upstream = await client.request_json(
            "POST",
            "/chat/",
            json={
                "agent_id": run.runtime_agent_id,
                "session_id": run.session_id,
                "input": input_value,
            },
        )
        response = _encode_chat_response(
            upstream.body,
            root_session_id=run.session_id,
            status_code=upstream.status_code,
            headers=upstream.headers,
        )
        store.record_chat_operation_response(
            operation_key,
            run_id=run.run_id,
            response_status=response.status_code,
            response_body=response.body,
            response_content_type=response.content_type,
            response_headers=response.headers,
        )
        return response
    except RuntimeUpstreamError as exc:
        _record_trigger_failure(store, run.run_id, exc)
        raise
    except Exception as exc:
        _mark_trigger_uncertain_preserving_error(store, run.run_id, exc)
        raise


async def observe_background_run(
    response: Any,
    *,
    store: RuntimeRunStore,
    run_id: str,
) -> _ObservedExecution:
    """消费后台 SSE，并等待 canonical receipts 收敛同一个 run。"""

    events: list[JsonObject] = []
    stream_task = asyncio.create_task(_collect_background_events(response, events))
    terminal_task = asyncio.create_task(_wait_for_terminal(store, run_id))
    try:
        done, _pending = await asyncio.wait(
            (stream_task, terminal_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stream_task in done:
            await stream_task
            terminal = await terminal_task
        else:
            terminal = terminal_task.result()
        return _ObservedExecution(events=events, terminal=terminal)
    finally:
        for task in (stream_task, terminal_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(stream_task, terminal_task, return_exceptions=True)


async def cancel_active_session_run(
    *,
    client: AgentScopeRuntimeClient,
    store: RuntimeRunStore,
    session_id: str,
) -> None:
    """通过 Team-aware 公共实现尽力取消后台 run。"""

    active = store.active_run_for_session(session_id)
    if active is None:
        return
    store.mark_cancel_requested(active.run_id)
    try:
        await _interrupt_active_run(
            client,
            store,
            active,
            primary_session_id=session_id,
        )
    except Exception:
        # Team-aware 实现已经持久化取消不确定态；不得用二次取消错误
        # 覆盖调用方原本要处理的执行或清理失败。
        return


def _encode_chat_response(
    body: object,
    *,
    root_session_id: str,
    status_code: int,
    headers: RuntimeHeaders,
) -> _EncodedChatResponse:
    projected = _canonical_chat_response(body, root_session_id=root_session_id)
    response_headers = copy_response_headers(headers)
    encoded = JSONResponse(
        projected,
        status_code=status_code,
        headers=response_headers,
    )
    return _EncodedChatResponse(
        status_code=status_code,
        body=encoded.body,
        content_type=encoded.headers["content-type"],
        headers=response_headers,
    )


def _canonical_chat_response(body: object, *, root_session_id: str) -> JsonObject:
    if not isinstance(body, dict) or body.get("status") != "started":
        raise RuntimeUpstreamError(502, b'{"detail":"Runtime chat returned an invalid response"}')
    projected = deepcopy(body)
    upstream_session_id = projected.get("session_id")
    if isinstance(upstream_session_id, str) and upstream_session_id and upstream_session_id != root_session_id:
        projected["worker_session_id"] = upstream_session_id
    projected["session_id"] = root_session_id
    return projected


def _record_trigger_failure(
    store: RuntimeRunStore,
    run_id: str,
    error: RuntimeUpstreamError,
) -> None:
    audit = {"type": error.__class__.__name__}
    if error.status_code in PROVEN_UNSCHEDULED_CHAT_STATUSES:
        store.fail_trigger(run_id, error=audit)
        return
    store.mark_trigger_uncertain(run_id, error=audit)


def _mark_trigger_uncertain_preserving_error(
    store: RuntimeRunStore,
    run_id: str,
    error: BaseException,
) -> None:
    try:
        store.mark_trigger_uncertain(run_id, error={"type": error.__class__.__name__})
    except Exception:
        # 原始传输/持久化异常持有有效因果链，二次记账失败不得覆盖它。
        return


async def _collect_background_events(
    response: Any,
    events: list[JsonObject],
) -> None:
    async for event in _iter_sse_events(response):
        events.append(event)
        if _requires_interactive_continuation(event):
            raise RuntimeStateConflict(
                "Non-interactive governance/test execution requires an explicit HITL continuation",
            )


async def _wait_for_terminal(
    store: RuntimeRunStore,
    run_id: str,
) -> AgentRunResponse:
    while True:
        run = store.get_run(run_id)
        if run.status in TERMINAL_RUN_STATUSES:
            return run
        await asyncio.sleep(0.05)


# 取消和恢复共用既有 Team-aware 实现；公开别名让 UI 与后台只依赖此边界。
interrupt_active_run = _interrupt_active_run
