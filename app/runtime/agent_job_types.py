from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, TypeAlias

from .agent_profiles import GOVERNOR_PROFILE
from .feedback_schemas import (
    AttributionFormatterOutput,
    AttributionOutput,
    ExecutionPlanFormatterOutput,
    ExecutionPlanOutput,
    ImprovementOptimizationPlanFormatterOutput,
    ImprovementOptimizationPlanOutput,
    NormalizedFeedbackFormatterOutput,
    NormalizedFeedbackOutput,
    RegressionTestDesignFormatterOutput,
    RegressionTestDesignOutput,
)
from .json_types import JsonObject
from .prompts.feedback_prompt_contexts import (
    build_attribution_prompt_context,
    build_execution_prompt_context,
    build_improvement_optimization_prompt_context,
    build_regression_test_design_prompt_context,
)
from .prompts.feedback_prompts import (
    attribution_prompt,
    execution_plan_prompt,
    improvement_optimization_plan_prompt,
    regression_test_design_prompt,
)


PromptBuilder = Callable[[JsonObject], str]

FormatterOutputModel: TypeAlias = (
    AttributionFormatterOutput
    | ImprovementOptimizationPlanFormatterOutput
    | ExecutionPlanFormatterOutput
    | RegressionTestDesignFormatterOutput
    | NormalizedFeedbackFormatterOutput
)
ProjectedOutputModel: TypeAlias = (
    AttributionOutput | ImprovementOptimizationPlanOutput | ExecutionPlanOutput | RegressionTestDesignOutput | NormalizedFeedbackOutput
)
FormatterOutputModelClass: TypeAlias = (
    type[AttributionFormatterOutput]
    | type[ImprovementOptimizationPlanFormatterOutput]
    | type[ExecutionPlanFormatterOutput]
    | type[RegressionTestDesignFormatterOutput]
    | type[NormalizedFeedbackFormatterOutput]
)
ProjectedOutputModelClass: TypeAlias = (
    type[AttributionOutput]
    | type[ImprovementOptimizationPlanOutput]
    | type[ExecutionPlanOutput]
    | type[RegressionTestDesignOutput]
    | type[NormalizedFeedbackOutput]
)


class AgentJobType(StrEnum):
    ATTRIBUTION = "attribution"
    OPTIMIZATION_PLAN = "optimization_plan"
    EXECUTION = "execution"
    REGRESSION_TEST_DESIGN = "regression_test_design"
    NORMALIZED_FEEDBACK = "normalized_feedback"


@dataclass(frozen=True)
class AgentJobSpec:
    job_type: AgentJobType
    profile_name: str
    prompt_builder: PromptBuilder
    output_model: ProjectedOutputModelClass
    formatter_output_model: FormatterOutputModelClass
    use_native_structured_output: bool = False


def _attribution_prompt_builder(job_input: JsonObject) -> str:
    return attribution_prompt(prompt_context=build_attribution_prompt_context(job_input))


def _improvement_optimization_prompt_builder(job_input: JsonObject) -> str:
    return improvement_optimization_plan_prompt(prompt_context=build_improvement_optimization_prompt_context(job_input))


def _execution_prompt_builder(job_input: JsonObject) -> str:
    return execution_plan_prompt(prompt_context=build_execution_prompt_context(job_input))


def _regression_test_design_prompt_builder(job_input: JsonObject) -> str:
    return regression_test_design_prompt(prompt_context=build_regression_test_design_prompt_context(job_input))


def _normalized_feedback_prompt_builder(job_input: JsonObject) -> str:
    return "你是反馈整理智能体。仅依据用户原始反馈提炼简短 title 和清晰 problem，不得补充原文没有的信息。\n\n用户原始反馈：\n" + str(
        job_input.get("raw_feedback", "")
    )


AGENT_JOB_SPECS: Final[dict[AgentJobType, AgentJobSpec]] = {
    AgentJobType.ATTRIBUTION: AgentJobSpec(
        job_type=AgentJobType.ATTRIBUTION,
        profile_name=GOVERNOR_PROFILE,
        prompt_builder=_attribution_prompt_builder,
        output_model=AttributionOutput,
        formatter_output_model=AttributionFormatterOutput,
    ),
    AgentJobType.OPTIMIZATION_PLAN: AgentJobSpec(
        job_type=AgentJobType.OPTIMIZATION_PLAN,
        profile_name=GOVERNOR_PROFILE,
        prompt_builder=_improvement_optimization_prompt_builder,
        output_model=ImprovementOptimizationPlanOutput,
        formatter_output_model=ImprovementOptimizationPlanFormatterOutput,
    ),
    AgentJobType.EXECUTION: AgentJobSpec(
        job_type=AgentJobType.EXECUTION,
        profile_name=GOVERNOR_PROFILE,
        prompt_builder=_execution_prompt_builder,
        output_model=ExecutionPlanOutput,
        formatter_output_model=ExecutionPlanFormatterOutput,
        use_native_structured_output=True,
    ),
    AgentJobType.REGRESSION_TEST_DESIGN: AgentJobSpec(
        job_type=AgentJobType.REGRESSION_TEST_DESIGN,
        profile_name=GOVERNOR_PROFILE,
        prompt_builder=_regression_test_design_prompt_builder,
        output_model=RegressionTestDesignOutput,
        formatter_output_model=RegressionTestDesignFormatterOutput,
        use_native_structured_output=True,
    ),
    AgentJobType.NORMALIZED_FEEDBACK: AgentJobSpec(
        job_type=AgentJobType.NORMALIZED_FEEDBACK,
        profile_name=GOVERNOR_PROFILE,
        prompt_builder=_normalized_feedback_prompt_builder,
        output_model=NormalizedFeedbackOutput,
        formatter_output_model=NormalizedFeedbackFormatterOutput,
    ),
}


def coerce_agent_job_type(job_type: AgentJobType | str) -> AgentJobType:
    if isinstance(job_type, AgentJobType):
        return job_type
    try:
        return AgentJobType(str(job_type))
    except ValueError as exc:
        raise ValueError(f"Unsupported agent job type: {job_type}") from exc


def agent_job_spec(job_type: AgentJobType | str) -> AgentJobSpec:
    normalized = coerce_agent_job_type(job_type)
    return AGENT_JOB_SPECS[normalized]
