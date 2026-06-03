from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, cast

from pydantic import BaseModel

from .prompts.feedback_prompts import extract_json_candidates
from .feedback_schemas import (
    AttributionOutput,
    ExecutionPlanOutput,
    FeedbackEvalCaseGenerationOutput,
    FeedbackOptimizationPlanOutput,
    ProposalOutput,
    RegressionImpactAnalysisOutput,
)
from .schema_versions import (
    ATTRIBUTION_OUTPUT_SCHEMA_VERSION,
    EXECUTION_PLAN_OUTPUT_SCHEMA_VERSION,
    FEEDBACK_EVAL_CASE_GENERATION_OUTPUT_SCHEMA_VERSION,
    FEEDBACK_OPTIMIZATION_PLAN_OUTPUT_SCHEMA_VERSION,
    PROPOSAL_OUTPUT_SCHEMA_VERSION,
    REGRESSION_IMPACT_ANALYSIS_OUTPUT_SCHEMA_VERSION,
)
from .json_types import JsonObject
from .settings import AppSettings


FormatterJobType = Literal["attribution", "proposal", "batch_plan", "execution", "eval_case_generation", "regression_impact_analysis"]


@dataclass(frozen=True)
class OutputFormatterResult:
    payload: JsonObject
    source: str


class DSPyOutputFormatter:
    """Convert free-form feedback Agent output into the runtime schemas.

    DSPy is imported lazily, but feedback jobs treat formatter availability as a
    runtime requirement instead of silently falling back to placeholder outputs.
    """

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self._lm: Any | None = None

    def enabled(self) -> bool:
        return self.settings.enable_dspy_output_formatter

    def format(
        self,
        *,
        job_type: FormatterJobType,
        raw_text: str,
        job_input: JsonObject,
        expected_schema_version: str,
    ) -> OutputFormatterResult:
        if not self.enabled():
            raise RuntimeError("DSPy output formatter is disabled")
        candidates = extract_json_candidates(raw_text)
        payload = self._format_with_dspy(
            job_type=job_type,
            raw_text=raw_text,
            job_input=job_input,
            candidates=candidates,
        )
        payload.setdefault("schema_version", expected_schema_version)
        payload["_formatter"] = {
            "name": "dspy",
            "source": "agent_text",
            "raw_text": _truncate(raw_text, 20000),
            "candidate_count": len(candidates),
        }
        return OutputFormatterResult(payload=payload, source="dspy")

    def _format_with_dspy(
        self,
        *,
        job_type: FormatterJobType,
        raw_text: str,
        job_input: JsonObject,
        candidates: list[JsonObject],
    ) -> JsonObject:
        import dspy  # type: ignore[import-untyped]

        output_model: type[BaseModel]
        if job_type == "attribution":
            output_model = AttributionOutput
            signature = _attribution_signature(dspy)
        elif job_type == "proposal":
            output_model = ProposalOutput
            signature = _proposal_signature(dspy)
        elif job_type == "batch_plan":
            output_model = FeedbackOptimizationPlanOutput
            signature = _batch_plan_signature(dspy)
        elif job_type == "execution":
            output_model = ExecutionPlanOutput
            signature = _execution_signature(dspy)
        elif job_type == "eval_case_generation":
            output_model = FeedbackEvalCaseGenerationOutput
            signature = _eval_case_generation_signature(dspy)
        elif job_type == "regression_impact_analysis":
            output_model = RegressionImpactAnalysisOutput
            signature = _regression_impact_signature(dspy)
        else:
            raise RuntimeError(f"Unsupported formatter job type: {job_type}")

        predictor = dspy.Predict(signature)
        lm = self._lm_instance(dspy)
        last_error: Exception | None = None
        for _ in range(max(1, self.settings.dspy_output_formatter_max_retries + 1)):
            try:
                with _dspy_lm_context(dspy, lm):
                    result = predictor(
                        raw_agent_output=raw_text,
                        job_input_json=json.dumps(job_input, ensure_ascii=False, indent=2),
                        candidate_json_objects=json.dumps(candidates, ensure_ascii=False, indent=2),
                    )
                return _coerce_payload(getattr(result, "formatted_output"), output_model)
            except Exception as exc:
                last_error = exc
        if last_error:
            raise last_error
        raise RuntimeError("DSPy formatter produced no result")

    def _lm_instance(self, dspy: Any) -> Any:
        if self._lm is not None:
            return self._lm
        model = self.settings.dspy_output_formatter_model or self.settings.agent_model
        if not model:
            raise RuntimeError("DSPy formatter model is not configured")
        if "/" not in model and self.settings.provider_api_url and "anthropic" in self.settings.provider_api_url:
            model = f"anthropic/{model}"
        kwargs: dict[str, object] = {}
        if self.settings.provider_api_key:
            kwargs["api_key"] = self.settings.provider_api_key
        if self.settings.provider_api_url:
            kwargs["api_base"] = self.settings.provider_api_url
        try:
            self._lm = dspy.LM(model=model, **kwargs)
        except TypeError:
            if "api_base" in kwargs:
                kwargs["base_url"] = kwargs.pop("api_base")
            self._lm = dspy.LM(model=model, **kwargs)
        return self._lm


