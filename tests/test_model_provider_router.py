from __future__ import annotations

import logging
from urllib.error import URLError

import pytest
from app.runtime import model_provider
from app.runtime.agent_job_errors import (
    LITELLM_CLAUDE_CODE_COMPAT_FAILED,
    MODEL_AGENT_LOOP_CAPABILITY_FAILED,
    MODEL_PROVIDER_SIDECAR_UNAVAILABLE,
    MODEL_SCHEMA_EXACT_OUTPUT_FAILED,
    VLLM_CHAT_PROBE_FAILED,
    VLLM_TOOL_CALLING_UNSUPPORTED,
    ModelProviderCapabilityError,
)
from app.runtime.model_provider import (
    LITELLM_SIDECAR_BASE_URL,
    LOCAL_PROVIDER_DUMMY_API_KEY,
    VLLM_VERSION_PROBE_FAILED,
    ModelProviderRouter,
    VersionProbeResult,
    provider_v1_api_base,
    version_probe_url,
)
from app.runtime.settings import AppSettings


class _FakeResponse:
    status = 200

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def getcode(self) -> int:
        return self.status

    def read(self, _: int) -> bytes:
        return self.payload


def _request_json(request) -> dict:
    data = getattr(request, "data", None)
    if not data:
        return {}
    return __import__("json").loads(data.decode("utf-8"))


def _fake_capability_response(request) -> _FakeResponse:
    payload = _request_json(request)
    if request.full_url.endswith("/version"):
        return _FakeResponse(b'{"version":"0.14.0"}')
    if request.full_url.endswith("/health/readiness"):
        return _FakeResponse(b"{}")
    if request.full_url.endswith("/v1/models"):
        return _FakeResponse(b'{"data":[{"id":"agent-gov-model"}]}')
    if request.full_url.endswith("/v1/chat/completions"):
        if any(isinstance(item, dict) and item.get("role") == "tool" for item in payload.get("messages", [])):
            return _FakeResponse(b'{"choices":[{"message":{"content":"DONE"}}]}')
        if payload.get("tools"):
            return _FakeResponse(b'{"choices":[{"message":{"tool_calls":[{"id":"call_1","type":"function","function":{"name":"agent_gov_probe","arguments":"{\\"value\\":\\"ok\\"}"}}]}}]}')
        if payload.get("response_format"):
            return _FakeResponse(b'{"choices":[{"message":{"content":"{\\"ok\\": true}"}}]}')
        return _FakeResponse(b'{"choices":[{"message":{"content":"OK"}}]}')
    if request.full_url.endswith("/v1/messages"):
        if payload.get("stream"):
            return _FakeResponse(b"event: message_start\n\n")
        return _FakeResponse(b'{"content":[{"type":"tool_use","id":"toolu_1","name":"agent_gov_probe","input":{"value":"ok"}}]}')
    raise AssertionError(request.full_url)


def _settings(**kwargs: object) -> AppSettings:
    return AppSettings(_env_file=None, **kwargs)


def test_anthropic_compatible_route_uses_direct_provider_url_without_version_probe(monkeypatch) -> None:
    def fail_urlopen(*_: object, **__: object) -> None:
        raise AssertionError("anthropic_compatible must not call vLLM /version")

    monkeypatch.setattr(model_provider, "urlopen", fail_urlopen)
    settings = _settings(
        MODEL_PROVIDER_BACKEND="anthropic_compatible",
        MODEL_PROVIDER_API_KEY="sk-test",
        MODEL_PROVIDER_API_URL="https://model-gateway.example.test/anthropic",
    )

    route = ModelProviderRouter(settings).route()

    assert route.route == "direct_anthropic"
    assert route.claude_env(settings.provider_api_key) == {
        "ANTHROPIC_API_KEY": "sk-test",
        "ANTHROPIC_BASE_URL": "https://model-gateway.example.test/anthropic",
    }
    assert route.version_probe == VersionProbeResult(status="skipped")


