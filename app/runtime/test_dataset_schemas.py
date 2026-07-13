from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from .json_types import JsonObject
from .test_dataset_state import TestDatasetLifecycleState


class TestDatasetAdoptRequest(BaseModel):
    """Adoption is derived from backend-owned improvement artifacts; no client fields are accepted."""

    model_config = ConfigDict(extra="forbid")


class TestDatasetLifecycleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_state: TestDatasetLifecycleState
    expected_revision: int = Field(ge=1)
    operator: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=2048)


class TestCaseResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    position: int = Field(ge=1)
    prompt: str
    expected_behavior: str
    checkpoints: list[str] = Field(default_factory=list)


class TestDatasetProvenanceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    regression_assessment_id: str
    regression_assessment_updated_at: str
    normalized_feedback_id: str
    normalized_feedback_updated_at: str
    attribution_id: str
    attribution_updated_at: str
    optimization_plan_id: str
    optimization_plan_updated_at: str
    execution_id: str
    execution_updated_at: str
    source_feedback_ids: list[str] = Field(default_factory=list)
    baseline_agent_version_id: str = ""
    candidate_agent_version_id: str


class TestDatasetResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: str
    agent_id: str
    owner_kind: Literal["business_agent"]
    owner_id: str
    source_improvement_id: str
    name: str
    description: str = ""
    scope: str
    revision: int = Field(ge=1)
    lifecycle_state: TestDatasetLifecycleState
    quality_tags: list[str] = Field(default_factory=list)
    provenance: TestDatasetProvenanceResponse
    cases: list[TestCaseResponse]
    created_at: str
    updated_at: str


class TestDatasetRevisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    revision_id: str
    dataset_id: str
    revision: int = Field(ge=1)
    previous_lifecycle_state: TestDatasetLifecycleState | None = None
    lifecycle_state: TestDatasetLifecycleState
    operator: str
    reason: str
    before: JsonObject = Field(default_factory=dict)
    after: JsonObject = Field(default_factory=dict)
    created_at: str


# Internal records and HTTP responses intentionally share one model: their required
# fields and lifecycle constraints are identical, so a second DTO would be a schema fork.
TestCaseRecord: TypeAlias = TestCaseResponse
TestDatasetProvenanceRecord: TypeAlias = TestDatasetProvenanceResponse
TestDatasetRecord: TypeAlias = TestDatasetResponse
TestDatasetRevisionRecord: TypeAlias = TestDatasetRevisionResponse