def _attribution_signature(dspy: Any) -> type[Any]:
    class AttributionFormattingSignature(dspy.Signature):
        """把归因分析智能体的自然语言或片段 JSON 转换为目标输出 schema。

        只能使用 raw_agent_output 和 job_input_json 中已有的信息；证据不足时输出
        insufficient_information、needs_human_analysis 或 needs_human_review。
        不要补充原文和证据没有支持的业务事实。
        """

        raw_agent_output: str = dspy.InputField(desc="归因分析智能体原始输出，可能是文本、片段 JSON 或完整 JSON。")
        job_input_json: str = dspy.InputField(desc="归因 job 输入，包含 feedback_case_id、job_id、证据路径等。")
        candidate_json_objects: str = dspy.InputField(desc="从原始输出中抽取到的 JSON 对象候选。")
        formatted_output: AttributionOutput = dspy.OutputField(
            desc=f"符合 {ATTRIBUTION_OUTPUT_SCHEMA_VERSION} 的完整对象。"
        )

    return AttributionFormattingSignature


def _proposal_signature(dspy: Any) -> type[Any]:
    class ProposalFormattingSignature(dspy.Signature):
        """把优化方案生成智能体的自然语言或片段 JSON 转换为目标输出 schema。

        只能使用 raw_agent_output 和 job_input_json 中已有的信息。无法形成可执行建议时，
        proposals 置空并填写 no_action_reason；外部问题写入 external_guidance。
        """

        raw_agent_output: str = dspy.InputField(desc="优化方案生成智能体原始输出，可能是文本、片段 JSON 或完整 JSON。")
        job_input_json: str = dspy.InputField(desc="建议 job 输入，包含 feedback_case_id、job_id、允许目标路径等。")
        candidate_json_objects: str = dspy.InputField(desc="从原始输出中抽取到的 JSON 对象候选。")
        formatted_output: ProposalOutput = dspy.OutputField(
            desc=f"符合 {PROPOSAL_OUTPUT_SCHEMA_VERSION} 的完整对象。"
        )

    return ProposalFormattingSignature


def _batch_plan_signature(dspy: Any) -> type[Any]:
    class BatchPlanFormattingSignature(dspy.Signature):
        """把批次优化方案生成智能体的输出转换为目标输出 schema。

        只能使用 raw_agent_output 和 job_input_json 中已有的信息。可执行任务必须放在
        tasks 中，外部任务的 task_context 必须嵌套在对应 task 内；能定位到外部系统、
        工具/API 和具体问题描述的项必须生成 external_webhook 任务；不能定位到具体对象、
        接口、工具或问题 ID 的项才放入 blocked_items。
        """

        raw_agent_output: str = dspy.InputField(desc="优化方案生成智能体原始输出，可能是文本、片段 JSON 或完整 JSON。")
        job_input_json: str = dspy.InputField(desc="批次优化方案 job 输入，包含 batch、归因输出、回归用例和目标策略。")
        candidate_json_objects: str = dspy.InputField(desc="从原始输出中抽取到的 JSON 对象候选。")
        formatted_output: FeedbackOptimizationPlanOutput = dspy.OutputField(
            desc=f"符合 {FEEDBACK_OPTIMIZATION_PLAN_OUTPUT_SCHEMA_VERSION} 的完整对象。"
        )

    return BatchPlanFormattingSignature


