from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends

from app.routers.error_helpers import ensure_found
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.json_types import JsonObject


def create_langfuse_traces_router(*, runtime: ClaudeRuntime, require_api_key: Callable) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["improvements"], dependencies=[Depends(require_api_key)])

    @router.get(
        "/langfuse/traces/{trace_id}",
        response_model=dict,
        summary="Fetch one Langfuse trace through backend credentials",
    )
    async def get_langfuse_trace(trace_id: str) -> JsonObject:
        return ensure_found(runtime.fetch_langfuse_trace(trace_id), "Langfuse trace not found")

    return router
