from __future__ import annotations

import asyncio
from collections.abc import Callable

from fastapi import APIRouter, Depends

from app.routers.error_helpers import ensure_found
from app.runtime.integrations.runtime_langfuse import RuntimeLangfuseClient
from app.runtime.json_types import JsonObject


def create_langfuse_traces_router(*, client: RuntimeLangfuseClient, require_api_key: Callable) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["traces"], dependencies=[Depends(require_api_key)])

    @router.get(
        "/langfuse/traces/{trace_id}",
        response_model=dict,
        summary="Fetch one Langfuse trace by OTel trace_id",
    )
    async def get_langfuse_trace(trace_id: str) -> JsonObject:
        trace = await asyncio.to_thread(client.fetch_trace, trace_id)
        return ensure_found(trace, "Langfuse trace not found")

    return router
