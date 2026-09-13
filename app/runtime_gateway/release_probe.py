from __future__ import annotations

import uuid

from app.runtime.json_types import JsonObject

from .client import AgentScopeRuntimeClient, RuntimeUpstreamError
from .store import RuntimeRunStore, RuntimeStateConflict


class ReleaseWorkspaceProbe:
    """用原生 Session 与 Workspace API 验证精确模板，持久保存清理定位符。"""

    def __init__(self, client: AgentScopeRuntimeClient, store: RuntimeRunStore, session_config: JsonObject) -> None:
        self.client = client
        self.store = store
        self.session_config = session_config

    async def ensure(self, runtime_agent_id: str, *, workspace_id: str, activation_key: str) -> None:
        if not self.session_config:
            raise RuntimeStateConflict("Release Runtime Session configuration is missing")
        session_id = await self._locate(runtime_agent_id, workspace_id, activation_key)
        if session_id is None:
            session_id = await self._create(runtime_agent_id, workspace_id, activation_key)
        await self.client.request_json(
            "GET",
            "/workspace/status",
            params={"agent_id": runtime_agent_id, "session_id": session_id},
        )

    async def _locate(self, runtime_agent_id: str, workspace_id: str, activation_key: str) -> str | None:
        matches = await self.client.list_session_ids_for_workspace(runtime_agent_id, workspace_id)
        if len(matches) > 1:
            raise RuntimeStateConflict("Published Runtime workspace probe identity is ambiguous")
        ledger = self.store.get_ephemeral_resource(activation_key)
        if ledger is None:
            raise RuntimeStateConflict("Release activation ledger is missing")
        session_id = ledger.session_id
        if session_id is not None and session_id not in matches:
            if matches:
                raise RuntimeStateConflict("Release probe Session locator is ambiguous")
            self.store.clear_ephemeral_session(activation_key, session_id)
            session_id = None
        if session_id is None and matches:
            session_id = matches[0]
            self.store.record_ephemeral_session(activation_key, session_id)
        return session_id

    async def _create(self, runtime_agent_id: str, workspace_id: str, activation_key: str) -> str:
        probe_key = uuid.uuid5(uuid.NAMESPACE_URL, f"agentgov:release-probe:{workspace_id}")
        try:
            response = await self.client.request_json(
                "POST",
                "/sessions/",
                json={
                    "agent_id": runtime_agent_id,
                    "workspace_id": workspace_id,
                    "chat_model_config": dict(self.session_config),
                    "name": f"AgentGov release probe {probe_key}",
                },
            )
        except RuntimeUpstreamError as exc:
            if exc.status_code not in {503, 504}:
                raise
            recovered = await self._locate(runtime_agent_id, workspace_id, activation_key)
            if recovered is None:
                raise
            return recovered
        session_id = response.body.get("session_id") if isinstance(response.body, dict) else None
        if not isinstance(session_id, str) or not session_id:
            raise RuntimeStateConflict("AgentScope did not return a release probe Session id")
        self.store.record_ephemeral_session(activation_key, session_id)
        return session_id

    async def cleanup(self, activation_key: str) -> None:
        ledger = self.store.get_ephemeral_resource(activation_key)
        if ledger is None:
            raise RuntimeStateConflict("Release activation ledger is missing")
        if ledger.runtime_agent_id is None:
            if ledger.session_id is not None:
                raise RuntimeStateConflict("Release probe Session has no durable Runtime Agent locator")
            return
        try:
            session_id = await self._locate(ledger.runtime_agent_id, ledger.workspace_id, activation_key)
        except RuntimeUpstreamError as exc:
            if exc.status_code != 404:
                raise
            session_id = ledger.session_id
        if session_id is None:
            return
        try:
            await self.client.delete_session(session_id, ledger.runtime_agent_id)
        except RuntimeUpstreamError as exc:
            if exc.status_code != 404:
                raise
        self.store.clear_ephemeral_session(activation_key, session_id)
