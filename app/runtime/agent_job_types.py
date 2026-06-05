from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from .litellm_defaults import configure_litellm_import_defaults

configure_litellm_import_defaults()

import dspy
from pydantic import BaseModel

from .agent_profiles import (
    ATTRIBUTION_ANALYZER_PROFILE,
    EVAL_CASE_GOVERNOR_PROFILE,
    EXECUTION_OPTIMIZER_PROFILE,
    PROPOSAL_GENERATOR_PROFILE,
    REGRESSION_IMPACT_ANALYZER_PROFILE,
)
from .feedback_schemas import (
    AttributionOutput,
    ExecutionPlanOutput,
    FeedbackEvalCaseGenerationOutput,
    FeedbackOptimizationPlanOutput,
    RegressionImpactAnalysisOutput,
)
from .json_types import JsonObject
from .prompts.feedback_prompts import (
    attribution_prompt,
    eval_case_generation_prompt,
    execution_plan_prompt,
    proposal_generator_prompt,
    regression_impact_analysis_prompt,
)


PromptBuilder = Callable[[str, JsonObject], str]


class AgentJobType(StrEnum):
    ATTRIBUTION = "attribution"
    BATCH_PLAN = "batch_plan"
    EXECUTION = "execution"
    EVAL_CASE_GENERATION = "eval_case_generation"
    REGRESSION_IMPACT_ANALYSIS = "regression_impact_analysis"


class AttributionFormattingSignature(dspy.Signature):
    """把归因分析智能体的自然语言或片段 JSON 转换为归因输出模型。

    只能使用 raw_agent_output 和 job_input_json 中已有的信息；证据不足时输出
    insufficient_information、needs_human_analysis 或 needs_human_review。
    不要补充原文和证据没有支持的业务事实。
    """

    raw_agent_output: str = dspy.InputField(desc="归因分析智能体原始输出。")
    job_input_json: str = dspy.InputField(desc="归因 job 输入，包含 feedback_case_id、job_id、证据路径等。")
    formatted_output: AttributionOutput = dspy.OutputField(desc="完整归因输出对象。")


class BatchPlanFormattingSignature(dspy.Signature):
    """把优化方案生成智能体的输出转换为优化方案输出模型。

    只能使用 raw_agent_output 和 job_input_json 中已有的信息。可执行任务必须放在
    tasks 中，外部任务的 task_context 必须嵌套在对应 task 内；能定位到外部系统、
    工具/API 和具体问题描述的项必须生成 external_webhook 任务；不能定位到具体对象、
    接口、工具或问题 ID 的项才放入 blocked_items。
    """

    raw_agent_output: str = dspy.InputField(desc="优化方案生成智能体原始输出。")
    job_input_json: str = dspy.InputField(desc="优化方案 job 输入，包含 batch、归因输出、回归用例和目标策略。")
    formatted_output: FeedbackOptimizationPlanOutput = dspy.OutputField(desc="完整优化方案输出对象。")


class ExecutionFormattingSignature(dspy.Signature):
    """把执行优化智能体的自然语言或片段 JSON 转换为执行方案输出模型。

    只能使用 raw_agent_output 和 job_input_json 中已有的信息。只能基于 target_file_contexts
    为 target_paths 生成 append_text、replace_file、create_file 或 noop 操作；无法安全执行时输出 needs_human_review。
    """

    raw_agent_output: str = dspy.InputField(desc="执行优化智能体原始输出。")
    job_input_json: str = dspy.InputField(desc="执行 job 输入，包含 optimization_task_id、execution_job_id、target_paths 和 proposal。")
    formatted_output: ExecutionPlanOutput = dspy.OutputField(desc="完整执行方案输出对象。")


class EvalCaseGenerationFormattingSignature(dspy.Signature):
    """把 eval-case-governor 的自然语言或片段 JSON 转换为评估用例生成输出模型。

    只能使用 raw_agent_output 和 job_input_json 中已有的信息。每个 eval case 必须包含
    prompt、expected_behavior、checks_json 和 labels；证据不足时输出 no_action_reason。
    """

    raw_agent_output: str = dspy.InputField(desc="eval-case-governor 原始输出。")
    job_input_json: str = dspy.InputField(desc="评估用例生成 job 输入，包含反馈来源、归因和建议上下文。")
    formatted_output: FeedbackEvalCaseGenerationOutput = dspy.OutputField(desc="完整评估用例生成输出对象。")


