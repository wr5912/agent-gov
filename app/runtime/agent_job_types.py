from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from .agent_profiles import (
    ATTRIBUTION_ANALYZER_PROFILE,
    EVAL_CASE_GOVERNOR_PROFILE,
    EXECUTION_OPTIMIZER_PROFILE,
    PROPOSAL_GENERATOR_PROFILE,
    REGRESSION_IMPACT_ANALYZER_PROFILE,
)
from .schema_versions import (
    ATTRIBUTION_OUTPUT_SCHEMA_VERSION,
    EXECUTION_PLAN_OUTPUT_SCHEMA_VERSION,
    FEEDBACK_EVAL_CASE_GENERATION_OUTPUT_SCHEMA_VERSION,
    FEEDBACK_OPTIMIZATION_PLAN_OUTPUT_SCHEMA_VERSION,
    REGRESSION_IMPACT_ANALYSIS_OUTPUT_SCHEMA_VERSION,
)


AgentJobType = Literal[
    "attribution",
    "batch_plan",
    "execution",
    "eval_case_generation",
    "regression_impact_analysis",
]


@dataclass(frozen=True)
class AgentJobSpec:
    job_type: AgentJobType
    profile_name: str
    output_schema_version: str


AGENT_JOB_SPECS: Final[dict[AgentJobType, AgentJobSpec]] = {
    "attribution": AgentJobSpec(
        job_type="attribution",
        profile_name=ATTRIBUTION_ANALYZER_PROFILE,
        output_schema_version=ATTRIBUTION_OUTPUT_SCHEMA_VERSION,
    ),
    "batch_plan": AgentJobSpec(
        job_type="batch_plan",
        profile_name=PROPOSAL_GENERATOR_PROFILE,
        output_schema_version=FEEDBACK_OPTIMIZATION_PLAN_OUTPUT_SCHEMA_VERSION,
    ),
    "execution": AgentJobSpec(
        job_type="execution",
        profile_name=EXECUTION_OPTIMIZER_PROFILE,
        output_schema_version=EXECUTION_PLAN_OUTPUT_SCHEMA_VERSION,
    ),
    "eval_case_generation": AgentJobSpec(
        job_type="eval_case_generation",
        profile_name=EVAL_CASE_GOVERNOR_PROFILE,
        output_schema_version=FEEDBACK_EVAL_CASE_GENERATION_OUTPUT_SCHEMA_VERSION,
    ),
    "regression_impact_analysis": AgentJobSpec(
        job_type="regression_impact_analysis",
        profile_name=REGRESSION_IMPACT_ANALYZER_PROFILE,
        output_schema_version=REGRESSION_IMPACT_ANALYSIS_OUTPUT_SCHEMA_VERSION,
    ),
}


def agent_job_spec(job_type: str) -> AgentJobSpec:
    spec = AGENT_JOB_SPECS.get(job_type)  # type: ignore[arg-type]
    if spec is None:
        raise ValueError(f"Unsupported agent job type: {job_type}")
    return spec
