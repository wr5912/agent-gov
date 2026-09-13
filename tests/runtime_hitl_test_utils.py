from __future__ import annotations

from typing import TypedDict

from app.runtime_gateway.hitl import tool_call_fingerprint


class FingerprintedHITLPayload(TypedDict):
    tool_calls: list[dict[str, object]]


def fingerprinted_hitl_payload(
    tool_calls: list[dict[str, object]],
    *,
    default_state: str | None = None,
) -> FingerprintedHITLPayload:
    """Build the fingerprint-only payload emitted by the real Runtime boundary."""

    return {
        "tool_calls": [
            tool_call_fingerprint(
                tool_call,
                default_state=default_state,
            ).model_dump(mode="json")
            for tool_call in tool_calls
        ],
    }
