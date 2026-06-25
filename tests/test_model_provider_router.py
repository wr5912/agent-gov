from __future__ import annotations

import logging
from urllib.error import URLError

import pytest
from app.runtime import model_provider
from app.runtime.agent_job_errors import MODEL_PROVIDER_SIDECAR_UNAVAILABLE, ModelProviderCapabilityError
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
        if request.full_url.endswith("/version"):
            return _FakeResponse(b'{"version":"0.14.0"}')
        if request.full_url.endswith("/health/readiness"):
            return _FakeResponse(b"{}")
        if request.full_url.endswith("/v1/models"):
            return _FakeResponse(b'{"data":[]}')
        raise AssertionError(request.full_url)

    monkeypatch.setattr(model_provider, "urlopen", fake_urlopen)
    router = ModelProviderRouter(_settings(MODEL_PROVIDER_BACKEND="vllm", MODEL_PROVIDER_API_URL="http://vllm:8000"))

    router.ensure_agent_runtime_ready()
    router.ensure_agent_runtime_ready()

    assert seen == [
        "http://vllm:8000/version",
        "http://agent-gov-litellm-sidecar:4000/health/readiness",
        "http://vllm:8000/v1/models",
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
