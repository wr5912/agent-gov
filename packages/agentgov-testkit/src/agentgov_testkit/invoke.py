from __future__ import annotations

import os

from ._reporting import record_invocation
from ._transport import AgentGovTestkitError, delete_session, post_message
from .result import AgentInvocation, JsonObject


class AgentTestAgent:
    def __init__(
        self,
        *,
        api_base: str,
        test_session_id: str,
        resolved_commit_sha: str | None,
        api_key: str | None = None,
        timeout_seconds: float = 300.0,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.test_session_id = test_session_id
        self.resolved_commit_sha = resolved_commit_sha
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def run(self, message: str, *, metadata: JsonObject | None = None) -> AgentInvocation:
        return invoke_agent(
            message,
            metadata=metadata,
            api_base=self.api_base,
            api_key=self.api_key,
            test_session_id=self.test_session_id,
            timeout_seconds=self.timeout_seconds,
        )

    def close(self) -> None:
        delete_session(
            api_base=self.api_base,
            test_session_id=self.test_session_id,
            api_key=self.api_key,
        )


def invoke_agent(
    message: str,
    *,
    metadata: JsonObject | None = None,
    api_base: str | None = None,
    api_key: str | None = None,
    test_session_id: str | None = None,
    timeout_seconds: float = 300.0,
) -> AgentInvocation:
    clean_message = message.strip()
    if not clean_message:
        raise ValueError("message must not be empty")
    resolved_api_base = (api_base or os.getenv("AGENTGOV_API_BASE") or "").rstrip("/")
    resolved_session_id = test_session_id or os.getenv("AGENTGOV_TEST_SESSION_ID")
    resolved_api_key = api_key if api_key is not None else os.getenv("AGENTGOV_API_KEY")
    if not resolved_api_base:
        raise AgentGovTestkitError("AGENTGOV_API_BASE is required")
    if not resolved_session_id:
        raise AgentGovTestkitError("AGENTGOV_TEST_SESSION_ID is required")
    payload = post_message(
        api_base=resolved_api_base,
        test_session_id=resolved_session_id,
        message=clean_message,
        metadata=metadata or {},
        api_key=resolved_api_key,
        timeout_seconds=timeout_seconds,
    )
    raw_errors = payload.get("errors")
    errors = tuple(str(item) for item in raw_errors if item is not None) if isinstance(raw_errors, list) else ()
    result = AgentInvocation(
        text=str(payload.get("answer") or ""),
        run_id=_optional_text(payload.get("run_id")),
        session_id=_optional_text(payload.get("session_id")),
        agent_version_id=_optional_text(payload.get("agent_version_id")),
        langfuse_trace_id=_optional_text(payload.get("langfuse_trace_id")),
        langfuse_trace_url=_optional_text(payload.get("langfuse_trace_url")),
        errors=errors,
        raw=payload,
    )
    record_invocation(result)
    return result


def _optional_text(value: object) -> str | None:
    return str(value) if value is not None and str(value) else None
