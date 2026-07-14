from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import suppress

from app.runtime.json_types import JsonObject
from app.runtime.response_disposition_control import TrustedResponseDispositionContext
from app.runtime.stores.response_disposition_claim_store import (
    ResponseDispositionClaimError,
    ResponseDispositionClaimStore,
)


async def observe_response_disposition_stream(
    source: AsyncIterator[JsonObject],
    *,
    context: TrustedResponseDispositionContext,
    claim_store: ResponseDispositionClaimStore,
) -> AsyncIterator[JsonObject]:
    """Bind and close an approved-execution claim around one runtime stream."""

    approval_request_id = context.approval_request_id
    if context.phase != "approved_execution" or approval_request_id is None:
        async for frame in source:
            yield frame
        return

    terminal = False
    saw_error = False
    try:
        async for frame in source:
            event = frame.get("event")
            data = frame.get("data")
            payload = data if isinstance(data, dict) else {}
            if event == "session":
                run_id = payload.get("run_id")
                if not isinstance(run_id, str) or not run_id:
                    raise ResponseDispositionClaimError("approved execution did not publish an AgentGov run id")
                claim_store.bind_run(approval_request_id, agent_run_id=run_id)
            elif event == "error":
                saw_error = True
            elif event == "result":
                errors = payload.get("errors")
                if saw_error or (isinstance(errors, list) and errors):
                    claim_store.finish(approval_request_id, target="failed", reason="runtime_reported_error")
                else:
                    claim_store.finish(approval_request_id, target="completed")
                terminal = True
            elif event == "done" and not terminal:
                claim_store.finish(
                    approval_request_id,
                    target="failed" if saw_error else "cancelled",
                    reason="runtime_reported_error" if saw_error else "stream_ended_without_result",
                )
                terminal = True
            yield frame
    except Exception as exc:
        if not terminal:
            _finish_failed(claim_store, approval_request_id, reason=type(exc).__name__)
            terminal = True
        raise
    finally:
        if not terminal:
            _finish_cancelled(claim_store, approval_request_id)


def _finish_failed(claim_store: ResponseDispositionClaimStore, approval_request_id: str, *, reason: str) -> None:
    with suppress(ResponseDispositionClaimError):
        claim_store.finish(approval_request_id, target="failed", reason=reason)


def _finish_cancelled(claim_store: ResponseDispositionClaimStore, approval_request_id: str) -> None:
    with suppress(ResponseDispositionClaimError):
        claim_store.finish(approval_request_id, target="cancelled", reason="stream_cancelled")
