from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.runtime.json_types import JsonObject
from app.runtime.schemas import ChatResponse


class AgentTestDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: Literal["warning", "error"]
    code: str
    path: str | None = None
    message: str


class AgentTestSuiteSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    commit_sha: str
    tests_directory_present: bool
    readme_present: bool
    test_file_count: int
    test_files: list[str] = Field(default_factory=list)
    suite_digest: str | None = None
    diagnostics: list[AgentTestDiagnostic] = Field(default_factory=list)

    @property
    def runnable(self) -> bool:
        return self.tests_directory_present and self.test_file_count > 0 and not any(item.level == "error" for item in self.diagnostics)


class AgentTestRunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    commit_sha: str | None = Field(default=None, description="Omit to pin the current active commit when this request is created.")


class AgentTestRunItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodeid: str
    outcome: str
    phase: str
    duration_seconds: float | None = None
    detail: str | None = None


class AgentTestRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    test_run_id: str
    agent_id: str
    commit_sha: str
    change_set_id: str | None = None
    source: str
    status: Literal["queued", "running", "passed", "failed", "error", "cancelled", "interrupted"]
    cancel_requested: bool = False
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    duration_seconds: float | None = None
    exit_code: int | None = None
    suite_digest: str | None = None
    command: list[str] = Field(default_factory=list)
    suite: JsonObject = Field(default_factory=dict)
    report: JsonObject = Field(default_factory=dict)
    items: list[AgentTestRunItemResponse] = Field(default_factory=list)
    invocations: list[JsonObject] = Field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    error: JsonObject = Field(default_factory=dict)


class AgentTestSessionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    commit_sha: str | None = Field(default=None, description="Omit to pin the current active commit once when the session is created.")
    change_set_id: str | None = None


class AgentTestSessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    test_session_id: str
    agent_id: str
    commit_sha: str
    change_set_id: str | None = None
    created_at: str


class AgentTestMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1)
    metadata: JsonObject = Field(default_factory=dict)


class AgentTestMessageResponse(ChatResponse):
    pass
