from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


@dataclass(frozen=True)
class AgentInvocation:
    text: str
    run_id: str | None
    session_id: str | None
    agent_version_id: str | None
    langfuse_trace_id: str | None
    langfuse_trace_url: str | None
    errors: tuple[str, ...]
    raw: JsonObject
