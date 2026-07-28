from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from .litellm_defaults import configure_litellm_import_defaults

configure_litellm_import_defaults()

import dspy  # noqa: E402
from dspy.utils.exceptions import AdapterParseError  # noqa: E402

from .json_types import JsonObject  # noqa: E402
from .managed_claude_events import AgentGovControlEvent  # noqa: E402
from .model_provider import ModelProviderRouter  # noqa: E402
from .settings import AppSettings, SpeechSummaryBoundary  # noqa: E402

logger = logging.getLogger(__name__)

SpeechSummarySourceKind = Literal["thinking", "assistant_response"]
_MAX_SOURCE_CHARS = 8_000
_MAX_OUTPUT_TOKENS = 512
_PROCESS_CONCURRENCY = 2
_SPEECH_EVENT_TYPE = "agentgov.speech_summary"

_URL_RE = re.compile(r"(?:https?://|www\.)", re.IGNORECASE)
_WINDOWS_PATH_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:\\[^\s\"']+")
_POSIX_PATH_RE = re.compile(r"(?<![A-Za-z0-9])/(?!/)[^\s\"']+")
_SECRET_RE = re.compile(
    r"(?:\b(?:api[_ -]?key|access[_ -]?token|bearer|password|secret)\b|"
    r"\bsk-[A-Za-z0-9_-]{8,}\b)",
    re.IGNORECASE,
)
_INTERNAL_REASONING_RE = re.compile(
    r"(?:chain[- ]of[- ]thought|思维链|系统提示词|system prompt|developer prompt|"
    r"内部推理|逐步推理过程|原始工具参数)",
    re.IGNORECASE,
)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class SpeechSummaryOutput(BaseModel):
    """DSPy-owned typed output. Provenance is deliberately absent."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=10, max_length=50)

    @field_validator("text")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        stripped = value.strip()
        if not 10 <= len(stripped) <= 50:
            raise ValueError("Speech Summary text must contain 10–50 non-whitespace characters")
        return stripped


class ThinkingSpeechSummaryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary_id: str
    source_kind: Literal["thinking"] = "thinking"
    message_id: str
    block_index: int = Field(ge=0)
    scope: Literal["main"] = "main"
    text: str = Field(min_length=10, max_length=50)
    char_count: int = Field(ge=10, le=50)


class AssistantResponseSpeechSummaryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary_id: str
    source_kind: Literal["assistant_response"] = "assistant_response"
    message_id: str
    scope: Literal["main"] = "main"
    text: str = Field(min_length=10, max_length=50)
    char_count: int = Field(ge=10, le=50)


SpeechSummaryPayload = Annotated[
    ThinkingSpeechSummaryPayload | AssistantResponseSpeechSummaryPayload,
    Field(discriminator="source_kind"),
]
_SPEECH_PAYLOAD_ADAPTER = TypeAdapter(SpeechSummaryPayload)


class SpeechSummaryControlData(BaseModel):
    """Backend-owned managed control data before HTTP envelope projection."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    payload: SpeechSummaryPayload


class AgentGovSpeechSummaryEnvelope(BaseModel):
    """Public SSE data contract shared by all Speech Summary surfaces."""

    model_config = ConfigDict(extra="forbid")

    v: Literal[1] = 1
    type: Literal["agentgov.speech_summary"] = "agentgov.speech_summary"
    run_id: str
    ts: float
    seq: int = Field(ge=1)
    payload: SpeechSummaryPayload


class _SpeechSummarySignature(dspy.Signature):
    """把已完成的 Agent 片段压缩成面向用户的进度播报。

    只生成一条 10–50 字自然语言短句。描述正在处理什么或已经得到什么结果，不披露逐步内部
    推理、chain-of-thought、system/developer prompt、密钥、URL、文件路径、原始工具参数、
    JSON 或代码。不要输出 ID、scope、时间戳、来源字段、前缀、编号或解释。
    """

    source_kind: str = dspy.InputField(desc="thinking 表示进度；assistant_response 表示完成结果。")
    source_text: str = dspy.InputField(desc="已经完成的源文本，只用于提炼安全播报。")
    repair_instruction: str = dspy.InputField(desc="首次为空；修复重试时说明只修正输出契约。")
    summary: SpeechSummaryOutput = dspy.OutputField(desc="只包含 text 的 typed 短播报。")


class SpeechSummaryContractError(ValueError):
    pass


class SpeechSummarySafetyError(ValueError):
    pass


