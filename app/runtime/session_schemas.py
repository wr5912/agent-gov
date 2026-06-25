"""Pydantic response models for the API session mappings and session-history endpoints.

Kept out of ``schemas.py`` to hold that module under the architecture size threshold and to
group the session-facing contracts in one place.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from .json_types import JsonObject


class SessionInfo(BaseModel):
    session_id: str
    sdk_session_id: Optional[str] = None
    agent_id: Optional[str] = None
    created_at: str
    updated_at: str
    title: Optional[str] = None
    turns: int = 0
    metadata: JsonObject = Field(default_factory=dict)


class SessionDeleteResponse(BaseModel):
    deleted: bool
    session_id: str


class SessionMessagesResponse(BaseModel):
    """A session's conversation history, projected from the SDK session transcript.

    ``messages`` are SDK ``SessionMessage`` projections in transcript order: each has
    ``uuid`` / ``role`` (user|assistant) / ``parent_tool_use_id`` / ``blocks`` (Anthropic block
    list: thinking/text/tool_use/tool_result). ``subagents`` carries each subagent's messages,
    linkable to a main-session ``tool_use`` via ``parent_tool_use_id``.
    """

    session_id: str
    sdk_session_id: Optional[str] = None
    title: Optional[str] = None
    messages: list[JsonObject] = Field(default_factory=list)
    subagents: list[JsonObject] = Field(default_factory=list)
