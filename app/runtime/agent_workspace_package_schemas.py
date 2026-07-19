"""Business Agent workspace package API contracts.

The package endpoints intentionally expose one small synchronous workflow:
export the current Git-backed workspace, import a complete replacement, and
restore a prior commit as a new commit.  Operation state is not persisted.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agent_testing.schemas import AgentTestDiagnostic
from app.runtime.agent_governance_schemas import AgentSummaryResponse


class WorkspaceImportResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["created", "overwritten", "unchanged"]
    agent: AgentSummaryResponse
    previous_commit_sha: str | None = None
    current_commit_sha: str
    package_sha256: str
    tree_sha256: str
    rollback_target_commit_sha: str | None = None
    activation_mode: Literal["next_turn"] = "next_turn"
    import_record_id: str
    test_suite_status: Literal["ready", "warning", "invalid"]
    test_file_count: int
    test_suite_warnings: list[AgentTestDiagnostic] = Field(default_factory=list)


class WorkspaceRestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_commit_sha: str = Field(description="Historical workspace commit whose tree will be restored.")
    expected_current_commit_sha: str = Field(description="Current workspace HEAD used as an optimistic concurrency guard.")
    reason: str | None = Field(default=None, max_length=512)


class WorkspaceRestoreResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["restored"] = "restored"
    agent: AgentSummaryResponse
    previous_commit_sha: str
    current_commit_sha: str
    restored_tree_commit_sha: str
    rollback_target_commit_sha: str
    activation_mode: Literal["next_turn"] = "next_turn"
