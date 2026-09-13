"""Business Agent candidate import and restore API contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent_testing.schemas import AgentTestDiagnostic
from app.runtime.agent_governance_schemas import AgentSummaryResponse


class CandidateReceipt(BaseModel):
    """A Git candidate receipt; no field implies activation of the live Workspace."""

    model_config = ConfigDict(extra="forbid")

    agent: AgentSummaryResponse
    change_set_id: str
    change_set_status: str
    base_commit_sha: str
    candidate_commit_sha: str
    changed_paths: list[str] = Field(default_factory=list)
    published: Literal[False] = False


class WorkspaceImportResponse(CandidateReceipt):
    model_config = ConfigDict(extra="forbid")

    action: Literal["created", "candidate_committed", "unchanged"]
    package_sha256: str
    tree_sha256: str
    import_record_id: str
    test_suite_status: Literal["ready", "warning", "invalid"]
    test_file_count: int
    test_suite_warnings: list[AgentTestDiagnostic] = Field(default_factory=list)


class WorkspaceRestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_commit_sha: str = Field(description="Historical workspace commit whose tree will be restored.")
    expected_current_commit_sha: str = Field(description="Current workspace HEAD used as an optimistic concurrency guard.")
    reason: str | None = Field(default=None, max_length=512)


class WorkspaceRestoreResponse(CandidateReceipt):
    model_config = ConfigDict(extra="forbid")

    action: Literal["candidate_committed"] = "candidate_committed"
    restored_tree_commit_sha: str


class NativeContextConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    compression_fallback_to_truncation: bool = True
    compression_prompt: str | None = None
    compression_tool_enabled: bool = False
    context_buffer_ratio: float = Field(default=0.2, ge=0.0, le=1.0)
    max_image_num: int = Field(default=5, ge=0)
    reserve_ratio: float = Field(default=0.1, gt=0.0, lt=0.9)
    summary_template: str | None = None
    tool_result_limit: int = 50_000
    trigger_ratio: float = Field(default=0.8, gt=0.0, le=0.9)


class NativeReactConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interruption_message: str = "I notice the interruption. How can I help you?"
    interruption_raise_cancelled_error: bool = False
    max_iters: int = 50
    stop_on_reject: bool = False
    structured_output_grace_iters: int = Field(default=5, gt=0)


class NativeInviteConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invitable: bool = False
    invite_description: str | None = None

    @model_validator(mode="after")
    def require_invite_description(self) -> NativeInviteConfig:
        if self.invitable and not (self.invite_description or "").strip():
            raise ValueError("invite_description is required when invitable is true")
        return self


class NativeAgentDataInput(BaseModel):
    """Reviewed user-editable subset of AgentScope AgentData."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=256)
    system_prompt: str = "You're a helpful assistant."
    context_config: NativeContextConfig
    react_config: NativeReactConfig
    invite_config: NativeInviteConfig = Field(default_factory=NativeInviteConfig)


class NativeAgentCandidateRequest(BaseModel):
    """Agent-owned AgentScope fields plus backend-owned optimistic concurrency."""

    model_config = ConfigDict(extra="forbid")

    agent_data: NativeAgentDataInput
    expected_current_commit_sha: str | None = Field(
        default=None,
        description="Required to start a candidate for an existing Agent; omitted for a draft or candidate continuation.",
    )
    change_set_id: str | None = Field(
        default=None,
        description="Existing open candidate identifier to update; requires expected_candidate_commit_sha.",
    )
    expected_candidate_commit_sha: str | None = Field(
        default=None,
        description="Current candidate commit used as a continuation CAS; requires change_set_id.",
    )
    reason: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def require_one_candidate_base(self) -> NativeAgentCandidateRequest:
        continuation_fields = (self.change_set_id, self.expected_candidate_commit_sha)
        if (continuation_fields[0] is None) != (continuation_fields[1] is None):
            raise ValueError("change_set_id and expected_candidate_commit_sha must be provided together")
        if self.change_set_id is not None and self.expected_current_commit_sha is not None:
            raise ValueError("candidate continuation must not carry expected_current_commit_sha")
        return self


class NativeAgentCandidateSourceResponse(BaseModel):
    """Safe form source projected from an open candidate or the current live Git commit."""

    model_config = ConfigDict(extra="forbid")

    agent_data: NativeAgentDataInput
    change_set_id: str | None = None
    current_commit_sha: str


class NativeAgentCandidateResponse(CandidateReceipt):
    model_config = ConfigDict(extra="forbid")

    action: Literal["created", "candidate_committed", "unchanged"]
