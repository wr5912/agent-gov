"""在 AgentScope Storage lifespan 内引导固定模型凭据。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from importlib.metadata import PackageNotFoundError, version
from typing import Self

import httpx
from agentscope.app.storage import AsyncSQLAlchemyStorage
from agentscope.credential import CredentialFactory
from agentscope.message import Msg
from agentscope.state import AgentState

from .context_registry import RuntimeContext, bind_reply_context, discard_reply_contexts, take_reply_context
from .receipt_middleware import RuntimeReceipt, RuntimeReceiptAck, fetch_runtime_context, post_runtime_receipt
from .run_trace import AgentGovRunTraceRegistry
from .settings import RUNTIME_USER_ID, RuntimeSettings
from .team_coordination import AgentGovInMemoryMessageBus, register_team_child_session

logger = logging.getLogger(__name__)

try:
    _AGENTSCOPE_VERSION = version("agentscope")
except PackageNotFoundError:  # pragma: no cover - 生产镜像固定安装 AgentScope
    _AGENTSCOPE_VERSION = "unknown"

_TERMINAL_ACK_STATUSES = frozenset({"succeeded", "failed", "cancelled", "interrupted"})


async def provision_runtime_credential(
    storage: AsyncSQLAlchemyStorage,
    settings: RuntimeSettings,
) -> str:
    """通过 AgentScope 公共 Credential/Storage API 幂等写入固定凭据。"""

    credential_class = CredentialFactory.get_credential_class(
        settings.credential_type,
    )
    if credential_class is None:
        raise ValueError(
            f"Unsupported AGENTSCOPE_CREDENTIAL_TYPE: {settings.credential_type!r}",
        )
    if "api_key" not in credential_class.model_fields:
        raise ValueError(
            "AGENTSCOPE_CREDENTIAL_TYPE must accept MODEL_PROVIDER_API_KEY",
        )
    payload: dict[str, str] = {
        "type": settings.credential_type,
        "id": settings.credential_id,
        "name": "AgentGov Runtime Model Provider",
        "api_key": settings.provider_api_key,
    }
    if settings.provider_api_url is not None:
        if "base_url" not in credential_class.model_fields:
            raise ValueError(
                "AGENTSCOPE_CREDENTIAL_TYPE must accept MODEL_PROVIDER_API_URL",
            )
        payload["base_url"] = settings.provider_api_url
    credential = CredentialFactory.from_dict(payload)
    credential_id = await storage.upsert_credential(
        RUNTIME_USER_ID,
        credential,
    )
    if credential_id != settings.credential_id:
        raise RuntimeError("AgentScope stored an unexpected runtime credential id")
    return credential_id


class ProvisionedAsyncSQLAlchemyStorage(AsyncSQLAlchemyStorage):
    """在父类完成建表后、AgentScope 服务启动前引导凭据。"""

    def __init__(
        self,
        settings: RuntimeSettings,
        *,
        receipt_transport: httpx.AsyncBaseTransport | None = None,
        trace_registry: AgentGovRunTraceRegistry | None = None,
        message_bus: AgentGovInMemoryMessageBus | None = None,
    ) -> None:
        super().__init__(
            settings.database_url,
            create_tables=True,
            auto_migrate=True,
            engine_kwargs={"connect_args": {"timeout": 30}},
        )
        self._runtime_settings = settings
        self._receipt_transport = receipt_transport
        self._trace_registry = trace_registry
        self._message_bus = message_bus
        self._receipt_tasks: set[asyncio.Task[RuntimeReceiptAck | None]] = set()
        self._pending_batches: dict[tuple[str, str], dict[str, RuntimeContext]] = {}

    async def __aenter__(self) -> Self:
        await super().__aenter__()
        try:
            await provision_runtime_credential(self, self._runtime_settings)
        except BaseException:
            await self.aclose()
            raise
        return self

    async def upsert_message(
        self,
        user_id: str,
        session_id: str,
        msg: Msg,
    ) -> None:
        """固定 reply/run 关联，确认可读后再异步投递持久化回执。"""

        if msg.role != "assistant" or msg.finished_reason is None:
            await super().upsert_message(user_id, session_id, msg)
            return
        context, setup_fallback = await self._message_context(session_id, msg.id)
        await super().upsert_message(user_id, session_id, msg)
        stored = await super().get_message(user_id, session_id, msg.id)
        if stored is None or stored.role != "assistant" or stored.finished_reason != msg.finished_reason:
            self._schedule_receipt(
                self._persistence_failed_receipt(
                    context,
                    msg.id,
                    "AgentScope Message commit was not readable with its terminal reason",
                ),
                context,
            )
            raise RuntimeError("AgentScope committed Message is not readable")

        batch = self._pending_batches.setdefault((session_id, context.run_id), {})
        batch[msg.id] = context
        self._schedule_receipt(self._message_receipt(context, stored), context)

        # ChatService 的 setup/assembly failure 直接持久化合成错误 Message，
        # 不会再调用 update_session_state。缺少 middleware 绑定是该路径的
        # 可验证特征；用 singleton marker 收口，并由 AgentGov 标 observation_incomplete。
        if setup_fallback and stored.error is not None:
            await self._complete_batch(session_id, context.run_id)

    async def update_session_state(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        state: AgentState,
    ) -> None:
        """Session state commit 是一批 terminal Messages 的稳定结束标记。"""

        await super().update_session_state(user_id, agent_id, session_id, state)
        for batch_session_id, run_id in tuple(self._pending_batches):
            if batch_session_id == session_id:
                await self._complete_batch(session_id, run_id)

    async def set_session_team_id(
        self,
        user_id: str,
        session_id: str,
        team_id: str | None,
    ) -> None:
        """在 AgentScope 唤醒新 Team worker 前同步建立 AgentGov 绑定。"""

        await super().set_session_team_id(user_id, session_id, team_id)
        if team_id is not None:
            await register_team_child_session(
                self,
                self._runtime_settings,
                user_id=user_id,
                child_session_id=session_id,
                team_id=team_id,
                transport=self._receipt_transport,
            )

    async def aclose(self) -> None:
        await self._flush_receipts()
        for batch in self._pending_batches.values():
            contexts = list(batch.values())
            if contexts:
                discard_reply_contexts(contexts[0], list(batch))
        self._pending_batches.clear()
        if self._trace_registry is not None:
            self._trace_registry.close_all()
        await super().aclose()

    async def _message_context(self, session_id: str, reply_id: str) -> tuple[RuntimeContext, bool]:
        context = take_reply_context(session_id, reply_id)
        if context is not None:
            return context, False
        context = await self._fetch_context(session_id)
        bind_reply_context(context, reply_id)
        taken = take_reply_context(session_id, reply_id)
        if taken is None:  # pragma: no cover - 同一 event loop 中不可达
            raise RuntimeError("Runtime context binding disappeared before persistence")
        return taken, True

    async def _fetch_context(self, session_id: str) -> RuntimeContext:
        settings = self._runtime_settings
        async with httpx.AsyncClient(
            base_url=settings.agentgov_api_base_url,
            timeout=settings.request_timeout_seconds,
            transport=self._receipt_transport,
        ) as client:
            return await fetch_runtime_context(client, settings, session_id)

    async def _complete_batch(self, session_id: str, run_id: str) -> None:
        batch = self._pending_batches.pop((session_id, run_id), None)
        if not batch:
            return
        contexts = list(batch.values())
        context = max(contexts, key=lambda value: value.team_generation)
        if any(not self._same_run_context(value, context) for value in contexts):
            raise RuntimeError("One AgentScope persistence batch crossed AgentGov runs")
        # RuntimeContext 是 reply 开始时的快照；其 team_generation 可能包含
        # persistence marker 之后才入队的消息。Team Session 必须改用共享
        # MessageBus 记录的“本 Session 已实际 drain”代际，不能读取控制面的
        # 全局最新值，否则会过度确认尚未处理的 TeamSay。
        if self._message_bus is not None:
            session = await super().get_session(RUNTIME_USER_ID, "", session_id)
            if session is not None and session.team_id is not None:
                context = context.model_copy(
                    update={
                        "team_generation": self._message_bus.processed_team_generation(
                            context,
                        ),
                    },
                )
        reply_ids = list(batch)
        self._schedule_receipt(self._session_persisted_receipt(context, reply_ids), context)
        for reply_id, reply_context in batch.items():
            discard_reply_contexts(reply_context, [reply_id])

    @staticmethod
    def _same_run_context(left: RuntimeContext, right: RuntimeContext) -> bool:
        return left.model_dump(exclude={"team_generation"}) == right.model_dump(exclude={"team_generation"})

    def _schedule_receipt(self, receipt: RuntimeReceipt, context: RuntimeContext) -> None:
        task = asyncio.create_task(
            self._deliver_receipt(receipt, context),
            name=f"agentgov-{receipt.type.lower()}-{receipt.event_id[:12]}",
        )
        self._receipt_tasks.add(task)
        task.add_done_callback(self._receipt_tasks.discard)

    async def _deliver_receipt(
        self,
        receipt: RuntimeReceipt,
        context: RuntimeContext,
    ) -> RuntimeReceiptAck | None:
        settings = self._runtime_settings
        attempt = 0
        while True:
            try:
                async with httpx.AsyncClient(
                    base_url=settings.agentgov_api_base_url,
                    timeout=settings.request_timeout_seconds,
                    transport=self._receipt_transport,
                ) as client:
                    acknowledgement = await post_runtime_receipt(client, settings, receipt)
                if acknowledgement.run_id != context.run_id:
                    raise RuntimeError("AgentGov receipt acknowledgement changed run identity")
                if acknowledgement.status in _TERMINAL_ACK_STATUSES and self._trace_registry is not None:
                    self._trace_registry.finish_run(
                        context,
                        terminal_reason=acknowledgement.terminal_reason or acknowledgement.status,
                        failed=acknowledgement.status != "succeeded",
                        runtime_version=settings.runtime_version,
                        agentscope_version=_AGENTSCOPE_VERSION,
                    )
                return acknowledgement
            except asyncio.CancelledError:
                raise
            except Exception:
                attempt += 1
                if attempt % settings.receipt_retry_attempts == 0:
                    logger.exception(
                        "%s receipt is still pending after %d attempts for session=%s run=%s event=%s",
                        receipt.type,
                        attempt,
                        receipt.session_id,
                        receipt.run_id,
                        receipt.event_id,
                    )
                await asyncio.sleep(
                    settings.receipt_retry_backoff_seconds * (2 ** min(attempt - 1, settings.receipt_retry_attempts - 1)),
                )

    @staticmethod
    def _message_receipt(context: RuntimeContext, msg: Msg) -> RuntimeReceipt:
        event_id = hashlib.sha256(
            f"MESSAGE_PERSISTED\n{context.session_id}\n{msg.id}".encode(),
        ).hexdigest()
        receipt_id = hashlib.sha256(
            f"{context.run_id}\n{context.session_id}\n{event_id}".encode(),
        ).hexdigest()
        finished_reason = msg.finished_reason.value if msg.finished_reason is not None else None
        error_type = getattr(msg.error.type, "value", msg.error.type) if msg.error is not None else None
        error = {"type": str(error_type)} if error_type is not None else None
        return RuntimeReceipt(
            receipt_id=receipt_id,
            event_id=event_id,
            run_id=context.run_id,
            session_id=context.session_id,
            reply_id=msg.id,
            trace_id=context.trace_id,
            type="MESSAGE_PERSISTED",
            payload={
                "message_id": msg.id,
                "message_persisted": True,
                "finished_reason": finished_reason,
                "error": error,
                "trace_complete": False,
            },
        )

    @staticmethod
    def _session_persisted_receipt(context: RuntimeContext, reply_ids: list[str]) -> RuntimeReceipt:
        identity = "\n".join(("SESSION_PERSISTED", context.run_id, context.session_id, *reply_ids))
        event_id = hashlib.sha256(identity.encode()).hexdigest()
        receipt_id = hashlib.sha256(f"{context.run_id}\n{context.session_id}\n{event_id}".encode()).hexdigest()
        return RuntimeReceipt(
            receipt_id=receipt_id,
            event_id=event_id,
            run_id=context.run_id,
            session_id=context.session_id,
            reply_id=None,
            trace_id=context.trace_id,
            type="SESSION_PERSISTED",
            payload={
                "reply_ids": reply_ids,
                "message_count": len(reply_ids),
                "team_generation": context.team_generation,
            },
        )

    @staticmethod
    def _persistence_failed_receipt(context: RuntimeContext, reply_id: str, error: str) -> RuntimeReceipt:
        identity = f"PERSISTENCE_FAILED\n{context.run_id}\n{context.session_id}\n{reply_id}"
        event_id = hashlib.sha256(identity.encode()).hexdigest()
        receipt_id = hashlib.sha256(f"{context.run_id}\n{context.session_id}\n{event_id}".encode()).hexdigest()
        return RuntimeReceipt(
            receipt_id=receipt_id,
            event_id=event_id,
            run_id=context.run_id,
            session_id=context.session_id,
            reply_id=reply_id,
            trace_id=context.trace_id,
            type="PERSISTENCE_FAILED",
            payload={"error": {"type": "persistence"}, "message_persisted": False},
        )

    async def _flush_receipts(self) -> None:
        tasks = tuple(self._receipt_tasks)
        if not tasks:
            return
        _, pending = await asyncio.wait(
            tasks,
            timeout=self._runtime_settings.receipt_flush_timeout_seconds,
        )
        if pending:
            logger.error(
                "Cancelling %d unflushed AgentGov persistence receipt(s)",
                len(pending),
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
