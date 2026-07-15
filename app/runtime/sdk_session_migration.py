from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import claude_agent_sdk as sdk

from .errors import RuntimeUnavailableError
from .sdk_session_store import SqliteSdkSessionStore
from .session_store import LocalSession, LocalSessionStore

_CONFIG_DIR_LOCK = threading.Lock()


@contextmanager
def _claude_config_dir(config_dir: Path) -> Iterator[None]:
    with _CONFIG_DIR_LOCK:
        previous = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = str(config_dir)
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop("CLAUDE_CONFIG_DIR", None)
            else:
                os.environ["CLAUDE_CONFIG_DIR"] = previous


async def ensure_sdk_store_ready(
    session_store: LocalSessionStore,
    session: LocalSession,
    *,
    workspace_dir: str | Path,
    claude_config_dir: str | Path,
) -> LocalSession:
    """将旧本地 transcript 一次性导入 committed SessionStore。"""
    sdk_session_id = session.sdk_session_id
    if not sdk_session_id:
        return session
    project_key = sdk.project_key_for_directory(str(workspace_dir))
    if session.sdk_store_ready_at is not None:
        if session.sdk_project_key != project_key:
            raise RuntimeUnavailableError("Persisted SDK session project key does not match its owning Agent")
        return session

    claim = session_store.begin_sdk_store_import(
        session_id=session.session_id,
        sdk_session_id=sdk_session_id,
        sdk_project_key=project_key,
    )
    if claim is None:
        ready = session_store.get(session.session_id)
        if ready is None:
            raise RuntimeUnavailableError("Session disappeared during SDK transcript migration")
        return ready

    adapter = SqliteSdkSessionStore.for_import(
        session_store.Session,
        project_key=project_key,
        sdk_session_id=sdk_session_id,
        import_id=claim.token,
    )

    def import_local_transcript() -> None:
        with _claude_config_dir(Path(claude_config_dir)):
            asyncio.run(
                sdk.import_session_to_store(
                    sdk_session_id,
                    adapter,
                    directory=str(workspace_dir),
                    include_subagents=True,
                )
            )

    try:
        await asyncio.to_thread(import_local_transcript)
        return session_store.complete_sdk_store_import(claim=claim)
    except Exception as exc:
        session_store.fail_sdk_store_import(
            claim=claim,
            error=f"{exc.__class__.__name__}: {exc}",
        )
        raise RuntimeUnavailableError(f"SDK transcript migration failed for session {session.session_id}") from exc


async def committed_sdk_history_store(
    session_store: LocalSessionStore,
    session: LocalSession,
    *,
    workspace_dir: str | Path,
    claude_config_dir: str | Path,
) -> tuple[LocalSession, SqliteSdkSessionStore]:
    """Resolve a read-only SDK store from the persisted session binding.

    A candidate worktree may be removed after publication, so history reads cannot recompute its
    project key from the owning Agent's current workspace. Legacy sessions are imported first;
    already-mirrored sessions use their backend-owned project key and SDK session id directly.
    """
    if session.sdk_store_ready_at is None:
        session = await ensure_sdk_store_ready(
            session_store,
            session,
            workspace_dir=workspace_dir,
            claude_config_dir=claude_config_dir,
        )
    if not session.sdk_session_id or not session.sdk_project_key:
        raise RuntimeUnavailableError(f"Session {session.session_id} has no committed SDK store binding")
    return session, SqliteSdkSessionStore.for_committed_session(
        session_store.Session,
        project_key=session.sdk_project_key,
        sdk_session_id=session.sdk_session_id,
    )
