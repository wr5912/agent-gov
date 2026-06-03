from __future__ import annotations

from typing import Optional

from pydantic import ConfigDict, Field, field_validator

from .base import StrictRuntimeRecord


class AgentChangeSetDiffSummaryRecord(StrictRuntimeRecord):
    added: int = Field(default=0, ge=0)
    modified: int = Field(default=0, ge=0)
    deleted: int = Field(default=0, ge=0)
    unchanged_count: int = Field(default=0, ge=0)


class AgentChangeSetProjectionRecord(StrictRuntimeRecord):
    """Embedded Agent change set snapshot used by task/application records."""

    model_config = ConfigDict(extra="allow")

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
    diff_summary: AgentChangeSetDiffSummaryRecord = Field(default_factory=AgentChangeSetDiffSummaryRecord)
    latest_eval_run_id: Optional[str] = None
    latest_release_id: Optional[str] = None

    @field_validator("diff_summary", mode="before")
    @classmethod
    def normalize_diff_summary(cls, value: object) -> object:
        if not isinstance(value, dict):
            return {}
        return {
            "added": int(value.get("added") or 0),
            "modified": int(value.get("modified") or 0),
            "deleted": int(value.get("deleted") or 0),
            "unchanged_count": int(value.get("unchanged_count") or 0),
        }
