from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class AgentConfigFileResponse(BaseModel):
    agent_id: str
    path: str
    container_path: str
    exists: bool
    content: str = ""
    sha256: Optional[str] = None
    size_bytes: int = 0
    content_type: str = "application/json"


class AgentConfigFileUpdateRequest(BaseModel):
    content: str = Field(description="New UTF-8 file content.")
    expected_sha256: Optional[str] = Field(
        default=None,
        description="Current file sha256 returned by GET; rejects stale edits when mismatched.",
    )


class AgentConfigFileUpdateResponse(AgentConfigFileResponse):
    existing_sessions_unchanged: bool = True
