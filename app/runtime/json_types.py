from __future__ import annotations

from typing import TypeAlias

from pydantic.types import JsonValue


JsonObject: TypeAlias = dict[str, JsonValue]
