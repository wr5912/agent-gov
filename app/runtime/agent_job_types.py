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
    ImprovementOptimizationPlanFormatterOutput,
    ImprovementOptimizationPlanOutput,
    NormalizedFeedbackFormatterOutput,
    NormalizedFeedbackOutput,
    RegressionAssessmentFormatterOutput,
    RegressionAssessmentOutput,
)
from .json_types import JsonObject
from .prompts.feedback_prompt_contexts import (
    build_attribution_prompt_context,
    build_execution_prompt_context,
    build_improvement_optimization_prompt_context,
    build_regression_assessment_prompt_context,
)
from .prompts.feedback_prompts import (
    attribution_prompt,
    execution_plan_prompt,
    improvement_optimization_plan_prompt,
    regression_assessment_prompt,
)


PromptBuilder = Callable[[JsonObject], str]

FormatterOutputModel: TypeAlias = (
    AttributionFormatterOutput
    | ImprovementOptimizationPlanFormatterOutput
    | ExecutionPlanFormatterOutput
    | RegressionAssessmentFormatterOutput
    | NormalizedFeedbackFormatterOutput
)
ProjectedOutputModel: TypeAlias = (
    AttributionOutput | ImprovementOptimizationPlanOutput | ExecutionPlanOutput | RegressionAssessmentOutput | NormalizedFeedbackOutput
)
FormatterOutputModelClass: TypeAlias = (
    type[AttributionFormatterOutput]
    | type[ImprovementOptimizationPlanFormatterOutput]
    | type[ExecutionPlanFormatterOutput]
    | type[RegressionAssessmentFormatterOutput]
    | type[NormalizedFeedbackFormatterOutput]
)
ProjectedOutputModelClass: TypeAlias = (
    type[AttributionOutput]
    | type[ImprovementOptimizationPlanOutput]
    | type[ExecutionPlanOutput]
    | type[RegressionAssessmentOutput]
    | type[NormalizedFeedbackOutput]
)


class AgentJobType(StrEnum):
    ATTRIBUTION = "attribution"
    OPTIMIZATION_PLAN = "optimization_plan"
    EXECUTION = "execution"
    REGRESSION_ASSESSMENT = "regression_assessment"
    NORMALIZED_FEEDBACK = "normalized_feedback"


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


class RegressionAssessmentFormattingSignature(dspy.Signature):
    """把回归保障治理结果转换为四阶段回归评估输出模型。

    只能使用 raw_agent_output 中已有的业务要点。每项只包含 expected_behavior、
    checks_json 和 labels；复测输入由后端从原始证据绑定，证据不足时输出 no_action_reason。
    """

    raw_agent_output: str = dspy.InputField(desc="回归保障治理 Agent 原始输出。")
    formatted_output: RegressionAssessmentFormatterOutput = dspy.OutputField(
        desc="四阶段回归评估业务内容，不包含 prompt、job、scope、标识、时间戳和生命周期字段。"
    )


class NormalizedFeedbackFormattingSignature(dspy.Signature):
    """把用户原始反馈直接归纳成 title + problem，不补充原文没有的信息。"""

    raw_agent_output: str = dspy.InputField(desc="用户原始反馈原文。")
    formatted_output: NormalizedFeedbackFormatterOutput = dspy.OutputField(desc="系统理解业务内容：title、problem。")


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


def _regression_assessment_prompt_builder(job_input: JsonObject) -> str:
    return regression_assessment_prompt(prompt_context=build_regression_assessment_prompt_context(job_input))


def _normalized_feedback_prompt_builder(job_input: JsonObject) -> str:
    # NF 走 DSPy formatter 直调（signature 即指令，无 governor prompt/工具）；此 builder 仅为 spec 完整性、不经 run_profile_json。
    return str(job_input.get("raw_feedback", ""))


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
    AgentJobType.REGRESSION_ASSESSMENT: AgentJobSpec(
        job_type=AgentJobType.REGRESSION_ASSESSMENT,
        profile_name=GOVERNOR_PROFILE,
        prompt_builder=_regression_assessment_prompt_builder,
        output_model=RegressionAssessmentOutput,
        formatter_output_model=RegressionAssessmentFormatterOutput,
        formatter_signature=RegressionAssessmentFormattingSignature,
    ),
    AgentJobType.NORMALIZED_FEEDBACK: AgentJobSpec(
        job_type=AgentJobType.NORMALIZED_FEEDBACK,
        profile_name=GOVERNOR_PROFILE,
        prompt_builder=_normalized_feedback_prompt_builder,
        output_model=NormalizedFeedbackOutput,
        formatter_output_model=NormalizedFeedbackFormatterOutput,
        formatter_signature=NormalizedFeedbackFormattingSignature,
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
