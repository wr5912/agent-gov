from __future__ import annotations

import asyncio
from typing import Any

from fastapi.responses import JSONResponse

from app.runtime.json_types import JsonObject

from .client import (
    AgentScopeRuntimeClient,
    RuntimeJsonResponse,
    RuntimeUpstreamError,
    copy_response_headers,
)
from .contracts import AgentRunResponse
from .store import RuntimeRunStore, RuntimeStateConflict, SessionCreationStatus

RUN_ID_HEADER = "X-AgentGov-Run-Id"
SESSION_ID_HEADER = "X-AgentGov-Session-Id"
PROVEN_UNSCHEDULED_CHAT_STATUSES = frozenset(
    {400, 401, 403, 404, 405, 409, 410, 413, 415, 422, 429},
)


def _audit_error(stage: str, error: BaseException) -> JsonObject:
    return {"stage": stage, "type": error.__class__.__name__}


async def _compensate_session_creation(
    client: AgentScopeRuntimeClient,
    store: RuntimeRunStore,
    intent_id: str,
    session_id: str,
    runtime_agent_id: str,
    original_error: BaseException,
) -> bool:
    audit = _audit_error("post_create_finalize", original_error)
    store.mark_session_creation(intent_id, status=SessionCreationStatus.CLEANUP_PENDING, error=audit)
    try:
        await client.request_json("DELETE", f"/sessions/{session_id}", params={"agent_id": runtime_agent_id})
    except RuntimeUpstreamError as exc:
        if exc.status_code != 404:
            store.mark_session_creation(
                intent_id,
                status=SessionCreationStatus.CLEANUP_PENDING,
                error={**audit, "cleanup_error": _audit_error("delete_upstream", exc)},
                cleanup_attempt=False,
            )
            return False
    except Exception as exc:
        store.mark_session_creation(
            intent_id,
            status=SessionCreationStatus.CLEANUP_PENDING,
            error={**audit, "cleanup_error": _audit_error("delete_upstream", exc)},
            cleanup_attempt=False,
        )
        return False
    store.mark_session_creation(intent_id, status=SessionCreationStatus.FAILED_CLEANED, error=audit)
    return True


async def _call(client: AgentScopeRuntimeClient, method: str, path: str, **kwargs: Any):
    try:
        return await client.request_json(method, path, **kwargs)
    except RuntimeUpstreamError:
        raise


async def _request_binding_interrupts(
    client: AgentScopeRuntimeClient,
    bindings: list[Any],
) -> list[Any]:
    """同时尝试全部 Team Session，避免首个失败遗留仍运行的 worker。"""

    return list(
        await asyncio.gather(
            *(
                client.request_json(
                    "POST",
                    f"/sessions/{binding.session_id}/interrupt",
                    params={"agent_id": binding.runtime_agent_id},
                )
                for binding in bindings
            ),
            return_exceptions=True,
        ),
    )


async def _interrupt_active_run(
    client: AgentScopeRuntimeClient,
    store: RuntimeRunStore,
    run: AgentRunResponse,
    *,
    primary_session_id: str,
) -> Any:
    bindings = store.active_session_bindings(run.run_id)
    if not bindings:
        raise RuntimeStateConflict("Active run has no Runtime Session fences")
    results = await _request_binding_interrupts(client, bindings)
    errors = [result for result in results if isinstance(result, BaseException)]
    unresolved = [error for error in errors if not isinstance(error, RuntimeUpstreamError) or error.status_code != 404]
    if unresolved:
        store.mark_cancellation_uncertain(
            run.run_id,
            error={"type": type(unresolved[0]).__name__},
        )
        raise unresolved[0]
    store.mark_recovery_interrupt_requested(run.run_id)
    for binding, result in zip(bindings, results, strict=True):
        if binding.session_id == primary_session_id:
            if isinstance(result, RuntimeUpstreamError) and result.status_code == 404:
                return RuntimeJsonResponse(
                    status_code=202,
                    headers={},
                    body={"status": "already_idle"},
                )
            return result
    raise RuntimeStateConflict("Requested Runtime Session is not fenced by the active run")


def _json_upstream(upstream: Any) -> JSONResponse:
    return JSONResponse(upstream.body, status_code=upstream.status_code, headers=copy_response_headers(upstream.headers))


def _cancel_response(run: AgentRunResponse) -> JSONResponse:
    return JSONResponse(
        {
            "run_id": run.run_id,
            "session_id": run.session_id,
            "status": run.status,
        },
        status_code=202,
        headers={RUN_ID_HEADER: run.run_id, SESSION_ID_HEADER: run.session_id},
    )
