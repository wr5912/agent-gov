from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi.concurrency import run_in_threadpool

from app.runtime.agent_paths import business_agent_layout
from app.runtime.errors import NotFoundError, SessionConflictError
from app.runtime.session_history import read_session_history
from app.runtime.session_schemas import SessionDeleteResponse, SessionInfo, SessionMessagesResponse
from app.runtime.session_store import LocalSession, LocalSessionStore
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_registry_store import AgentRegistryStore


def _resolve_owning_profile(
    settings: AppSettings, agent_registry_store: AgentRegistryStore, session: LocalSession
) -> tuple[Path, Path]:
    """Strongly resolve (workspace_dir, claude_config_dir) for the session's owning agent.

    The owning agent is the backend-owned ``session.agent_id`` persisted by the runtime at chat
    time (never client-supplied metadata). It is a hard invariant — there is no silent fallback:

    - missing / empty while a transcript exists -> 409 (the server cannot prove which Agent owns it);
    - any agent (含预制 main-agent) -> validated against the agent registry; cwd taken from its
      registered ``workspace_dir`` (consistent with /api/chat); an unknown / stale id -> 404.

    main-agent 在 lifespan 经 sync_business_agents 登记，故与其它业务 Agent 走完全相同的解析路径
    （不再有 main 特判）。
    """
    agent_id = (session.agent_id or "").strip()
    if not agent_id:
        raise SessionConflictError(f"Session {session.session_id} has no unambiguous business agent owner")
    record = agent_registry_store.get_agent(agent_id)
    if record is None:
        raise NotFoundError(f"owning agent '{agent_id}' of session {session.session_id} not found")
    # cwd 用注册表登记的 workspace_dir（与 /api/chat 的 build_business_agent_profile 一致，支持自定义/同步 workspace）；
    # claude-root 仍按 agent_id 推导（与 build_business_agent_profile 的 claude_root 一致）。
    claude_config_dir = business_agent_layout(settings.data_dir, agent_id).claude_root / ".claude"
    return Path(record.workspace_dir), claude_config_dir


def create_sessions_router(
    *,
    session_store: LocalSessionStore,
    settings: AppSettings,
    agent_registry_store: AgentRegistryStore,
    require_api_key: Callable,
) -> APIRouter:
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
    async def get_session_messages(
        session_id: str,
        limit: int | None = Query(default=None, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> SessionMessagesResponse:
        session = session_store.get(session_id)
        if session is None:
            raise NotFoundError(f"session {session_id} not found")
        if not session.sdk_session_id:
            # No SDK transcript produced yet -> empty history (not an owning-agent error).
            return SessionMessagesResponse(session_id=session_id, sdk_session_id=None, title=session.title)
        workspace_dir, claude_config_dir = _resolve_owning_profile(settings, agent_registry_store, session)
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
