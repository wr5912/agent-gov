from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field
from pydantic.types import JsonValue

from app.runtime.json_types import JsonObject


class HttpErrorResponse(BaseModel):
    """Runtime HTTPException envelope returned by app-level error handlers."""

    detail: JsonValue
    error_code: str


class DomainErrorResponse(HttpErrorResponse):
    """Domain errors may add route-specific top-level diagnostic fields."""

    model_config = ConfigDict(extra="allow")


class FeedbackValidationErrorResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Optional[str] = None
    loc: list[str | int] = Field(default_factory=list)
    msg: Optional[str] = None
    input: object | None = None
    ctx: Optional[JsonObject] = None
    url: Optional[str] = None


class FeedbackJobErrorResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    error_code: Optional[str] = None
    message: Optional[str] = None
    created_at: Optional[str] = None
    job_id: Optional[str] = None
    validation_errors: list[FeedbackValidationErrorResponse] = Field(default_factory=list)
