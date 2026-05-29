from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends

from app.runtime.schemas import SessionInfo
from app.runtime.session_store import LocalSessionStore


def create_sessions_router(*, session_store: LocalSessionStore, require_api_key: Callable) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["sessions"], dependencies=[Depends(require_api_key)])

    @router.get(
        "/sessions",
        response_model=list[SessionInfo],
        summary="List API session mappings",
    )
    async def list_sessions() -> list[SessionInfo]:
        return [SessionInfo(**session.__dict__) for session in session_store.list()]

    @router.delete(
        "/sessions/{session_id}",
        summary="Delete one API session mapping",
    )
    async def delete_session(session_id: str) -> dict[str, object]:
        deleted = session_store.delete(session_id)
        return {"deleted": deleted, "session_id": session_id}

    return router