class RegressionImpactFormattingSignature(dspy.Signature):
    """把 regression-impact-analyzer 的自然语言或片段 JSON 转换为回归影响分析输出模型。

    只能使用 raw_agent_output 和 job_input_json 中已有的信息。必须总结 gate_result、
    impacted_assets 和 recommendations；不确定时输出 needs_human_review。
    """

    raw_agent_output: str = dspy.InputField(desc="regression-impact-analyzer 原始输出。")
    job_input_json: str = dspy.InputField(desc="回归影响分析 job 输入，包含 eval_run、gate_result 和 item 快照。")
    formatted_output: RegressionImpactAnalysisOutput = dspy.OutputField(desc="完整回归影响分析输出对象。")


@dataclass(frozen=True)
class AgentJobSpec:
    job_type: AgentJobType
    profile_name: str
    prompt_builder: PromptBuilder
    output_model: type[BaseModel]
    formatter_signature: type[dspy.Signature]


def _attribution_prompt_builder(input_path: str, _job_input: JsonObject) -> str:
    return attribution_prompt(input_path)


def _proposal_prompt_builder(input_path: str, job_input: JsonObject) -> str:
    return proposal_generator_prompt(input_path, input_payload=job_input)


def _execution_prompt_builder(input_path: str, job_input: JsonObject) -> str:
    return execution_plan_prompt(input_path, input_payload=job_input)


def _eval_case_prompt_builder(input_path: str, job_input: JsonObject) -> str:
    return eval_case_generation_prompt(input_path, input_payload=job_input)


def _regression_impact_prompt_builder(input_path: str, job_input: JsonObject) -> str:
    return regression_impact_analysis_prompt(input_path, input_payload=job_input)


AGENT_JOB_SPECS: Final[dict[AgentJobType, AgentJobSpec]] = {
    AgentJobType.ATTRIBUTION: AgentJobSpec(
        job_type=AgentJobType.ATTRIBUTION,
        profile_name=ATTRIBUTION_ANALYZER_PROFILE,
        prompt_builder=_attribution_prompt_builder,
        output_model=AttributionOutput,
        formatter_signature=AttributionFormattingSignature,
    ),
    AgentJobType.BATCH_PLAN: AgentJobSpec(
        job_type=AgentJobType.BATCH_PLAN,
        profile_name=PROPOSAL_GENERATOR_PROFILE,
        prompt_builder=_proposal_prompt_builder,
        output_model=FeedbackOptimizationPlanOutput,
        formatter_signature=BatchPlanFormattingSignature,
    ),
    AgentJobType.EXECUTION: AgentJobSpec(
        job_type=AgentJobType.EXECUTION,
        profile_name=EXECUTION_OPTIMIZER_PROFILE,
        prompt_builder=_execution_prompt_builder,
        output_model=ExecutionPlanOutput,
        formatter_signature=ExecutionFormattingSignature,
    ),
    AgentJobType.EVAL_CASE_GENERATION: AgentJobSpec(
        job_type=AgentJobType.EVAL_CASE_GENERATION,
        profile_name=EVAL_CASE_GOVERNOR_PROFILE,
        prompt_builder=_eval_case_prompt_builder,
        output_model=FeedbackEvalCaseGenerationOutput,
        formatter_signature=EvalCaseGenerationFormattingSignature,
    ),
    AgentJobType.REGRESSION_IMPACT_ANALYSIS: AgentJobSpec(
        job_type=AgentJobType.REGRESSION_IMPACT_ANALYSIS,
        profile_name=REGRESSION_IMPACT_ANALYZER_PROFILE,
        prompt_builder=_regression_impact_prompt_builder,
        output_model=RegressionImpactAnalysisOutput,
        formatter_signature=RegressionImpactFormattingSignature,
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
