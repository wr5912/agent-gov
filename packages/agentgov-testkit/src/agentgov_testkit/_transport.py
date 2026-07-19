from __future__ import annotations

from contextlib import suppress
from typing import cast

import httpx

from .result import JsonObject


class AgentGovTestkitError(RuntimeError):
    """Raised when AgentGov cannot create or execute a test session."""


def post_message(
    *,
    api_base: str,
    test_session_id: str,
    message: str,
    metadata: JsonObject,
    api_key: str | None,
    timeout_seconds: float,
) -> JsonObject:
    try:
        response = httpx.post(
            f"{api_base}/api/agent-test-sessions/{test_session_id}/messages",
            json={"message": message, "metadata": metadata},
            headers=_headers(api_key),
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        payload: object = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise AgentGovTestkitError(f"AgentGov test invocation failed: {exc}") from exc
    if not isinstance(payload, dict) or not all(isinstance(key, str) for key in payload):
        raise AgentGovTestkitError("AgentGov test invocation returned a non-object response")
    return cast(JsonObject, payload)


def delete_session(*, api_base: str, test_session_id: str, api_key: str | None) -> None:
    with suppress(httpx.HTTPError):
        httpx.delete(
            f"{api_base}/api/agent-test-sessions/{test_session_id}",
            headers=_headers(api_key),
            timeout=10.0,
        )


def _headers(api_key: str | None) -> httpx.Headers:
    headers = httpx.Headers()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers
