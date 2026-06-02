from __future__ import annotations

from typing import TypeAlias

from pydantic import BaseModel, ConfigDict
from pydantic.types import JsonValue


JsonObject: TypeAlias = dict[str, JsonValue]


class StrictRuntimeRecord(BaseModel):
    """Base model for internal runtime records that must not grow implicit fields."""

    model_config = ConfigDict(extra="forbid")
