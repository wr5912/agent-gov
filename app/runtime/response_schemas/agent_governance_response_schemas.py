from __future__ import annotations

from typing import Literal, Optional, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.runtime.agent_git_read_helpers import AgentGitFileMode
from app.runtime.json_types import JsonObject
from app.runtime.protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from app.runtime.schemas import ExtensibleResponse
from app.runtime.state_machines import AgentChangeSetStatus, AgentReleaseStatus

AgentRepositoryHealthStatus: TypeAlias = Literal["active", "degraded"]
AgentGitFileDiffStatus: TypeAlias = Literal[
    "missing",
    "added",
    "deleted",
    "unchanged",
    "modified",
    "binary_or_too_large",
]


class AgentRepositoryStatusResponse(ExtensibleResponse):
    schema_version: str = "agent-repository-status/v1"
    provider: str
    repository_name: str
    repository_dir: str
    worktrees_dir: str
    releases_dir: str
    status: AgentRepositoryHealthStatus
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
    mode: AgentGitFileMode = Field(description="Git tree 中记录的精确普通文件 mode。")
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
    status: AgentGitFileDiffStatus
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

    @model_validator(mode="before")
    @classmethod
    def hide_internal_coordination_fields(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        public_value = dict(value)
        for field in ("before", "after"):
            if isinstance(public_value.get(field), dict):
                snapshot = dict(public_value[field])
                snapshot.pop("publication_intent", None)
                public_value[field] = snapshot
        return public_value


class AgentPublicationErrorResponse(BaseModel):
    detail: str
    updated_at: str


class AgentChangeSetApprovalEvidenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_commit_sha: str
    diff_digest: str
    test_run_id: str
    suite_digest: str
    review_digest: str
    reviewed_file_count: int


class AgentChangeSetPublicationEvidenceResponse(BaseModel):
    """公开发布恢复所需的不可变证据，不暴露内部操作人与备注。"""

    model_config = ConfigDict(extra="forbid")

    candidate_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    diff_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    test_run_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    suite_digest: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    tag_name: str = Field(min_length=1)
    force: bool

    @model_validator(mode="after")
    def validate_test_evidence_mode(self) -> AgentChangeSetPublicationEvidenceResponse:
        has_test_evidence = self.test_run_id is not None and self.suite_digest is not None
        if (self.test_run_id is None) != (self.suite_digest is None):
            raise ValueError("publication evidence test identity is incomplete")
        if self.force == has_test_evidence:
            raise ValueError("publication evidence test identity does not match force mode")
        return self


class AgentChangeSetResponse(ExtensibleResponse):
    schema_version: str = "agent-change-set/v1"
    change_set_id: str
    agent_id: str = DEFAULT_BUSINESS_AGENT_ID
    created_at: str
    updated_at: str
    status: AgentChangeSetStatus
    execution_job_id: Optional[str] = None
    base_commit_sha: str
    candidate_commit_sha: Optional[str] = None
    branch_name: str
    worktree_path: str
    title: Optional[str] = None
    note: Optional[str] = None
    diff_summary: JsonObject = Field(default_factory=dict)
    approval_evidence: Optional[AgentChangeSetApprovalEvidenceResponse] = None
    publication_evidence: Optional[AgentChangeSetPublicationEvidenceResponse] = None
    latest_test_run_id: Optional[str] = None
    latest_test_run: Optional[JsonObject] = None
    latest_release_id: Optional[str] = None
    source_improvement_id: Optional[str] = None
    source_attribution_id: Optional[str] = None
    source_attribution_status: Optional[str] = None
    publication_provenance_blocker: Optional[str] = None
    publication_blocker: Optional[str] = None
    publication_error: Optional[AgentPublicationErrorResponse] = None
    candidate_evidence_epoch: Optional[str] = None
    evidence_not_before: Optional[str] = None
    legacy_evidence_migration: Optional[JsonObject] = None
    legacy_publication_quarantine: Optional[JsonObject] = None
    legacy_publication_identity: Optional[JsonObject] = None
    worktree_cleanup_pending: bool = False
    worktree_cleanup: Optional[JsonObject] = None

    @model_validator(mode="before")
    @classmethod
    def hide_internal_publication_intent(cls, value: object) -> object:
        if isinstance(value, dict) and "publication_intent" in value:
            public_value = dict(value)
            public_value.pop("publication_intent", None)
            return public_value
        return value


class AgentReleaseResponse(ExtensibleResponse):
    schema_version: str = "agent-release/v1"
    release_id: str
    agent_id: str = DEFAULT_BUSINESS_AGENT_ID
    created_at: str
    updated_at: str
    status: AgentReleaseStatus
    tag_name: str
    commit_sha: str
    previous_commit_sha: Optional[str] = None
    source_improvement_id: Optional[str] = None
    change_set_id: Optional[str] = None
    source_feedback_case_ids: list[str] = Field(default_factory=list, description="由来源改进事项的现存反馈归属派生，不复制反馈或 Git 资产。")
    rollback_of_release_id: Optional[str] = None
    archive_path: Optional[str] = None
    archive_sha256: Optional[str] = None
    runtime_agent_id: Optional[str] = None
    harness_digest: Optional[str] = None
    workspace_id: Optional[str] = None
    note: Optional[str] = None
    operator: Optional[str] = None
    force_published: bool = False
    force_publication_blocker: Optional[str] = None
    force_publish_reason: Optional[str] = None


class AgentChangeSetCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_commit_sha: Optional[str] = None
    title: Optional[str] = None
    note: Optional[str] = None


class AgentChangeSetActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operator: str = "runtime"
    note: Optional[str] = None


class AgentChangeSetReviewedFileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=4096)
    detail_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class AgentChangeSetApproveRequest(AgentChangeSetActionRequest):
    candidate_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    diff_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    test_run_id: str = Field(min_length=1, max_length=128)
    suite_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewed_files: list[AgentChangeSetReviewedFileRequest] = Field(min_length=1, max_length=10_000)


class AgentChangeSetPublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_candidate_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    expected_diff_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_test_run_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    expected_suite_digest: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    operator: str = "runtime"
    tag_name: Optional[str] = None
    note: Optional[str] = None
    force: bool = False
    force_reason: Optional[str] = Field(default=None, min_length=1, max_length=2048)

    @model_validator(mode="after")
    def require_force_reason(self) -> AgentChangeSetPublishRequest:
        if self.force and not (self.force_reason or "").strip():
            raise ValueError("force_reason is required when force=true")
        if not self.force and self.force_reason is not None:
            raise ValueError("force_reason is only valid when force=true")
        if self.force and (self.expected_test_run_id is not None or self.expected_suite_digest is not None):
            raise ValueError("force publication must explicitly omit test evidence because it bypasses the test gate")
        if not self.force and (self.expected_test_run_id is None or self.expected_suite_digest is None):
            raise ValueError("normal publication requires exact test_run_id and suite_digest evidence")
        return self
