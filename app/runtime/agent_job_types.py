from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, TypeAlias

from .litellm_defaults import configure_litellm_import_defaults

configure_litellm_import_defaults()

import dspy

from .agent_profiles import GOVERNOR_PROFILE
from .feedback_schemas import (
    AttributionFormatterOutput,
    AttributionOutput,
    ExecutionPlanFormatterOutput,
    ExecutionPlanOutput,
    FeedbackEvalCaseGenerationFormatterOutput,
    FeedbackEvalCaseGenerationOutput,
    ImprovementOptimizationPlanFormatterOutput,
    ImprovementOptimizationPlanOutput,
)
from .json_types import JsonObject
from .prompts.feedback_prompt_contexts import (
    build_attribution_prompt_context,
    build_eval_case_generation_prompt_context,
    build_execution_prompt_context,
    build_improvement_optimization_prompt_context,
)
from .prompts.feedback_prompts import (
    attribution_prompt,
    eval_case_generation_prompt,
    execution_plan_prompt,
    improvement_optimization_plan_prompt,
)


PromptBuilder = Callable[[JsonObject], str]

FormatterOutputModel: TypeAlias = (
    AttributionFormatterOutput | ImprovementOptimizationPlanFormatterOutput | ExecutionPlanFormatterOutput | FeedbackEvalCaseGenerationFormatterOutput
)
ProjectedOutputModel: TypeAlias = AttributionOutput | ImprovementOptimizationPlanOutput | ExecutionPlanOutput | FeedbackEvalCaseGenerationOutput
FormatterOutputModelClass: TypeAlias = (
    type[AttributionFormatterOutput]
    | type[ImprovementOptimizationPlanFormatterOutput]
    | type[ExecutionPlanFormatterOutput]
    | type[FeedbackEvalCaseGenerationFormatterOutput]
)
ProjectedOutputModelClass: TypeAlias = (
    type[AttributionOutput] | type[ImprovementOptimizationPlanOutput] | type[ExecutionPlanOutput] | type[FeedbackEvalCaseGenerationOutput]
)


class AgentJobType(StrEnum):
    ATTRIBUTION = "attribution"
    OPTIMIZATION_PLAN = "optimization_plan"
    EXECUTION = "execution"
    EVAL_CASE_GENERATION = "eval_case_generation"


class AttributionFormattingSignature(dspy.Signature):
    """把归因分析智能体的自然语言或片段 JSON 转换为归因输出模型。

    只能使用 raw_agent_output 中已有的业务要点；证据不足时输出
    insufficient_information、needs_human_analysis 或 needs_human_review。
    不要补充原文和证据没有支持的业务事实。
    """

    raw_agent_output: str = dspy.InputField(desc="归因分析智能体原始输出。")
    formatted_output: AttributionFormatterOutput = dspy.OutputField(desc="归因业务内容，不包含 feedback_case_id 和 attribution_job_id。")


class ImprovementOptimizationPlanFormattingSignature(dspy.Signature):
    """把改进事项优化方案智能体输出转换为四阶段 OptimizationPlan 内容模型。"""

    raw_agent_output: str = dspy.InputField(desc="改进事项优化方案智能体原始输出。")
    formatted_output: ImprovementOptimizationPlanFormatterOutput = dspy.OutputField(desc="事项级优化方案业务内容，只包含 summary、changes 和 risk_level。")


class ExecutionFormattingSignature(dspy.Signature):
    """把执行优化智能体的自然语言或片段 JSON 转换为执行方案输出模型。

    只能使用 raw_agent_output 中已有的执行方案业务要点。无法安全执行时输出 needs_human_review。
    """

    raw_agent_output: str = dspy.InputField(desc="执行优化智能体原始输出。")
    formatted_output: ExecutionPlanFormatterOutput = dspy.OutputField(
        desc="执行方案业务内容，只包含 status、summary、operations、validation、risk 和人工复核原因。"
    )


class EvalCaseGenerationFormattingSignature(dspy.Signature):
    """把 eval-case-governor 的自然语言或片段 JSON 转换为评估用例生成输出模型。

    只能使用 raw_agent_output 中已有的评估用例业务要点。每个 eval case 必须包含
    prompt、expected_behavior、checks_json 和 labels；证据不足时输出 no_action_reason。
    """

    raw_agent_output: str = dspy.InputField(desc="eval-case-governor 原始输出。")
    formatted_output: FeedbackEvalCaseGenerationFormatterOutput = dspy.OutputField(desc="评估用例草案业务内容，不包含 job、scope、结果计数和生命周期字段。")


@dataclass(frozen=True)
class AgentJobSpec:
    job_type: AgentJobType
    profile_name: str
    prompt_builder: PromptBuilder
    output_model: ProjectedOutputModelClass
    formatter_output_model: FormatterOutputModelClass
    formatter_signature: type[dspy.Signature]


def _attribution_prompt_builder(job_input: JsonObject) -> str:
    return attribution_prompt(prompt_context=build_attribution_prompt_context(job_input))


def _improvement_optimization_prompt_builder(job_input: JsonObject) -> str:
    return improvement_optimization_plan_prompt(prompt_context=build_improvement_optimization_prompt_context(job_input))


def _execution_prompt_builder(job_input: JsonObject) -> str:
    return execution_plan_prompt(prompt_context=build_execution_prompt_context(job_input))


def _eval_case_prompt_builder(job_input: JsonObject) -> str:
    return eval_case_generation_prompt(prompt_context=build_eval_case_generation_prompt_context(job_input))


AGENT_JOB_SPECS: Final[dict[AgentJobType, AgentJobSpec]] = {
    AgentJobType.ATTRIBUTION: AgentJobSpec(
        job_type=AgentJobType.ATTRIBUTION,
        profile_name=GOVERNOR_PROFILE,
        prompt_builder=_attribution_prompt_builder,
        output_model=AttributionOutput,
        formatter_output_model=AttributionFormatterOutput,
        formatter_signature=AttributionFormattingSignature,
    ),
    AgentJobType.OPTIMIZATION_PLAN: AgentJobSpec(
        job_type=AgentJobType.OPTIMIZATION_PLAN,
        profile_name=GOVERNOR_PROFILE,
        prompt_builder=_improvement_optimization_prompt_builder,
        output_model=ImprovementOptimizationPlanOutput,
        formatter_output_model=ImprovementOptimizationPlanFormatterOutput,
        formatter_signature=ImprovementOptimizationPlanFormattingSignature,
    ),
    AgentJobType.EXECUTION: AgentJobSpec(
        job_type=AgentJobType.EXECUTION,
        profile_name=GOVERNOR_PROFILE,
        prompt_builder=_execution_prompt_builder,
        output_model=ExecutionPlanOutput,
        formatter_output_model=ExecutionPlanFormatterOutput,
        formatter_signature=ExecutionFormattingSignature,
    ),
    AgentJobType.EVAL_CASE_GENERATION: AgentJobSpec(
        job_type=AgentJobType.EVAL_CASE_GENERATION,
        profile_name=GOVERNOR_PROFILE,
        prompt_builder=_eval_case_prompt_builder,
        output_model=FeedbackEvalCaseGenerationOutput,
        formatter_output_model=FeedbackEvalCaseGenerationFormatterOutput,
        formatter_signature=EvalCaseGenerationFormattingSignature,
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