def test_vllm_route_probes_version_and_derives_litellm_sidecar(monkeypatch) -> None:
    seen: list[str] = []

    def fake_urlopen(request, timeout: float):
        seen.append(request.full_url)
        assert timeout == 3.0
        return _FakeResponse(b'{"version":"0.14.0"}')

    monkeypatch.setattr(model_provider, "urlopen", fake_urlopen)
    settings = _settings(MODEL_PROVIDER_BACKEND="vllm", MODEL_PROVIDER_API_URL="http://vllm:8000/")
    router = ModelProviderRouter(settings)

    route = router.route()

    assert seen == ["http://vllm:8000/version"]
    assert route.backend == "vllm"
    assert route.route == "litellm_sidecar"
    assert route.sidecar_required is True
    assert route.claude_base_url == LITELLM_SIDECAR_BASE_URL
    assert route.formatter_api_base == LITELLM_SIDECAR_BASE_URL
    assert route.formatter_model_prefix == "openai"
    assert route.version_probe.status == "succeeded"
    assert route.version_probe.version == "0.14.0"
    assert router.claude_env() == {
        "ANTHROPIC_API_KEY": LOCAL_PROVIDER_DUMMY_API_KEY,
        "ANTHROPIC_BASE_URL": LITELLM_SIDECAR_BASE_URL,
    }


def test_vllm_version_probe_failure_warns_sanitized_and_falls_back_to_sidecar(monkeypatch, caplog) -> None:
    model_provider._WARNING_LAST_EMITTED_AT.clear()

    def fake_urlopen(*_: object, **__: object):
        raise URLError(TimeoutError())

    monkeypatch.setattr(model_provider, "urlopen", fake_urlopen)
    settings = _settings(
        MODEL_PROVIDER_BACKEND="vllm",
        MODEL_PROVIDER_API_URL="http://user:secret@vllm:8000/private?token=hidden",
        MODEL_PROVIDER_WARNING_TTL_SECONDS=0,
    )

    with caplog.at_level(logging.WARNING, logger="app.runtime.model_provider"):
        route = ModelProviderRouter(settings).route()

    assert route.route == "litellm_sidecar"
    assert route.version_probe.status == "failed"
    assert route.version_probe.error_code == VLLM_VERSION_PROBE_FAILED
    assert route.version_probe.reason == "timeout"
    log_text = caplog.text
    assert "event=VLLM_VERSION_PROBE_FAILED" in log_text
    assert "provider_endpoint=http://vllm:8000" in log_text
    assert "fallback_to_litellm_sidecar" in log_text
    assert "secret" not in log_text
    assert "token=hidden" not in log_text
    assert "/private" not in log_text


def test_vllm_route_without_provider_url_fails_credentials_precheck() -> None:
    settings = _settings(MODEL_PROVIDER_BACKEND="vllm", MODEL_PROVIDER_API_URL="")
    router = ModelProviderRouter(settings)

    assert router.route().version_probe.reason == "missing_provider_endpoint"
    assert router.provider_credentials_configured() is False


def test_provider_url_derivatives_do_not_use_second_upstream_url() -> None:
    assert version_probe_url("http://vllm:8000/") == "http://vllm:8000/version"
    assert provider_v1_api_base("http://vllm:8000") == "http://vllm:8000/v1"


def test_vllm_capability_gate_checks_sidecar_and_models(monkeypatch) -> None:
    seen: list[str] = []

    def fake_urlopen(request, timeout: float):
        seen.append(request.full_url)
        return _fake_capability_response(request)

    monkeypatch.setattr(model_provider, "urlopen", fake_urlopen)
    router = ModelProviderRouter(_settings(MODEL_PROVIDER_BACKEND="vllm", MODEL_PROVIDER_API_URL="http://vllm:8000"))

    router.ensure_agent_runtime_ready()
    router.ensure_agent_runtime_ready()

    assert seen == [
        "http://vllm:8000/version",
        "http://agent-gov-litellm-sidecar:4000/health/readiness",
        "http://vllm:8000/v1/models",
        "http://vllm:8000/v1/chat/completions",
        "http://vllm:8000/v1/chat/completions",
        "http://vllm:8000/v1/chat/completions",
        "http://vllm:8000/v1/chat/completions",
        "http://agent-gov-litellm-sidecar:4000/v1/messages",
        "http://agent-gov-litellm-sidecar:4000/v1/messages",
    ]


