from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from .litellm_defaults import configure_litellm_import_defaults

configure_litellm_import_defaults()

import dspy
from pydantic import BaseModel

from .agent_job_types import AgentJobType, agent_job_spec, coerce_agent_job_type
from .json_types import JsonObject
from .settings import AppSettings


TOutput = TypeVar("TOutput", bound=BaseModel)


@dataclass(frozen=True)
class OutputFormatterResult(Generic[TOutput]):
    output: TOutput


class OutputFormatterError(RuntimeError):
    """Raised when fallback output formatting fails after preserving diagnostics."""

    def __init__(
        self,
        *,
        job_type: str,
        raw_text: str,
        cause: Exception,
    ) -> None:
        self.cause = cause
        self.raw_output_json = {
            "_formatter": {
                "name": "dspy",
                "status": "failed",
                "job_type": job_type,
                "error_type": cause.__class__.__name__,
                "error_message": _truncate(str(cause), 4000),
            },
            "raw_text": _truncate(raw_text, 20000),
        }
        super().__init__(f"DSPy output formatter failed for {job_type}: {_truncate(str(cause), 4000)}")


class DSPyOutputFormatter:
    """Convert free-form feedback Agent output into typed Pydantic output models.

    Feedback jobs treat formatter availability as a runtime requirement instead
    of silently falling back to placeholder outputs.
    """

    def __init__(self, settings: AppSettings, langfuse: Any | None = None) -> None:
        self.settings = settings
        self.langfuse = langfuse
        self._lm: Any | None = None

    def enabled(self) -> bool:
        return self.settings.enable_dspy_output_formatter

    def format(
        self,
        *,
        job_type: AgentJobType | str,
        raw_text: str,
        job_input: JsonObject,
    ) -> OutputFormatterResult[BaseModel]:
        if not self.enabled():
            raise RuntimeError("DSPy output formatter is disabled")
        normalized_job_type = coerce_agent_job_type(job_type)
        metadata = _formatter_metadata_payload(normalized_job_type, job_input)
        try:
            with self._langfuse_scope(metadata) as observation:
                try:
                    output = self._format_with_dspy(
                        job_type=normalized_job_type,
                        raw_text=raw_text,
                        job_input=job_input,
                        output_model=agent_job_spec(normalized_job_type).formatter_output_model,
                    )
                    self._update_observation(
                        observation,
                        output={
                            "status": "completed",
                            "output_model": output.__class__.__name__,
                        },
                    )
                except Exception as exc:
                    self._update_observation(
                        observation,
                        output={
                            "status": "failed",
                            "error_type": exc.__class__.__name__,
                            "error_message": _truncate(str(exc), 1000),
                        },
                    )
                    raise
        except Exception as exc:
            raise OutputFormatterError(
                job_type=normalized_job_type.value,
                raw_text=raw_text,
                cause=exc,
            ) from exc
        return OutputFormatterResult(output=output)

    def _format_with_dspy(
        self,
        *,
        job_type: AgentJobType,
        raw_text: str,
        job_input: JsonObject,
        output_model: type[BaseModel],
    ) -> BaseModel:
        self._instrument_dspy()
        signature = _signature_for_job_type(job_type)
        predictor = dspy.Predict(signature)
        lm = self._lm_instance()
        last_error: Exception | None = None
        for _ in range(max(1, self.settings.dspy_output_formatter_max_retries + 1)):
            try:
                with _dspy_lm_context(lm):
                    result = predictor(
                        raw_agent_output=raw_text,
                        job_input_json=json.dumps(job_input, ensure_ascii=False, indent=2),
                    )
                return _coerce_output_model(getattr(result, "formatted_output"), output_model)
            except Exception as exc:
                last_error = exc
        if last_error:
            raise last_error
        raise RuntimeError("DSPy formatter produced no result")

    def _lm_instance(self) -> Any:
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

    def _instrument_dspy(self) -> None:
        if self.langfuse is not None and hasattr(self.langfuse, "instrument_dspy"):
            self.langfuse.instrument_dspy()

    def _langfuse_scope(self, metadata: dict[str, str]) -> Any:
        if self.langfuse is None:
            return _NullContext()
        propagate = getattr(self.langfuse, "propagate_attributes", None)
        start = getattr(self.langfuse, "start_observation", None)
        if propagate is None or start is None:
            return _NullContext()
        session_id = metadata.get("job_id") or metadata.get("batch_id")
        return _NestedContext(
            propagate(
                session_id=session_id,
                metadata=metadata,
                trace_name=f"runtime.output_formatter.{metadata['job_type']}",
            ),
            start(
                as_type="span",
                name=f"runtime.output_formatter.{metadata['job_type']}",
                input=metadata,
                metadata=metadata,
            ),
        )

    def _update_observation(self, observation: Any, **kwargs: Any) -> None:
        if self.langfuse is not None and hasattr(self.langfuse, "update_observation"):
            self.langfuse.update_observation(observation, **kwargs)


def _output_model_for_job_type(job_type: AgentJobType | str) -> type[BaseModel]:
    return agent_job_spec(job_type).output_model


def _signature_for_job_type(job_type: AgentJobType | str) -> type[dspy.Signature]:
    return agent_job_spec(job_type).formatter_signature


def _dspy_lm_context(lm: Any) -> Any:
    context = getattr(dspy, "context", None)
    if context:
        return context(lm=lm)
    dspy.configure(lm=lm)
    return _NullContext()


def _coerce_output_model(value: Any, output_model: type[TOutput]) -> TOutput:
    if isinstance(value, output_model):
        return value
    if isinstance(value, BaseModel):
        return output_model.model_validate(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return output_model.model_validate(value)
    if isinstance(value, str):
        return output_model.model_validate(json.loads(value))
    raise TypeError(f"Unsupported DSPy formatter output: {type(value).__name__}")


def _formatter_metadata_payload(
    job_type: AgentJobType,
    job_input: JsonObject,
) -> dict[str, str]:
    metadata = {
        "component": "dspy_output_formatter",
        "job_type": job_type.value,
        "output_model": agent_job_spec(job_type).formatter_output_model.__name__,
    }
    for key in (
        "job_id",
        "batch_id",
        "feedback_case_id",
        "optimization_task_id",
        "execution_job_id",
        "eval_run_id",
        "regression_plan_id",
    ):
        value = job_input.get(key)
        if isinstance(value, (str, int, float, bool)) and str(value).strip():
            metadata[key] = str(value)
    return metadata


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\n...[truncated]"


class _NullContext:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_: object) -> None:
        return None


class _NestedContext:
    def __init__(self, *contexts: Any) -> None:
        self.contexts = contexts
        self.entered: list[Any] = []

    def __enter__(self) -> Any:
        value = None
        for context in self.contexts:
            entered = context.__enter__()
            self.entered.append(context)
            if entered is not None:
                value = entered
        return value

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        suppress = False
        for context in reversed(self.entered):
            suppress = bool(context.__exit__(exc_type, exc, traceback)) or suppress
        return suppress
