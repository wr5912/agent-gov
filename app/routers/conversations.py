from __future__ import annotations

from collections.abc import Callable
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.concurrency import run_in_threadpool

from app.routers.sessions import _resolve_owning_profile
from app.runtime.errors import NotFoundError
from app.runtime.json_types import JsonObject
from app.runtime.openai_responses_adapter import (
    conversation_id_from_session,
    iso_to_epoch,
    public_metadata,
    session_id_from_conversation,
)
from app.runtime.openai_responses_schemas import (
    Conversation,
    ConversationCreateRequest,
    ConversationDeleted,
    ConversationItem,
    ConversationItemList,
    ConversationList,
)
from app.runtime.session_history import read_session_history
from app.runtime.session_store import LocalSession, LocalSessionStore
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_registry_store import AgentRegistryStore


def _conversation(session: LocalSession) -> Conversation:
    return Conversation(
        id=conversation_id_from_session(session.session_id) or session.session_id,
        created_at=iso_to_epoch(session.created_at),
        title=session.title,
        metadata=public_metadata(session.metadata),
    )


def _item(message: JsonObject, index: int) -> ConversationItem:
    role = message.get("role")
    blocks = message.get("blocks")
    parent = message.get("parent_tool_use_id")
    return ConversationItem(
        id=f"msg_{index}",
        role=role if isinstance(role, str) else None,
        content=blocks if isinstance(blocks, list) else [],
        parent_tool_use_id=parent if isinstance(parent, str) else None,
    )


def _offset_from_cursor(after: Optional[str]) -> int:
    """cursor ``msg_<n>`` -> 下一页 offset ``n+1``（不暴露旧 offset 契约）。"""
    if isinstance(after, str) and after.startswith("msg_"):
        try:
            return int(after[len("msg_") :]) + 1
        except ValueError:
            return 0
    return 0


async def _list_items_impl(
    conversation_id: str,
    *,
    after: Optional[str],
    limit: int,
    session_store: LocalSessionStore,
    settings: AppSettings,
    agent_registry_store: AgentRegistryStore,
) -> ConversationItemList:
    session_id = session_id_from_conversation(conversation_id)
    session = session_store.get(session_id) if session_id else None
    if session is None:
        raise NotFoundError(f"conversation {conversation_id} not found")
    if not session.sdk_session_id:
        return ConversationItemList()  # 尚无 SDK transcript -> 空历史（非 owning-agent 错误）
    workspace_dir, claude_config_dir = _resolve_owning_profile(settings, agent_registry_store, session)
    offset = _offset_from_cursor(after)
    history = await run_in_threadpool(
        read_session_history,
        sdk_session_id=session.sdk_session_id,
        workspace_dir=workspace_dir,
        claude_config_dir=claude_config_dir,
        scrub=settings.session_history_scrub,
        limit=limit,
        offset=offset,
    )
    messages = history.get("messages") or []
    items = [_item(message, offset + i) for i, message in enumerate(messages) if isinstance(message, dict)]
    return ConversationItemList(
        data=items,
        first_id=items[0].id if items else None,
        last_id=items[-1].id if items else None,
        has_more=len(items) == limit,
    )


def create_conversations_router(
    *,
    session_store: LocalSessionStore,
    settings: AppSettings,
    agent_registry_store: AgentRegistryStore,
    require_api_key: Callable,
) -> APIRouter:
    """OpenAI Conversations 接口。会话对象与 items 均投影自 SDK session/transcript，后端不另建消息副本。"""

    router = APIRouter(prefix="/v1", tags=["openai-conversations"], dependencies=[Depends(require_api_key)])

    @router.post("/conversations", response_model=Conversation, summary="Create a conversation")
    async def create_conversation(req: Optional[ConversationCreateRequest] = None) -> Conversation:
        metadata = public_metadata(req.metadata) if req else {}
        return _conversation(session_store.create(metadata=metadata))

    @router.get("/conversations", response_model=ConversationList, summary="List conversations (AgentGov extension for the session sidebar)")
    async def list_conversations() -> ConversationList:
        return ConversationList(data=[_conversation(session) for session in session_store.list()])

    @router.get("/conversations/{conversation_id}", response_model=Conversation, summary="Retrieve a conversation")
    async def get_conversation(conversation_id: str) -> Conversation:
        session_id = session_id_from_conversation(conversation_id)
        session = session_store.get(session_id) if session_id else None
        if session is None:
            raise NotFoundError(f"conversation {conversation_id} not found")
        return _conversation(session)

    @router.delete("/conversations/{conversation_id}", response_model=ConversationDeleted, summary="Delete a conversation mapping")
    async def delete_conversation(conversation_id: str) -> ConversationDeleted:
        session_id = session_id_from_conversation(conversation_id)
        deleted = bool(session_id and session_store.delete(session_id))
        return ConversationDeleted(id=conversation_id, deleted=deleted)

    @router.get(
        "/conversations/{conversation_id}/items",
        response_model=ConversationItemList,
        summary="List conversation items (projected from the SDK transcript; cursor-style after/limit/order/include)",
    )
    async def list_conversation_items(
        conversation_id: str,
        after: str | None = Query(default=None),
        limit: int = Query(default=20, ge=1, le=100),
        order: str = Query(default="asc", description="Chronological asc supported; desc reserved."),
        include: str | None = Query(default=None, description="OpenAI-shape passthrough; currently a no-op."),
    ) -> ConversationItemList:
        return await _list_items_impl(
            conversation_id,
            after=after,
            limit=limit,
            session_store=session_store,
            settings=settings,
            agent_registry_store=agent_registry_store,
        )

    return router
