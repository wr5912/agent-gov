"""AgentScope Runtime 内部边界类型。"""

from __future__ import annotations

from typing import TypeAlias

from pydantic import JsonValue

JsonObject: TypeAlias = dict[str, JsonValue]
MiddlewareInput: TypeAlias = dict[str, object]
