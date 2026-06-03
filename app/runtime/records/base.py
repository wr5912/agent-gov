from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class StrictRuntimeRecord(BaseModel):
    """Base model for internal runtime records that must not grow implicit fields."""

    model_config = ConfigDict(extra="forbid")
