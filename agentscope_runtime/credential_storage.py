"""在 AgentScope Storage lifespan 内引导固定模型凭据。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable
from typing import Protocol, Self, TypeVar

import httpx
from agentscope.app.storage import AsyncSQLAlchemyStorage, SessionConfig, SessionOrigin, SessionRecord
from agentscope.credential import CredentialFactory
from agentscope.message import Msg
from agentscope.state import AgentState

from .context_registry import RuntimeContext, bind_reply_context, discard_reply_contexts, take_reply_context
from .receipt_middleware import AgentGovReceiptDispatcher, RuntimeReceipt, fetch_runtime_context
from .settings import RUNTIME_USER_ID, RuntimeSettings
from .team_coordination import AgentGovInMemoryMessageBus, register_team_child_session
from .workspace_reference_fence import NativeSessionWorkspaceReferences, SessionWorkspaceReferenceFence

logger = logging.getLogger(__name__)
_OperationResult = TypeVar("_OperationResult")


class WorkspaceDeletionReconciler(Protocol):
    async def reconcile(self, user_id: str) -> None: ...


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
        receipt_dispatcher: AgentGovReceiptDispatcher,
        message_bus: AgentGovInMemoryMessageBus | None = None,
        workspace_reference_fence: SessionWorkspaceReferenceFence | None = None,
    ) -> None:
        super().__init__(
            settings.database_url,
            create_tables=True,
            auto_migrate=True,
            engine_kwargs={"connect_args": {"timeout": 30}},
        )
        self._runtime_settings = settings
        self._receipt_dispatcher = receipt_dispatcher
        self._message_bus = message_bus
        self._workspace_reference_fence = workspace_reference_fence or SessionWorkspaceReferenceFence()
        self._workspace_reference_reservations: NativeSessionWorkspaceReferences | None = None
        self._workspace_deletion_reconciler: WorkspaceDeletionReconciler | None = None
        self._pending_batches: dict[tuple[str, str], dict[str, RuntimeContext]] = {}

    def bind_workspace_deletion_reconciler(self, reconciler: WorkspaceDeletionReconciler) -> None:
        """Bind post-commit cleanup without coupling SQL transactions to files."""

        self._workspace_deletion_reconciler = reconciler

    def bind_workspace_reference_reservations(self, reservations: NativeSessionWorkspaceReferences) -> None:
        """Bind the durable discovery index used for hidden Team Sessions."""

        self._workspace_reference_reservations = reservations

    async def __aenter__(self) -> Self:
        await super().__aenter__()
        try:
            await provision_runtime_credential(self, self._runtime_settings)
        except BaseException:
            await self.aclose()
            raise
        return self

    async def upsert_session(
        self,
        user_id: str,
        agent_id: str,
        config: SessionConfig,
        state: AgentState | None = None,
        session_id: str | None = None,
        origin: SessionOrigin | None = None,
        source: str | None = None,
        source_schedule_id: str | None = None,
        source_chat_id: str | None = None,
        source_chat_name: str | None = None,
        source_channel_id: str | None = None,
    ) -> SessionRecord:
        """Serialize public Session references against Workspace retirement."""

        async with self._workspace_reference_fence.hold():
            if config.workspace_id is not None:
                self._workspace_reference_fence.require_writable(config.workspace_id)
            effective_session_id = (
                session_id
                or SessionRecord(
                    user_id=user_id,
                    agent_id=agent_id,
                    config=config,
                ).id
            )
            reservations = self._workspace_reference_reservations
            if reservations is not None:
                await reservations.reserve(
                    user_id,
                    agent_id,
                    effective_session_id,
                    config.workspace_id,
                )
            return await super().upsert_session(
                user_id,
                agent_id,
                config,
                state,
                effective_session_id,
                origin,
                source,
                source_schedule_id,
                source_chat_id,
                source_chat_name,
                source_channel_id,
            )

    async def delete_session(self, user_id: str, agent_id: str, session_id: str) -> bool:
        return await self._delete_and_reconcile(
            user_id,
            super().delete_session(user_id, agent_id, session_id),
        )

    async def delete_agent(self, user_id: str, agent_id: str) -> bool:
        return await self._delete_and_reconcile(
            user_id,
            super().delete_agent(user_id, agent_id),
        )

    async def delete_team(self, user_id: str, team_id: str) -> bool:
        return await self._delete_and_reconcile(
            user_id,
            super().delete_team(user_id, team_id),
        )

    async def delete_schedule(self, user_id: str, schedule_id: str) -> bool:
        return await self._delete_and_reconcile(
            user_id,
            super().delete_schedule(user_id, schedule_id),
        )

    async def _delete_and_reconcile(self, user_id: str, operation: Awaitable[bool]) -> bool:
        deleted, cancelled = await self._finish_awaitable_despite_cancellation(operation)
        reconciler = self._workspace_deletion_reconciler
        if reconciler is None:
            if cancelled:
                raise asyncio.CancelledError
            return deleted
        try:
            _, reconcile_cancelled = await self._finish_awaitable_despite_cancellation(
                reconciler.reconcile(user_id),
            )
            cancelled = cancelled or reconcile_cancelled
        except Exception as exc:
            logger.warning(
                "workspace_reclaim stage=storage_delete result=deferred error_type=%s",
                type(exc).__name__,
            )
        if cancelled:
            raise asyncio.CancelledError
        return deleted

    @staticmethod
    async def _finish_awaitable_despite_cancellation(
        operation: Awaitable[_OperationResult],
    ) -> tuple[_OperationResult, bool]:
        task = asyncio.ensure_future(operation)
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        return task.result(), cancelled

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
            )

    async def aclose(self) -> None:
        for batch in self._pending_batches.values():
            contexts = list(batch.values())
            if contexts:
                discard_reply_contexts(contexts[0], list(batch))
        self._pending_batches.clear()
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
            trust_env=False,
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
        self._receipt_dispatcher.schedule(receipt, context)

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
