from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Protocol

from pydantic import ConfigDict, Field, field_validator

from .agent_profiles import AgentRuntimeProfile
from .errors import FeedbackStoreError
from .schemas import ChatRequest

RAW_EVENTS_MEDIA_TYPE = "application/octet-stream"
RAW_EVENT_RESPONSE_HEADER_DESCRIPTIONS: Mapping[str, str] = {
    "X-AgentGov-Run-Id": "Backend-owned managed run id.",
    "X-AgentGov-Session-Id": "Backend-owned AgentGov session id.",
    "X-AgentGov-Agent-Id": "Backend-resolved registered business Agent id.",
    "X-AgentGov-Runtime-Kind": "Native Runtime implementation that produced the body.",
    "X-AgentGov-Execution-Origin": "Execution origin; managed means the normal AgentGov lifecycle ran.",
    "X-AgentGov-Native-Protocol": "Native stdout protocol carried by the response body.",
    "X-AgentGov-Runtime-Version": "Native Runtime executable version, or unknown when unavailable.",
    "X-AgentGov-Raw-Fidelity": "Raw fidelity guarantee; byte-exact means no decode or re-serialization occurred.",
}
RAW_EVENT_RESPONSE_HEADER_NAMES = tuple(RAW_EVENT_RESPONSE_HEADER_DESCRIPTIONS)


class RuntimeRawEventsRequest(ChatRequest):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(
        ...,
        description="Registered business agent to run. The Runtime implementation is selected by the server.",
    )
    stream: bool = Field(
        default=False,
        description="When true, flush raw Runtime stdout bytes as they arrive; otherwise buffer the same bytes into one response.",
    )

    @field_validator("agent_id")
    @classmethod
    def _non_blank_agent_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("agent_id must identify a registered business agent")
        return normalized


@dataclass(frozen=True)
class RuntimeRawEventMetadata:
    run_id: str
    session_id: str
    agent_id: str
    runtime_kind: str
    native_protocol: str
    runtime_version: str
    execution_origin: str = "managed"
    fidelity: str = "byte-exact"


class PreparedRuntimeRawEvents(Protocol):
    metadata: RuntimeRawEventMetadata

    async def collect(self) -> bytes: ...

    def iter_bytes(self) -> AsyncIterator[bytes]: ...

    async def aclose(self) -> None: ...


class RuntimeRawEventsBackend(Protocol):
    async def start(
        self,
        req: RuntimeRawEventsRequest,
        *,
        profile: AgentRuntimeProfile,
    ) -> PreparedRuntimeRawEvents: ...


def raw_event_response_headers(metadata: RuntimeRawEventMetadata) -> Mapping[str, str]:
    return {
        "X-AgentGov-Run-Id": metadata.run_id,
        "X-AgentGov-Session-Id": metadata.session_id,
        "X-AgentGov-Agent-Id": metadata.agent_id,
        "X-AgentGov-Runtime-Kind": metadata.runtime_kind,
        "X-AgentGov-Execution-Origin": metadata.execution_origin,
        "X-AgentGov-Native-Protocol": metadata.native_protocol,
        "X-AgentGov-Runtime-Version": metadata.runtime_version,
        "X-AgentGov-Raw-Fidelity": metadata.fidelity,
        "Cache-Control": "no-store, no-transform",
        "X-Content-Type-Options": "nosniff",
    }


class RuntimeRawEventsDisabledError(FeedbackStoreError):
    status_code = 403
    error_code = "AGENT_RUNTIME_RAW_EVENTS_DISABLED"


class RuntimeRawLimitExceededError(FeedbackStoreError):
    status_code = 413
    error_code = "AGENT_RUNTIME_RAW_LIMIT_EXCEEDED"


class RuntimeRawCaptureUnsupportedError(FeedbackStoreError):
    status_code = 501
    error_code = "AGENT_RUNTIME_RAW_CAPTURE_UNSUPPORTED"


class RuntimeRawCaptureUnavailableError(FeedbackStoreError):
    status_code = 503
    error_code = "AGENT_RUNTIME_RAW_CAPTURE_UNAVAILABLE"


class RuntimeRawPreflightError(FeedbackStoreError):
    status_code = 503
    error_code = "AGENT_RUNTIME_RAW_CAPTURE_UNAVAILABLE"

    def __init__(self, message: str, *, error_code: str | None = None) -> None:
        super().__init__(message)
        if error_code:
            self.error_code = error_code
