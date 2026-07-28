from __future__ import annotations

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from app.runtime.settings import AppSettings
from app.runtime.speech_summary import SpeechSummaryOutput, SpeechSummaryService


class _Predictor:
    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, str]] = []

    async def acall(self, **kwargs: str) -> object:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return SimpleNamespace(
            summary=outcome,
            get_lm_usage=lambda: {"speech_summary_tokens": 7},
        )


def _service(
    monkeypatch: pytest.MonkeyPatch,
    predictor: _Predictor,
    *,
    timeout: float = 1,
) -> SpeechSummaryService:
    monkeypatch.setattr(
        "app.runtime.speech_summary.dspy.context",
        lambda **_kwargs: nullcontext(),
    )
    service = SpeechSummaryService(
        AppSettings(
            _env_file=None,
            SPEECH_SUMMARY_TIMEOUT_SECONDS=timeout,
        ),
        predictor=predictor,
    )
    monkeypatch.setattr(service, "_lm_instance", lambda: object())
    return service


def _generate(service: SpeechSummaryService) -> SpeechSummaryOutput | None:
    return asyncio.run(
        service.generate(
            source_kind="thinking",
            source_text="正在核对告警证据并关联攻击链路。",
            run_id="run-1",
            message_id="msg-1",
            block_index=0,
        )
    )


def test_typed_output_is_trimmed_and_contains_only_agent_owned_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    predictor = _Predictor({"text": "  正在核对告警证据与关键攻击链路  "})
    output = _generate(_service(monkeypatch, predictor))

    assert output == SpeechSummaryOutput(text="正在核对告警证据与关键攻击链路")
    assert output.model_dump() == {"text": "正在核对告警证据与关键攻击链路"}
    schema = SpeechSummaryOutput.model_json_schema()
    assert set(schema["properties"]) == {"text"}
    assert schema["additionalProperties"] is False


def test_contract_failure_gets_exactly_one_repair_without_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    predictor = _Predictor(
        {"text": "太短"},
        {"text": "已完成关键证据核对并形成可用结论"},
    )
    output = _generate(_service(monkeypatch, predictor))

    assert output is not None
    assert output.text == "已完成关键证据核对并形成可用结论"
    assert len(predictor.calls) == 2
    assert predictor.calls[0]["repair_instruction"] == ""
    assert "typed 契约" in predictor.calls[1]["repair_instruction"]


def test_second_invalid_output_is_silently_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    predictor = _Predictor(
        {"text": "短"},
        {"text": "仍短"},
    )

    assert _generate(_service(monkeypatch, predictor)) is None
    assert len(predictor.calls) == 2


def test_provider_failure_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    predictor = _Predictor(RuntimeError("provider unavailable"))

    assert _generate(_service(monkeypatch, predictor)) is None
    assert len(predictor.calls) == 1


def test_hostile_backend_owned_fields_cannot_enter_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    predictor = _Predictor(
        {
            "text": "正在核对告警证据与关键攻击链路",
            "run_id": "attacker-run",
            "scope": "subagent",
        },
        {"text": "正在核对告警证据与关键攻击链路"},
    )
    output = _generate(_service(monkeypatch, predictor))

    assert output is not None
    assert output.model_dump() == {"text": "正在核对告警证据与关键攻击链路"}
    assert len(predictor.calls) == 2


@pytest.mark.parametrize(
    "unsafe_text",
    [
        "正在查看 https://example.com 告警详情",
        "正在检查 /data/reports/today.md 中的证据",
        "正在检查 /tmp 目录中的异常配置",
        "正在检查 C:\\tmp 目录中的异常配置",
        "正在检查路径：/tmp中的异常配置",
        "正在检查路径：C:\\tmp中的异常配置",
        "正在核对 api_key sk-abcdefgh123456",
        "正在复述系统提示词和内部推理过程",
        '{"status":"正在核对关键告警证据"}',
    ],
)
def test_unsafe_summary_is_silently_dropped(
    monkeypatch: pytest.MonkeyPatch,
    unsafe_text: str,
) -> None:
    predictor = _Predictor({"text": unsafe_text})

    assert _generate(_service(monkeypatch, predictor)) is None
    assert len(predictor.calls) == 1


def test_timeout_is_silently_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _SlowPredictor(_Predictor):
        async def acall(self, **kwargs: str) -> object:
            self.calls.append(kwargs)
            await asyncio.sleep(1)
            return SimpleNamespace(summary={"text": "正在核对告警证据与关键攻击链路"})

    predictor = _SlowPredictor()

    assert _generate(_service(monkeypatch, predictor, timeout=0.01)) is None
    assert len(predictor.calls) == 1