class SpeechSummaryService:
    """Generate one safe typed summary without changing the main Agent outcome."""

    def __init__(
        self,
        settings: AppSettings,
        *,
        provider_router: ModelProviderRouter | None = None,
        langfuse: Any | None = None,
        predictor: Any | None = None,
    ) -> None:
        self.settings = settings
        self.provider_router = provider_router or ModelProviderRouter(settings)
        self.langfuse = langfuse
        self._predictor = predictor
        self._lm: Any | None = None
        self._adapter = dspy.ChatAdapter(use_json_adapter_fallback=False)
        self._semaphore = asyncio.Semaphore(_PROCESS_CONCURRENCY)

    async def generate(
        self,
        *,
        source_kind: SpeechSummarySourceKind,
        source_text: str,
        run_id: str,
        message_id: str,
        block_index: int | None,
    ) -> SpeechSummaryOutput | None:
        normalized_source = source_text.strip()
        if not normalized_source:
            return None
        metadata = {
            "component": "speech_summary",
            "run_id": run_id,
            "message_id": message_id,
            "source_kind": source_kind,
            "block_index": str(block_index) if block_index is not None else "",
            "source_char_count": str(len(normalized_source)),
        }
        try:
            async with self._semaphore:
                async with asyncio.timeout(self.settings.speech_summary_timeout_seconds):
                    with self._langfuse_scope(metadata) as observation:
                        output, usage = await self._generate_with_one_repair(
                            source_kind=source_kind,
                            source_text=normalized_source[:_MAX_SOURCE_CHARS],
                        )
                        _validate_safe_summary(output.text)
                        self._update_observation(
                            observation,
                            output={
                                "status": "completed",
                                "text": output.text,
                                "char_count": len(output.text),
                            },
                            usage_details=usage,
                        )
                        return output
        except asyncio.CancelledError:
            raise
        except SpeechSummarySafetyError:
            logger.warning(
                "event=speech_summary.generate status=dropped reason=safety_filter source_kind=%s",
                source_kind,
            )
        except TimeoutError:
            logger.warning(
                "event=speech_summary.generate status=dropped reason=timeout source_kind=%s",
                source_kind,
            )
        except Exception as exc:
            logger.warning(
                "event=speech_summary.generate status=dropped reason=generation_failed source_kind=%s error_type=%s",
                source_kind,
                exc.__class__.__name__,
            )
        return None

    async def _generate_with_one_repair(
        self,
        *,
        source_kind: SpeechSummarySourceKind,
        source_text: str,
    ) -> tuple[SpeechSummaryOutput, JsonObject | None]:
        try:
            return await self._predict(
                source_kind=source_kind,
                source_text=source_text,
                repair_instruction="",
            )
        except (AdapterParseError, SpeechSummaryContractError, ValidationError, json.JSONDecodeError):
            return await self._predict(
                source_kind=source_kind,
                source_text=source_text,
                repair_instruction=("上次输出未满足 typed 契约。只返回 10–50 字的安全自然语言播报，summary 只能包含 text，不得增加其他字段。"),
            )

    async def _predict(
        self,
        *,
        source_kind: SpeechSummarySourceKind,
        source_text: str,
        repair_instruction: str,
    ) -> tuple[SpeechSummaryOutput, JsonObject | None]:
        self._instrument_dspy()
        predictor = self._predictor or dspy.Predict(_SpeechSummarySignature)
        with dspy.context(
            lm=self._lm_instance(),
            adapter=self._adapter,
            disable_history=True,
            track_usage=True,
        ):
            result = await predictor.acall(
                source_kind=source_kind,
                source_text=source_text,
                repair_instruction=repair_instruction,
            )
        value = getattr(result, "summary", None)
        try:
            output = _coerce_summary_output(value)
        except (ValidationError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise SpeechSummaryContractError("DSPy Speech Summary output did not match SpeechSummaryOutput") from exc
        usage = getattr(result, "get_lm_usage", lambda: None)()
        return output, cast(JsonObject | None, usage if isinstance(usage, dict) else None)

    def _lm_instance(self) -> Any:
        if self._lm is not None:
            return self._lm
        model = self.settings.dspy_output_formatter_model or self.settings.agent_model
        if not model:
            raise RuntimeError("Speech Summary model is not configured")
        kwargs: dict[str, object] = dict(self.provider_router.formatter_kwargs())
        kwargs.update(
            {
                "max_tokens": _MAX_OUTPUT_TOKENS,
                "timeout": self.settings.speech_summary_timeout_seconds,
                "cache": False,
                "num_retries": 0,
            }
        )
        resolved_model = self.provider_router.formatter_model_name(model)
        try:
            self._lm = dspy.LM(model=resolved_model, **kwargs)
        except TypeError:
            if "api_base" in kwargs:
                kwargs["base_url"] = kwargs.pop("api_base")
            self._lm = dspy.LM(model=resolved_model, **kwargs)
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
        return _NestedContext(
            propagate(
                session_id=metadata["run_id"],
                metadata=metadata,
            ),
            start(
                as_type="generation",
                name="runtime.speech_summary",
                input={
                    "source_kind": metadata["source_kind"],
                    "source_char_count": metadata["source_char_count"],
                },
                model=self.settings.dspy_output_formatter_model or self.settings.agent_model,
                metadata=metadata,
            ),
        )

    def _update_observation(self, observation: Any, **kwargs: Any) -> None:
        if self.langfuse is not None and hasattr(self.langfuse, "update_observation"):
            self.langfuse.update_observation(observation, **kwargs)


class SpeechSummaryCoordinator:
    """Per-run semantic boundary detector and cancellable task coordinator."""

    def __init__(
        self,
        *,
        service: SpeechSummaryService,
        run_id: str,
        boundaries: tuple[SpeechSummaryBoundary, ...],
        enabled: bool,
        emit: Callable[[AgentGovControlEvent], Awaitable[None]],
    ) -> None:
        self.service = service
        self.run_id = run_id
        self.boundaries = frozenset(boundaries)
        self.enabled = bool(enabled and boundaries)
        self.emit = emit
        self._current_main_message_id: str | None = None
        self._thinking_parts: dict[tuple[str, int], list[str]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._task_kinds: dict[str, SpeechSummarySourceKind] = {}
        self._scheduled: set[str] = set()
        self._serial_lock = asyncio.Lock()
        self._closed = False

    def observe(self, message: Any) -> None:
        if not self.enabled or self._closed:
            return
        if getattr(message, "parent_tool_use_id", None) is not None:
            return
        name = message.__class__.__name__
        if name == "StreamEvent":
            self._observe_stream_event(message)
            return
        if name.startswith("AssistantMessage"):
            self._observe_assistant_message(message)

    def _observe_stream_event(self, message: Any) -> None:
        event = getattr(message, "event", None)
        if not isinstance(event, dict):
            return
        event_type = event.get("type")
        if event_type == "message_start":
            raw_message = event.get("message")
            message_id = raw_message.get("id") if isinstance(raw_message, dict) else None
            if isinstance(message_id, str) and message_id.strip():
                self._current_main_message_id = message_id
            return
        message_id = self._current_main_message_id
        block_index = event.get("index")
        if not message_id or not isinstance(block_index, int) or block_index < 0:
            return
        key = (message_id, block_index)
        if event_type == "content_block_delta":
            delta = event.get("delta")
            if isinstance(delta, dict) and delta.get("type") == "thinking_delta":
                thinking = delta.get("thinking")
                if isinstance(thinking, str) and thinking:
                    self._thinking_parts.setdefault(key, []).append(thinking)
            return
        if event_type != "content_block_stop":
            return
        parts = self._thinking_parts.pop(key, [])
        if "thinking_block_completed" not in self.boundaries:
            return
        source_text = "".join(parts).strip()
        if source_text:
            self._schedule(
                source_kind="thinking",
                source_text=source_text,
                message_id=message_id,
                block_index=block_index,
            )

    def _observe_assistant_message(self, message: Any) -> None:
        message_id = getattr(message, "message_id", None)
        if not isinstance(message_id, str) or not message_id.strip():
            return
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            return
        text_parts: list[str] = []
        for block in content:
            if block.__class__.__name__ != "TextBlock":
                continue
            text = getattr(block, "text", None)
            if isinstance(text, str) and text.strip():
                text_parts.append(text.strip())
        source_text = "\n".join(text_parts)
        if not source_text:
            return
        # 只有含正文的完整顶层 AssistantMessage 才表示最终回答已经推进到可播报边界。
        # 纯工具调用消息必须跳过，不能顺带取消仍有价值的 thinking 进度摘要。
        self._cancel_pending_thinking()
        if "assistant_response_completed" not in self.boundaries:
            return
        self._schedule(
            source_kind="assistant_response",
            source_text=source_text,
            message_id=message_id,
            block_index=None,
        )

    def _schedule(
        self,
        *,
        source_kind: SpeechSummarySourceKind,
        source_text: str,
        message_id: str,
        block_index: int | None,
    ) -> None:
        summary_id = _summary_id(
            source_kind=source_kind,
            message_id=message_id,
            block_index=block_index,
        )
        if summary_id in self._scheduled:
            return
        self._scheduled.add(summary_id)
        task = asyncio.create_task(
            self._run_summary(
                summary_id=summary_id,
                source_kind=source_kind,
                source_text=source_text,
                message_id=message_id,
                block_index=block_index,
            ),
            name=f"speech-summary-{self.run_id}-{summary_id}",
        )
        self._tasks[summary_id] = task
        self._task_kinds[summary_id] = source_kind

    async def _run_summary(
        self,
        *,
        summary_id: str,
        source_kind: SpeechSummarySourceKind,
        source_text: str,
        message_id: str,
        block_index: int | None,
    ) -> None:
        try:
            async with self._serial_lock:
                output = await self.service.generate(
                    source_kind=source_kind,
                    source_text=source_text,
                    run_id=self.run_id,
                    message_id=message_id,
                    block_index=block_index,
                )
            if output is None or self._closed:
                return
            payload = _speech_payload(
                summary_id=summary_id,
                source_kind=source_kind,
                message_id=message_id,
                block_index=block_index,
                text=output.text,
            )
            control = SpeechSummaryControlData(run_id=self.run_id, payload=payload)
            await self.emit(
                AgentGovControlEvent(
                    name="speech_summary",
                    data=control.model_dump(mode="json", exclude_none=True),
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "event=speech_summary.emit status=dropped error_type=%s",
                exc.__class__.__name__,
            )

    def _cancel_pending_thinking(self) -> None:
        for summary_id, task in list(self._tasks.items()):
            if self._task_kinds.get(summary_id) == "thinking" and not task.done():
                task.cancel()

    async def drain(self, timeout_seconds: float) -> None:
        if not self.enabled:
            return
        pending = [task for task in self._tasks.values() if not task.done()]
        if not pending:
            return
        try:
            async with asyncio.timeout(timeout_seconds):
                await asyncio.gather(*pending, return_exceptions=True)
        except TimeoutError:
            await _cancel_tasks(pending)

    async def cancel(self) -> None:
        self._closed = True
        await _cancel_tasks([task for task in self._tasks.values() if not task.done()])

    def close(self) -> None:
        self._closed = True


def build_speech_summary_envelope(
    data: JsonObject,
    *,
    seq: int,
    timestamp: float | None = None,
) -> JsonObject:
    control = SpeechSummaryControlData.model_validate(data)
    envelope = AgentGovSpeechSummaryEnvelope(
        run_id=control.run_id,
        ts=time.time() if timestamp is None else timestamp,
        seq=seq,
        payload=control.payload,
    )
    return envelope.model_dump(mode="json", exclude_none=True)


def _coerce_summary_output(value: Any) -> SpeechSummaryOutput:
    if isinstance(value, SpeechSummaryOutput):
        return value
    if isinstance(value, BaseModel):
        return SpeechSummaryOutput.model_validate(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return SpeechSummaryOutput.model_validate(value)
    if isinstance(value, str):
        return SpeechSummaryOutput.model_validate(json.loads(value))
    raise TypeError(f"Unsupported Speech Summary output: {type(value).__name__}")


def _validate_safe_summary(text: str) -> None:
    if "\n" in text or "\r" in text or _CONTROL_RE.search(text):
        raise SpeechSummarySafetyError("Speech Summary must be a single printable line")
    if "```" in text or _URL_RE.search(text) or _WINDOWS_PATH_RE.search(text) or _POSIX_PATH_RE.search(text):
        raise SpeechSummarySafetyError("Speech Summary contains a prohibited location or code marker")
    if _SECRET_RE.search(text) or _INTERNAL_REASONING_RE.search(text):
        raise SpeechSummarySafetyError("Speech Summary contains prohibited sensitive content")
    stripped = text.strip()
    if (stripped.startswith("{") and stripped.endswith("}")) or (stripped.startswith("[") and stripped.endswith("]")):
        raise SpeechSummarySafetyError("Speech Summary must not be JSON")


def _summary_id(
    *,
    source_kind: SpeechSummarySourceKind,
    message_id: str,
    block_index: int | None,
) -> str:
    if source_kind == "thinking":
        if block_index is None:
            raise ValueError("thinking Speech Summary requires block_index")
        return f"speech:{message_id}:thinking:{block_index}"
    return f"speech:{message_id}:assistant_response"


def _speech_payload(
    *,
    summary_id: str,
    source_kind: SpeechSummarySourceKind,
    message_id: str,
    block_index: int | None,
    text: str,
) -> SpeechSummaryPayload:
    value: JsonObject = {
        "summary_id": summary_id,
        "source_kind": source_kind,
        "message_id": message_id,
        "scope": "main",
        "text": text,
        "char_count": len(text),
    }
    if source_kind == "thinking":
        value["block_index"] = block_index
    return _SPEECH_PAYLOAD_ADAPTER.validate_python(value)


async def _cancel_tasks(tasks: list[asyncio.Task[None]]) -> None:
    if not tasks:
        return
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


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
