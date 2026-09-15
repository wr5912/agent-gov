"""ASGI startup hook for Runtime Workspace reconciliation."""

from __future__ import annotations

from typing import Protocol

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .settings import RUNTIME_USER_ID


class WorkspaceReconciler(Protocol):
    async def reconcile(self, user_id: str) -> None: ...


class SessionWorkspaceReleaseMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        workspace_reclaimer: WorkspaceReconciler,
    ) -> None:
        self._app = app
        self._reclaimer = workspace_reclaimer

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._run_lifespan(scope, receive, send)
            return
        await self._app(scope, receive, send)

    async def _run_lifespan(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def reconcile_before_ready(message: Message) -> None:
            if message["type"] == "lifespan.startup.complete":
                await self._reclaimer.reconcile(RUNTIME_USER_ID)
            await send(message)

        await self._app(scope, receive, reconcile_before_ready)
