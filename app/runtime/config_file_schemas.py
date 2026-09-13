from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class AgentCandidateFileResponse(BaseModel):
    agent_id: str
    change_set_id: str
    change_set_status: str
    base_commit_sha: str
    candidate_commit_sha: str
    path: str
    exists: bool
    content: str = ""
    sha256: Optional[str] = None
    size_bytes: int = 0
    content_type: str = "application/json"
    published: bool = False


class AgentCandidateTextFileWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=4096)
    content: str = Field(description="New UTF-8 candidate file content.")
    expected_sha256: Optional[str] = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description="Current file sha256 returned by candidate GET; rejects stale edits.",
    )
    mode: int = Field(default=0o644, description="Regular-file mode; only 0644 and 0755 are accepted.")


class AgentCandidateFilesWriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_candidate_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    files: list[AgentCandidateTextFileWrite] = Field(min_length=1, max_length=100)
    operator: str = Field(default="runtime", min_length=1, max_length=256)
    note: Optional[str] = Field(default=None, max_length=2048)


class AgentCandidateFilesWriteResponse(BaseModel):
    agent_id: str
    change_set_id: str
    change_set_status: str
    base_commit_sha: str
    candidate_commit_sha: str
    changed_paths: list[str] = Field(default_factory=list)
    published: bool = False
