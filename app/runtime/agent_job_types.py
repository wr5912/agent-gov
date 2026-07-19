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


class RegressionTestDesignFormattingSignature(dspy.Signature):
    """把回归保障治理结果转换为可直接写入 Workspace 的 pytest 代码候选。

    只能使用 raw_agent_output 中已有的测试代码、测试意图和断言依据。目标路径、Agent ID、
    改进事项 ID、commit 和时间戳由后端绑定；证据不足时输出 no_action_reason。
    """

    raw_agent_output: str = dspy.InputField(desc="回归保障治理 Agent 原始输出。")
    formatted_output: RegressionTestDesignFormatterOutput = dspy.OutputField(
        desc=(
            "只包含至多一个 tests item（完整 pytest 模块）的 test_code、test_intent、assertion_rationale，或 no_action_reason。"
            "test_code 必须保留真实换行符，不得改写为含字面量反斜杠加 n 的单行字符串；不得自行定义 agent fixture。"
        )
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
        use_native_structured_output=True,
    ),
    AgentJobType.REGRESSION_TEST_DESIGN: AgentJobSpec(
        job_type=AgentJobType.REGRESSION_TEST_DESIGN,
        profile_name=GOVERNOR_PROFILE,
        prompt_builder=_regression_test_design_prompt_builder,
        output_model=RegressionTestDesignOutput,
        formatter_output_model=RegressionTestDesignFormatterOutput,
        formatter_signature=RegressionTestDesignFormattingSignature,
        use_native_structured_output=True,
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
