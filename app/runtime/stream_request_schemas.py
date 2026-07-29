from __future__ import annotations

from pydantic import Field

from .schemas import NON_BLANK_TEXT_PATTERN, ChatRequest


class AgentTargetedChatRequest(ChatRequest):
    """Public native Chat request that always names the business Agent."""

    agent_id: str = Field(
        ...,
        min_length=1,
        pattern=NON_BLANK_TEXT_PATTERN,
        description="Registered business agent to run. Must contain at least one non-whitespace character.",
    )


class ChatStreamRequest(AgentTargetedChatRequest):
    """Legacy Chat SSE request with a stream-only derived-event opt-in."""

    with_speech_summary: bool = Field(
        default=False,
        description="Emit best-effort agentgov.speech_summary events before the terminal done event.",
    )


class ClaudeSdkEventsRequest(AgentTargetedChatRequest):
    """SDK-native SSE request; kept separate from shared Chat/raw schemas."""

    with_speech_summary: bool = Field(
        default=False,
        description="Emit best-effort agentgov.speech_summary events alongside native SDK messages.",
    )
