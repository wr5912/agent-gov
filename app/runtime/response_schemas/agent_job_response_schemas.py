from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict

from app.runtime.json_types import JsonObject
from app.runtime.response_schemas.error_response_schemas import FeedbackJobErrorResponse


class AgentJobResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    job_id: str
    job_type: str
    scope_kind: Optional[str] = None
    scope_id: Optional[str] = None
    status: str
    profile_name: str
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    input_path: str
    raw_output_path: str
    validated_output_path: str
    error_path: str
    runtime_version: Optional[str] = None
    schema_version: Optional[str] = None
    timeout_seconds: int = 300
    retry_count: int = 0
    profile_version: Optional[JsonObject] = None
    input_json: Optional[JsonObject] = None
    raw_output_json: Optional[JsonObject] = None
    validated_output_json: Optional[JsonObject] = None
    error_json: Optional[FeedbackJobErrorResponse] = None
