"""Release zero-reference Workspaces after AgentScope Session deletion."""

from __future__ import annotations

import logging
from urllib.parse import parse_qs

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .settings import RUNTIME_USER_ID
from .workspace_manager import AgentGovWorkspaceManager

logger = logging.getLogger(__name__)


class SessionWorkspaceReleaseMiddleware:
    """Observe Session DELETE outcomes without patching AgentScope routes."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        workspace_manager: AgentGovWorkspaceManager,
    ) -> None:
        self._app = app
        self._workspace_manager = workspace_manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        target = self._delete_target(scope)
        if target is None:
            await self._app(scope, receive, send)
            return

        agent_id, session_id = target
        workspace_id: str | None = None
        try:
            workspace_id = await self._workspace_manager.resolve_session_workspace_id(
                RUNTIME_USER_ID,
                agent_id,
                session_id,
            )
        except Exception:
            logger.exception(
                "Failed to resolve Workspace before deleting Session %r",
                session_id,
            )

        status_code: int | None = None

        async def capture_status(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        await self._app(scope, receive, capture_status)
        if status_code not in {204, 404}:
            return
        try:
            await self._workspace_manager.release_session_workspace_if_unreferenced(
                RUNTIME_USER_ID,
                agent_id,
                session_id,
                workspace_id=workspace_id,
            )
        except Exception:
            # The Session response is already committed. Do not replace it
            # with an observer failure; a later retry/restart can reconcile.
            logger.exception(
                "Failed to release Workspace after deleting Session %r",
                session_id,
            )

    @staticmethod
    def _delete_target(scope: Scope) -> tuple[str, str] | None:
        if scope["type"] != "http" or scope.get("method") != "DELETE":
            return None
        path = scope.get("path", "")
        prefix = "/sessions/"
        if not isinstance(path, str) or not path.startswith(prefix):
            return None
        session_id = path[len(prefix) :]
        if not session_id or "/" in session_id:
            return None
        try:
            query = parse_qs(
                scope.get("query_string", b"").decode("ascii"),
                keep_blank_values=True,
            )
        except UnicodeDecodeError:
            return None
        agent_ids = query.get("agent_id", [])
        if len(agent_ids) != 1 or not agent_ids[0]:
            return None
        return agent_ids[0], session_id
