"""AgentScope 原生 SSE 透传契约。"""

from __future__ import annotations

from typing import Final

OpenApiObject = dict[str, object]

RUNTIME_STREAM_PATH: Final = "/api/runtime/sessions/{session_id}/stream"
_READINESS_COMMENT: Final = ":\n\n"

_NATIVE_EVENT_EXAMPLE: Final = 'data: {"id":"evt_01","type":"TEXT_BLOCK_DELTA","reply_id":"reply_01","delta":"正在分析"}\n\n'


def runtime_sse_contract() -> OpenApiObject:
    """返回传输级约束；事件集合由 AgentScope 版本定义，AgentGov 不复制其 schema。"""

    return {
        "mode": "readiness-comment-then-raw-byte-proxy",
        "source": "AgentScope AgentEvent SSE",
        "readiness": {
            "frame": _READINESS_COMMENT,
            "semantics": "sse-comment",
            "position": "before-upstream",
        },
        "upstream_bytes": "pass-through",
        "event_family": "open",
        "unknown_events": "pass-through",
        "terminal_event": "REPLY_END",
        "recovery": {
            "transient_delta_replay": False,
            "final_state_sources": ["messages", "status"],
        },
        "example": _READINESS_COMMENT + _NATIVE_EVENT_EXAMPLE,
    }
