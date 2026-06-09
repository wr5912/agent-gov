from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from app.runtime.json_types import JsonObject
from app.runtime.schemas import ExtensibleResponse


class AgentRepositoryStatusResponse(ExtensibleResponse):
    schema_version: str = "agent-repository-status/v1"
    provider: str
    repository_name: str
    repository_dir: str
    worktrees_dir: str
    releases_dir: str
    status: str
    degraded_reason: Optional[str] = None
    service_url: Optional[str] = None
    service_public_url: Optional[str] = None
    current_commit_sha: Optional[str] = None
    current_branch: Optional[str] = None
    dirty: bool = False
    changed_file_count: int = 0
    changed_files: list[JsonObject] = Field(default_factory=list)
    file_diffs: list[JsonObject] = Field(default_factory=list)
    maintenance_active: bool = False


class AgentGitRefResponse(ExtensibleResponse):
    agent_version_id: str
    commit_sha: Optional[str] = None
    parent_version_id: Optional[str] = None
    created_at: str
    reason: str
    note: Optional[str] = None
    file_count: Optional[int] = None


class AgentGitFileEntryResponse(ExtensibleResponse):
    path: str
    type: str
    sha256: Optional[str] = None
    size: Optional[int] = None


class AgentGitDiffEntryResponse(ExtensibleResponse):
    path: str
    before: Optional[AgentGitFileEntryResponse] = None
    after: Optional[AgentGitFileEntryResponse] = None


class AgentGitDiffResponse(ExtensibleResponse):
    from_version_id: str
    to_version_id: str
    added: list[AgentGitFileEntryResponse] = Field(default_factory=list)
    modified: list[AgentGitDiffEntryResponse] = Field(default_factory=list)
    deleted: list[AgentGitFileEntryResponse] = Field(default_factory=list)
    unchanged_count: int = 0


class AgentGitFileDiffResponse(ExtensibleResponse):
    from_version_id: str
    to_version_id: str
    path: str
    archive_path: str
    status: str
    before: Optional[AgentGitFileEntryResponse] = None
    after: Optional[AgentGitFileEntryResponse] = None
    unified_diff: str = ""
    is_text: bool = False
    truncated: bool = False
    reason: Optional[str] = None


class AgentChangeSetEventResponse(ExtensibleResponse):
    event_id: str
    change_set_id: str
    action: str
    operator: str
    created_at: str
    before: JsonObject = Field(default_factory=dict)
    after: JsonObject = Field(default_factory=dict)


class AgentChangeSetResponse(ExtensibleResponse):
    schema_version: str = "agent-change-set/v1"
    change_set_id: str
    created_at: str
    updated_at: str
    status: str
    optimization_task_id: Optional[str] = None
    execution_job_id: Optional[str] = None
    base_commit_sha: str
    candidate_commit_sha: Optional[str] = None
    branch_name: str
    worktree_path: str
    title: Optional[str] = None
    note: Optional[str] = None
    diff_summary: JsonObject = Field(default_factory=dict)
    latest_eval_run_id: Optional[str] = None
    latest_eval_run: Optional[JsonObject] = None
    latest_release_id: Optional[str] = None
    publication_blocker: Optional[str] = None


class AgentReleaseResponse(ExtensibleResponse):
    schema_version: str = "agent-release/v1"
    release_id: str
    created_at: str
    updated_at: str
    status: str
    tag_name: str
    commit_sha: str
    change_set_id: Optional[str] = None
    rollback_of_release_id: Optional[str] = None
    archive_path: Optional[str] = None
    archive_sha256: Optional[str] = None
    note: Optional[str] = None


class AgentChangeSetCreateRequest(BaseModel):
    optimization_task_id: Optional[str] = None
    base_commit_sha: Optional[str] = None
    title: Optional[str] = None
    note: Optional[str] = None


class AgentChangeSetActionRequest(BaseModel):
    operator: str = "runtime"
    note: Optional[str] = None


class AgentChangeSetRegressionRunRequest(BaseModel):
    eval_case_ids: list[str] | None = None


class AgentChangeSetPublishRequest(BaseModel):
    operator: str = "runtime"
    tag_name: Optional[str] = None
    note: Optional[str] = None


class AgentReleaseRollbackRequest(BaseModel):
    operator: str = "runtime"
    note: Optional[str] = None


class AgentReleaseRestoreRequest(BaseModel):
    operator: str = "runtime"
    note: Optional[str] = None


class AgentReleaseRestoreResponse(ExtensibleResponse):
    schema_version: str = "agent-release-restore/v1"
    release: AgentReleaseResponse
    restore_result: JsonObject = Field(default_factory=dict)


class AgentRepositoryDiscardChangesRequest(BaseModel):
    paths: list[str] = Field(default_factory=list)


class AgentRepositorySnapshotRequest(BaseModel):
    operator: str = "runtime"
    note: Optional[str] = None