def test_vllm_capability_gate_reports_sidecar_unavailable(monkeypatch) -> None:
    def fake_urlopen(request, timeout: float):
        if request.full_url.endswith("/version"):
            return _FakeResponse(b'{"version":"0.14.0"}')
        raise URLError(ConnectionError("refused"))

    monkeypatch.setattr(model_provider, "urlopen", fake_urlopen)
    router = ModelProviderRouter(_settings(MODEL_PROVIDER_BACKEND="vllm", MODEL_PROVIDER_API_URL="http://vllm:8000"))

    with pytest.raises(ModelProviderCapabilityError) as exc_info:
        router.ensure_agent_runtime_ready()

    assert exc_info.value.error_code == MODEL_PROVIDER_SIDECAR_UNAVAILABLE
    assert exc_info.value.raw_output_json is not None
    assert exc_info.value.raw_output_json["probe"] == "sidecar_readiness"
    assert exc_info.value.raw_output_json["route"] == "litellm_sidecar"


@pytest.mark.parametrize(
    ("broken_probe", "expected_code"),
    [
        ("chat", VLLM_CHAT_PROBE_FAILED),
        ("tool_calling", VLLM_TOOL_CALLING_UNSUPPORTED),
        ("agent_tool_loop", MODEL_AGENT_LOOP_CAPABILITY_FAILED),
        ("schema_exact_json", MODEL_SCHEMA_EXACT_OUTPUT_FAILED),
        ("claude_tool_compat", LITELLM_CLAUDE_CODE_COMPAT_FAILED),
        ("claude_streaming", LITELLM_CLAUDE_CODE_COMPAT_FAILED),
    ],
)
def test_vllm_capability_gate_reports_specific_probe_failures(monkeypatch, broken_probe: str, expected_code: str) -> None:
    def fake_urlopen(request, timeout: float):
        payload = _request_json(request)
        if request.full_url.endswith("/v1/chat/completions"):
            if broken_probe == "chat" and not payload.get("tools") and not payload.get("response_format"):
                return _FakeResponse(b'{"choices":[]}')
            if broken_probe == "tool_calling" and payload.get("tools"):
                return _FakeResponse(b'{"choices":[{"message":{"content":"I cannot call tools."}}]}')
            if broken_probe == "agent_tool_loop" and any(
                isinstance(item, dict) and item.get("role") == "tool" for item in payload.get("messages", [])
            ):
                return _FakeResponse(b'{"choices":[{"message":{"content":""}}]}')
            if broken_probe == "schema_exact_json" and payload.get("response_format"):
                return _FakeResponse(b'{"choices":[{"message":{"content":"not json"}}]}')
        if request.full_url.endswith("/v1/messages"):
            if broken_probe == "claude_tool_compat" and payload.get("tools"):
                return _FakeResponse(b'{"content":[{"type":"text","text":"no tool"}]}')
            if broken_probe == "claude_streaming" and payload.get("stream"):
                return _FakeResponse(b"event: error\n\ndata: {}\n\n")
        return _fake_capability_response(request)

    monkeypatch.setattr(model_provider, "urlopen", fake_urlopen)
    router = ModelProviderRouter(_settings(MODEL_PROVIDER_BACKEND="vllm", MODEL_PROVIDER_API_URL="http://vllm:8000"))

    with pytest.raises(ModelProviderCapabilityError) as exc_info:
        router.ensure_agent_runtime_ready()

    assert exc_info.value.error_code == expected_code
    assert exc_info.value.raw_output_json is not None
    assert exc_info.value.raw_output_json["error_code"] == expected_code
    assert exc_info.value.raw_output_json["probe"] == broken_probe
