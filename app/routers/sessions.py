from __future__ import annotations

from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool

from app.runtime.errors import NotFoundError
from app.runtime.session_schemas import SessionDeleteResponse, SessionInfo, SessionMessagesResponse
from app.runtime.session_history import read_session_history
from app.runtime.session_store import LocalSession, LocalSessionStore
from app.runtime.settings import AppSettings

_MAIN_AGENT_IDS = {"", "main-agent", "main"}


def _resolve_profile_paths(settings: AppSettings, session: LocalSession) -> tuple[Path, Path]:
    """Return (workspace_dir, claude_config_dir) for the agent that owns the session.

    Defaults to the main profile. If the session metadata records a business ``agent_id``, use
    that business agent's workspace + claude-root (mirrors build_business_agent_profile).
    """
    metadata = session.metadata if isinstance(session.metadata, dict) else {}
    agent_id = metadata.get("agent_id")
    if isinstance(agent_id, str) and agent_id.strip() and agent_id.strip() not in _MAIN_AGENT_IDS:
        base = settings.data_dir / "business-agents" / agent_id.strip()
        return base, base / "claude-root" / ".claude"
    return settings.main_workspace_dir, settings.main_claude_root / ".claude"


def create_sessions_router(*, session_store: LocalSessionStore, settings: AppSettings, require_api_key: Callable) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["sessions"], dependencies=[Depends(require_api_key)])

    @router.get(
        "/sessions",
        response_model=list[SessionInfo],
        summary="List API session mappings",
    )
    async def list_sessions() -> list[SessionInfo]:
        return [SessionInfo(**session.__dict__) for session in session_store.list()]

    @router.get(
        "/sessions/{session_id}/messages",
        response_model=SessionMessagesResponse,
        summary="Read a session's conversation history (projected from the SDK session transcript)",
    )
    async def get_session_messages(session_id: str, limit: int | None = None, offset: int = 0) -> SessionMessagesResponse:
        session = session_store.get(session_id)
        if session is None:
            raise NotFoundError(f"session {session_id} not found")
        if not session.sdk_session_id:
            # Session exists but never produced an SDK transcript yet -> empty history, not 404.
            return SessionMessagesResponse(session_id=session_id, sdk_session_id=None, title=session.title)
        workspace_dir, claude_config_dir = _resolve_profile_paths(settings, session)
        history = await run_in_threadpool(
            read_session_history,
            sdk_session_id=session.sdk_session_id,
            workspace_dir=workspace_dir,
            claude_config_dir=claude_config_dir,
            scrub=settings.session_history_scrub,
            limit=limit,
            offset=offset,
        )
        return SessionMessagesResponse(session_id=session_id, **history)

    @router.delete(
        "/sessions/{session_id}",
        response_model=SessionDeleteResponse,
        summary="Delete one API session mapping",
    )
    async def delete_session(session_id: str) -> SessionDeleteResponse:
        deleted = session_store.delete(session_id)
        return SessionDeleteResponse(deleted=deleted, session_id=session_id)

    return router
