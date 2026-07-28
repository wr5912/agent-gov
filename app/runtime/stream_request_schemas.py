from __future__ import annotations

from pydantic import Field

from .schemas import ChatRequest


class ChatStreamRequest(ChatRequest):
    """Legacy Chat SSE request with a stream-only derived-event opt-in."""

    with_speech_summary: bool = Field(
        default=False,
        description="Emit best-effort agentgov.speech_summary events before the terminal done event.",
    )


class ClaudeSdkEventsRequest(ChatRequest):
    """SDK-native SSE request; kept separate from shared Chat/raw schemas."""

    with_speech_summary: bool = Field(
        default=False,
        description="Emit best-effort agentgov.speech_summary events alongside native SDK messages.",
    )
