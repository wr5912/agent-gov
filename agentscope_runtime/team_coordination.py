"""通过公共 Storage/MessageBus 扩展关联 AgentScope Team 子会话。"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

import httpx
from agentscope.app.message_bus import InMemoryMessageBus, MessageBusKeys
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .context_registry import RuntimeContext
from .receipt_middleware import CURRENT_RUNTIME_CONTEXT, fetch_runtime_context
from .settings import RuntimeSettings
from .signing import signed_headers

_CHILD_SESSION_PATH = "/internal/runtime-child-sessions"
_TEAM_INBOX_PATH = "/internal/runtime-team-inbox"
_RUNTIME_BOOT_PATH = "/internal/runtime-boots"
_INBOX_PREFIX = MessageBusKeys.inbox("")

logger = logging.getLogger(__name__)


class RuntimeChildSessionRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    parent_session_id: str = Field(min_length=1)
    child_session_id: str = Field(min_length=1)
    child_runtime_agent_id: str = Field(min_length=1)
    team_id: str = Field(min_length=1)


class RuntimeTeamInboxDelivery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    source_session_id: str = Field(min_length=1)
    target_session_id: str = Field(min_length=1)


class RuntimeTeamInboxAck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    generation: int = Field(ge=1)


class RuntimeBootAnnouncement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    boot_id: str = Field(min_length=1)
    runtime_version: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9.+_-]*$",
    )


class RuntimeBootAck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    boot_id: str = Field(min_length=1)
    runtime_version: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9.+_-]*$",
    )
    recovery_run_ids: list[str] = Field(default_factory=list)


class RuntimeBootCoordinator:
    """通知 AgentGov Runtime 进程代际，并在确认前关闭 chat 数据面。"""

    def __init__(
        self,
        settings: RuntimeSettings,
        *,
        boot_id: str | None = None,
    ) -> None:
        self._settings = settings
        self.boot_id = boot_id or f"runtime-boot-{uuid.uuid4()}"
        self._acknowledged = asyncio.Event()

    @property
    def acknowledged(self) -> bool:
        return self._acknowledged.is_set()

    async def announce_until_acknowledged(self) -> None:
        """API 可能依赖 Runtime health 启动，因此后台重试而不阻塞 lifespan。"""

        delay = self._settings.receipt_retry_backoff_seconds
        while not self._acknowledged.is_set():
            try:
                async with httpx.AsyncClient(
                    base_url=self._settings.agentgov_api_base_url,
                    timeout=self._settings.request_timeout_seconds,
                    trust_env=False,
                ) as client:
                    response = await _post_signed_json(
                        client,
                        self._settings,
                        _RUNTIME_BOOT_PATH,
                        RuntimeBootAnnouncement(
                            boot_id=self.boot_id,
                            runtime_version=self._settings.runtime_version,
                        ),
                    )
                acknowledgement = RuntimeBootAck.model_validate(response)
                if acknowledgement.boot_id != self.boot_id or acknowledgement.runtime_version != self._settings.runtime_version:
                    raise RuntimeError("AgentGov returned a mismatched Runtime boot acknowledgement")
                self._acknowledged.set()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # API 在 Compose 依赖链后启动是正常暂态
                logger.debug(
                    "AgentGov Runtime boot acknowledgement deferred: error_type=%s",
                    type(exc).__name__,
                )
                await asyncio.sleep(min(delay, 1.0))
                delay = min(delay * 2, 1.0)


class RuntimeBootCoordinationMiddleware:
    """允许 liveness 启动依赖链，但 boot ack 前拒绝任何新 chat。"""

    def __init__(self, app: ASGIApp, *, coordinator: RuntimeBootCoordinator) -> None:
        self.app = app
        self._coordinator = coordinator

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            task = asyncio.create_task(
                self._coordinator.announce_until_acknowledged(),
                name="agentgov-runtime-boot-coordination",
            )
            try:
                await self.app(scope, receive, send)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            return
        if scope["type"] == "http" and scope.get("method") == "POST" and scope.get("path") == "/chat/" and not self._coordinator.acknowledged:
            response = JSONResponse(
                status_code=503,
                content={
                    "detail": "Runtime boot coordination is pending",
                    "error_code": "RUNTIME_BOOT_PENDING",
                },
                headers={"Retry-After": "1"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


async def register_team_child_session(
    storage: Any,
    settings: RuntimeSettings,
    *,
    user_id: str,
    child_session_id: str,
    team_id: str,
) -> RuntimeContext | None:
    """Team leader 是顶层 session；worker 在唤醒前同步绑定到同一 run。"""

    team = await storage.get_team(user_id, team_id)
    if team is None:
        raise RuntimeError("AgentScope Team disappeared before child binding")
    if team.session_id == child_session_id:
        return None
    child = await storage.get_session(user_id, "", child_session_id)
    if child is None:
        raise RuntimeError("AgentScope child Session disappeared before binding")
    async with httpx.AsyncClient(
        base_url=settings.agentgov_api_base_url,
        timeout=settings.request_timeout_seconds,
        trust_env=False,
    ) as client:
        parent = await fetch_runtime_context(client, settings, team.session_id)
        response = await _post_signed_json(
            client,
            settings,
            _CHILD_SESSION_PATH,
            RuntimeChildSessionRegistration(
                run_id=parent.run_id,
                parent_session_id=team.session_id,
                child_session_id=child_session_id,
                child_runtime_agent_id=child.agent_id,
                team_id=team_id,
            ),
        )
    context = RuntimeContext.model_validate(response)
    if (
        context.run_id != parent.run_id
        or context.session_id != child_session_id
        or context.runtime_agent_id != child.agent_id
        or context.trace_id != parent.trace_id
    ):
        raise RuntimeError("AgentGov returned a mismatched child Runtime context")
    return context


class AgentGovInMemoryMessageBus(InMemoryMessageBus):
    """在跨 Session inbox 入队前持久化 run quiescence fence。"""

    def __init__(
        self,
        settings: RuntimeSettings,
    ) -> None:
        super().__init__()
        self._settings = settings
        self._delivery_by_payload_id: dict[int, tuple[str, str, int]] = {}
        self._processed_generations: dict[tuple[str, str], int] = {}

    async def queue_push(
        self,
        key: str,
        payload: dict,
        *,
        ttl_secs: int | None = None,
    ) -> str:
        context = CURRENT_RUNTIME_CONTEXT.get()
        target_session_id = key.removeprefix(_INBOX_PREFIX) if key.startswith(_INBOX_PREFIX) else ""
        if context is not None and target_session_id and target_session_id != context.session_id:
            # 先落控制面 fence 再入队。若进程在两步之间退出，run 会保持非终态并在
            # restart reconciliation 中 interrupted；绝不产生“消息已发但 run 已成功”。
            event_id = f"team-delivery-{uuid.uuid4()}"
            delivery = RuntimeTeamInboxDelivery(
                event_id=event_id,
                run_id=context.run_id,
                source_session_id=context.session_id,
                target_session_id=target_session_id,
            )
            async with httpx.AsyncClient(
                base_url=self._settings.agentgov_api_base_url,
                timeout=self._settings.request_timeout_seconds,
                trust_env=False,
            ) as client:
                response = await _post_signed_json(
                    client,
                    self._settings,
                    _TEAM_INBOX_PATH,
                    delivery,
                )
            acknowledgement = RuntimeTeamInboxAck.model_validate(response)
            if acknowledgement.run_id != context.run_id or acknowledgement.event_id != event_id:
                raise RuntimeError("AgentGov returned a mismatched Team inbox fence")
            # InMemoryMessageBus keeps the exact payload object across the
            # framework's drain/requeue peek. A shallow copy gives this
            # delivery a stable process-local identity without adding private
            # metadata to AgentScope's public HintBlock payload.
            payload = dict(payload)
            self._delivery_by_payload_id[id(payload)] = (
                context.run_id,
                target_session_id,
                acknowledgement.generation,
            )
        return await super().queue_push(key, payload, ttl_secs=ttl_secs)

    async def queue_drain(
        self,
        key: str,
        max_count: int = 100,
    ) -> list[tuple[str, dict]]:
        entries = await super().queue_drain(key, max_count=max_count)
        context = CURRENT_RUNTIME_CONTEXT.get()
        target_session_id = key.removeprefix(_INBOX_PREFIX) if key.startswith(_INBOX_PREFIX) else ""
        if context is None or context.session_id != target_session_id:
            return entries
        for _entry_id, payload in entries:
            delivery = self._delivery_by_payload_id.pop(id(payload), None)
            if delivery is None:
                continue
            run_id, delivery_target, generation = delivery
            if run_id != context.run_id or delivery_target != context.session_id:
                raise RuntimeError("AgentScope Team inbox delivery crossed governed run context")
            identity = (run_id, delivery_target)
            self._processed_generations[identity] = max(
                self._processed_generations.get(identity, 0),
                generation,
            )
        return entries

    def processed_team_generation(self, context: RuntimeContext) -> int:
        """返回目标 Session 已实际 drain 的最大受控 Team generation。"""

        return self._processed_generations.get((context.run_id, context.session_id), 0)

    async def aclose(self) -> None:
        self._delivery_by_payload_id.clear()
        self._processed_generations.clear()
        await super().aclose()


async def _post_signed_json(
    client: httpx.AsyncClient,
    settings: RuntimeSettings,
    path: str,
    payload: BaseModel,
) -> object:
    body = json.dumps(
        payload.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    headers = signed_headers(settings.shared_secret, "POST", path, body)
    headers["Content-Type"] = "application/json"
    response = await client.post(path, content=body, headers=headers)
    response.raise_for_status()
    if not response.content:
        return None
    return response.json()
