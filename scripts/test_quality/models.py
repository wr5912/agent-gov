from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Identifier = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")]
NonEmpty = Annotated[str, Field(min_length=1)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Lifecycle(StrEnum):
    KEEP = "KEEP"
    PROMOTE = "PROMOTE"
    DEMOTE = "DEMOTE"
    REFACTOR = "REFACTOR"
    MERGE = "MERGE"
    QUARANTINE = "QUARANTINE"
    DELETE_CANDIDATE = "DELETE-CANDIDATE"


class CoverageThreshold(StrictModel):
    line_percent_min: Annotated[float, Field(ge=0, le=100)]
    branch_percent_min: Annotated[float, Field(ge=0, le=100)]


class FileCoverageThreshold(CoverageThreshold):
    path: NonEmpty


class CoveragePolicy(StrictModel):
    global_: CoverageThreshold = Field(alias="global")
    files: list[FileCoverageThreshold] = Field(default_factory=list)


class Owner(StrictModel):
    id: Identifier
    description: NonEmpty
    contacts: Annotated[list[NonEmpty], Field(min_length=1)]


class Capability(StrictModel):
    id: Identifier
    description: NonEmpty
    risk: Literal["low", "medium", "high", "critical"]


class Lane(StrictModel):
    id: Identifier
    description: NonEmpty
    enforcement: Literal["blocking", "shadow", "manual"]


class Classification(StrictModel):
    lifecycle: Lifecycle
    level: Literal["static", "unit", "component", "contract", "integration", "e2e", "performance", "security", "resilience"]
    purpose: Literal[
        "requirement-acceptance",
        "defect-regression",
        "security-boundary",
        "compatibility-migration",
        "data-integrity",
        "architecture-contract",
        "implementation-detail",
    ]
    owner: Identifier
    capabilities: Annotated[list[Identifier], Field(min_length=1)]
    lanes: Annotated[list[Identifier], Field(min_length=1)]
    parallelism: Literal["worker-safe", "process-isolated", "exclusive"]
    resources: Annotated[
        list[Literal["hermetic", "db", "git", "process", "port", "docker", "browser", "live-provider", "serial"]],
        Field(min_length=1),
    ]


class PortfolioRule(StrictModel):
    id: Identifier
    selectors: Annotated[list[NonEmpty], Field(min_length=1)]
    exclude_selectors: list[NonEmpty] = Field(default_factory=list)
    classification: Classification


class PortfolioPolicy(StrictModel):
    rules: Annotated[list[PortfolioRule], Field(min_length=1)]


class Quarantine(StrictModel):
    selector: NonEmpty
    owner: Identifier
    reason: NonEmpty
    issue: NonEmpty
    quarantined_at: date
    expires_at: date
    repair_plan: NonEmpty


class DeleteCandidate(StrictModel):
    selector: NonEmpty
    owner: Identifier
    evidence: Annotated[list[NonEmpty], Field(min_length=2)]


class Gap(StrictModel):
    id: Identifier
    capability: Identifier
    owner: Identifier
    risk: Literal["low", "medium", "high", "critical"]
    description: NonEmpty
    target_lane: Identifier
    issue: NonEmpty
    acceptance: Annotated[list[NonEmpty], Field(min_length=1)]


class MainFlowScenario(StrictModel):
    id: Identifier
    description: NonEmpty | None = None
    pytest: list[NonEmpty] = Field(default_factory=list)
    ui_scripts: list[NonEmpty] = Field(default_factory=list)
    real_container_ui_target: NonEmpty | None = None


class MainFlow(StrictModel):
    id: Identifier
    description: NonEmpty
    scenarios: Annotated[list[MainFlowScenario], Field(min_length=1)]


class ImpactRule(StrictModel):
    id: Identifier
    changed_paths: Annotated[list[NonEmpty], Field(min_length=1)]
    test_selectors: Annotated[list[NonEmpty], Field(min_length=1)]


class PromotionGate(StrictModel):
    min_paired_samples: Annotated[int, Field(ge=1)]
    min_calendar_days: Annotated[int, Field(ge=1)]
    max_misses: Annotated[int, Field(ge=0)]


class ImpactPolicy(StrictModel):
    mode: Literal["shadow", "blocking"]
    unknown_change_lane: Identifier
    always_full_paths: Annotated[list[NonEmpty], Field(min_length=1)]
    rules: list[ImpactRule]
    promotion_gate: PromotionGate


class ParallelPolicy(StrictModel):
    mode: Literal["shadow", "blocking"]
    worker_counts: Annotated[list[Annotated[int, Field(ge=1)]], Field(min_length=1)]
    schedulers: Annotated[list[Literal["load", "worksteal"]], Field(min_length=1)]
    promotion_gate: PromotionGate
    min_p50_speedup_percent: Annotated[float, Field(ge=0, le=100)]
    max_cpu_minutes_increase_percent: Annotated[float, Field(ge=0)]
    max_coverage_delta_percentage_points: Annotated[float, Field(ge=0, le=100)]


class MutationTarget(StrictModel):
    path: NonEmpty
    tests: Annotated[list[NonEmpty], Field(min_length=1)]
    min_score: Annotated[float, Field(ge=0, le=100)]


class MutationPolicy(StrictModel):
    lane: Identifier
    time_budget_seconds: Annotated[int, Field(ge=1)]
    targets: Annotated[list[MutationTarget], Field(min_length=1)]


class Budgets(StrictModel):
    impact_selection_p95_seconds: Annotated[int, Field(ge=1)]
    main_full_p95_seconds: Annotated[int, Field(ge=1)]
    max_flaky_rate_percent: Annotated[float, Field(ge=0, le=100)]
    max_quarantine_days: Annotated[int, Field(ge=1)]
    evidence_retention_days: Annotated[int, Field(ge=1)]


class QualityPolicy(StrictModel):
    coverage: CoveragePolicy
    owners: Annotated[list[Owner], Field(min_length=1)]
    capabilities: Annotated[list[Capability], Field(min_length=1)]
    lanes: Annotated[list[Lane], Field(min_length=1)]
    portfolio: PortfolioPolicy
    quarantines: list[Quarantine] = Field(default_factory=list)
    delete_candidates: list[DeleteCandidate] = Field(default_factory=list)
    gaps: list[Gap] = Field(default_factory=list)
    impact: ImpactPolicy
    parallel: ParallelPolicy
    mutation: MutationPolicy
    budgets: Budgets
    main_flows: Annotated[list[MainFlow], Field(min_length=1)]