def _execution_signature(dspy: Any) -> type[Any]:
    class ExecutionFormattingSignature(dspy.Signature):
        """把执行优化智能体的自然语言或片段 JSON 转换为目标输出 schema。

        只能使用 raw_agent_output 和 job_input_json 中已有的信息。只能基于 target_file_contexts
        为 target_paths 生成 append_text、replace_file、create_file 或 noop 操作；无法安全执行时输出 needs_human_review。
        """

        raw_agent_output: str = dspy.InputField(desc="执行优化智能体原始输出，可能是文本、片段 JSON 或完整 JSON。")
        job_input_json: str = dspy.InputField(desc="执行 job 输入，包含 optimization_task_id、execution_job_id、target_paths 和 proposal。")
        candidate_json_objects: str = dspy.InputField(desc="从原始输出中抽取到的 JSON 对象候选。")
        formatted_output: ExecutionPlanOutput = dspy.OutputField(
            desc=f"符合 {EXECUTION_PLAN_OUTPUT_SCHEMA_VERSION} 的完整对象。"
        )

    return ExecutionFormattingSignature


def _eval_case_generation_signature(dspy: Any) -> type[Any]:
    class EvalCaseGenerationFormattingSignature(dspy.Signature):
        """把 eval-case-governor 的自然语言或片段 JSON 转换为评估用例生成输出 schema。

        只能使用 raw_agent_output 和 job_input_json 中已有的信息。每个 eval case 必须包含
        prompt、expected_behavior、checks_json 和 labels；证据不足时输出 no_action_reason。
        """

        raw_agent_output: str = dspy.InputField(desc="eval-case-governor 原始输出，可能是文本、片段 JSON 或完整 JSON。")
        job_input_json: str = dspy.InputField(desc="评估用例生成 job 输入，包含反馈来源、归因和建议上下文。")
        candidate_json_objects: str = dspy.InputField(desc="从原始输出中抽取到的 JSON 对象候选。")
        formatted_output: FeedbackEvalCaseGenerationOutput = dspy.OutputField(
            desc=f"符合 {FEEDBACK_EVAL_CASE_GENERATION_OUTPUT_SCHEMA_VERSION} 的完整对象。"
        )

    return EvalCaseGenerationFormattingSignature


def _regression_impact_signature(dspy: Any) -> type[Any]:
    class RegressionImpactFormattingSignature(dspy.Signature):
        """把 regression-impact-analyzer 的自然语言或片段 JSON 转换为回归影响分析输出 schema。

        只能使用 raw_agent_output 和 job_input_json 中已有的信息。必须总结 gate_result、
        impacted_assets 和 recommendations；不确定时输出 needs_human_review。
        """

        raw_agent_output: str = dspy.InputField(desc="regression-impact-analyzer 原始输出，可能是文本、片段 JSON 或完整 JSON。")
        job_input_json: str = dspy.InputField(desc="回归影响分析 job 输入，包含 eval_run、gate_result 和 item 快照。")
        candidate_json_objects: str = dspy.InputField(desc="从原始输出中抽取到的 JSON 对象候选。")
        formatted_output: RegressionImpactAnalysisOutput = dspy.OutputField(
            desc=f"符合 {REGRESSION_IMPACT_ANALYSIS_OUTPUT_SCHEMA_VERSION} 的完整对象。"
        )

    return RegressionImpactFormattingSignature


def _dspy_lm_context(dspy: Any, lm: Any) -> Any:
    context = getattr(dspy, "context", None)
    if context:
        return context(lm=lm)
    dspy.configure(lm=lm)
    return _NullContext()


def _coerce_payload(value: Any, output_model: type[BaseModel]) -> JsonObject:
    if isinstance(value, output_model):
        return cast(JsonObject, value.model_dump(mode="json"))
    if isinstance(value, BaseModel):
        return cast(JsonObject, output_model.model_validate(value.model_dump(mode="json")).model_dump(mode="json"))
    if isinstance(value, dict):
        return cast(JsonObject, output_model.model_validate(value).model_dump(mode="json"))
    if isinstance(value, str):
        return cast(JsonObject, output_model.model_validate(json.loads(value)).model_dump(mode="json"))
    raise TypeError(f"Unsupported DSPy formatter output: {type(value).__name__}")


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\n...[truncated]"


class _NullContext:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_: object) -> None:
        return None
