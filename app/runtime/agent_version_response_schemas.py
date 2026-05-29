from __future__ import annotations

from typing import Optional

from pydantic import Field

from app.runtime.schemas import ExtensibleResponse


class AgentVersionSummaryResponse(ExtensibleResponse):
    agent_version_id: str
    parent_version_id: Optional[str] = None
    created_at: str
    reason: str
    rollback_of_version_id: Optional[str] = None
    source_proposal_ids: list[str] = Field(default_factory=list)
    note: Optional[str] = None
    agent_yaml_version: Optional[str] = None
    snapshot_policy_version: Optional[str] = None
    bundle_sha256: Optional[str] = None
    bundle_path: Optional[str] = None
    manifest_path: Optional[str] = None
    file_count: Optional[int] = None
    entry_count: Optional[int] = None
    total_bytes: Optional[int] = None


class AgentVersionFileEntryResponse(ExtensibleResponse):
    path: str
    type: str
    sha256: Optional[str] = None
    size: Optional[int] = None
    mode: Optional[int] = None
    mtime: Optional[int] = None
    link_target: Optional[str] = None


class AgentVersionIncludedRootResponse(ExtensibleResponse):
    name: str
    path: str
    mode: str


class AgentVersionExcludedPathResponse(ExtensibleResponse):
    path: str
    reason: str


class AgentVersionSkippedPathResponse(ExtensibleResponse):
    path: str
    reason: str


class AgentVersionRelatedDataResponse(ExtensibleResponse):
    data_dir: Optional[str] = None
    runtime_db_path: Optional[str] = None


class AgentVersionManifestResponse(AgentVersionSummaryResponse):
    included_roots: list[AgentVersionIncludedRootResponse] = Field(default_factory=list)
    excluded_paths: list[AgentVersionExcludedPathResponse] = Field(default_factory=list)
    skipped_paths: list[AgentVersionSkippedPathResponse] = Field(default_factory=list)
    files: list[AgentVersionFileEntryResponse] = Field(default_factory=list)
    related_data: AgentVersionRelatedDataResponse = Field(default_factory=AgentVersionRelatedDataResponse)


class AgentVersionDiffEntryResponse(ExtensibleResponse):
    path: str
    before: Optional[AgentVersionFileEntryResponse] = None
    after: Optional[AgentVersionFileEntryResponse] = None


class AgentVersionDiffResponse(ExtensibleResponse):
    from_version_id: str
    to_version_id: str
    added: list[AgentVersionFileEntryResponse] = Field(default_factory=list)
    modified: list[AgentVersionDiffEntryResponse] = Field(default_factory=list)
    deleted: list[AgentVersionFileEntryResponse] = Field(default_factory=list)
    unchanged_count: int = 0


class AgentVersionFileDiffResponse(ExtensibleResponse):
    from_version_id: str
    to_version_id: str
    path: str
    archive_path: str
    status: str
    before: Optional[AgentVersionFileEntryResponse] = None
    after: Optional[AgentVersionFileEntryResponse] = None
    unified_diff: str = ""
    is_text: bool = False
    truncated: bool = False
    reason: Optional[str] = None


class AgentVersionRestoreResponse(ExtensibleResponse):
    restored_from_version: AgentVersionSummaryResponse
    pre_restore_version: AgentVersionSummaryResponse
    current_version: AgentVersionSummaryResponse
    requires_runtime_restart: bool = True
