from __future__ import annotations

import base64

import pytest
import requests
from agentscope_runtime import observability
from agentscope_runtime.otel_config import runtime_otel_config
from opentelemetry import trace


def _enabled_env() -> dict[str, str]:
    return {
        "LANGFUSE_ENABLED": "true",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://langfuse.test/api/public/otel",
        "AGENTGOV_OTEL_PUBLIC_KEY": "test-public",
        "AGENTGOV_OTEL_SECRET_KEY": "test-secret",
    }


@pytest.mark.parametrize("enabled", [None, "false", "0", "off"])
def test_disabled_telemetry_ignores_existing_or_invalid_exporter_configuration(enabled: str | None) -> None:
    environ = _enabled_env() | {"OTEL_EXPORTER_OTLP_HEADERS": "invalid-private-value"}
    if enabled is None:
        del environ["LANGFUSE_ENABLED"]
    else:
        environ["LANGFUSE_ENABLED"] = enabled

    assert runtime_otel_config(environ) is None


def test_runtime_derives_authenticated_ingestion_from_one_credential_pair() -> None:
    config = runtime_otel_config(_enabled_env())

    assert config is not None
    assert config.endpoint == "http://langfuse.test/api/public/otel/v1/traces"
    assert config.headers == {
        "authorization": f"Basic {base64.b64encode(b'test-public:test-secret').decode()}",
        "x-langfuse-ingestion-version": "4",
    }
    assert "test-secret" not in repr(config)
    assert config.headers["authorization"] not in repr(config)


@pytest.mark.parametrize("base", ["http://langfuse.test", "http://langfuse.test/"])
def test_ingestion_base_derives_one_correct_otel_path(base: str) -> None:
    environ = _enabled_env() | {"OTEL_EXPORTER_OTLP_ENDPOINT": "", "AGENTGOV_OTEL_BASE_URL": base}

    config = runtime_otel_config(environ)

    assert config is not None
    assert config.endpoint == "http://langfuse.test/api/public/otel/v1/traces"


def test_standard_otlp_endpoint_takes_precedence_over_ingestion_base() -> None:
    config = runtime_otel_config(_enabled_env() | {"AGENTGOV_OTEL_BASE_URL": "http://unused-langfuse.test"})

    assert config is not None
    assert config.endpoint == "http://langfuse.test/api/public/otel/v1/traces"


def test_explicit_otlp_headers_and_trace_endpoint_take_precedence() -> None:
    config = runtime_otel_config(
        {
            "LANGFUSE_ENABLED": "true",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.test/base",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://collector.test/custom/traces",
            "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer%20test-token,x-custom=test%3Dvalue",
        },
    )

    assert config is not None
    assert config.endpoint == "http://collector.test/custom/traces"
    assert config.headers == {"authorization": "Bearer test-token", "x-custom": "test=value"}


def test_trace_headers_override_generic_headers() -> None:
    config = runtime_otel_config(
        _enabled_env()
        | {
            "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer generic-token",
            "OTEL_EXPORTER_OTLP_TRACES_HEADERS": "Authorization=Bearer traces-token",
        },
    )

    assert config is not None
    assert config.headers == {"authorization": "Bearer traces-token"}


@pytest.mark.parametrize("missing", ["OTEL_EXPORTER_OTLP_ENDPOINT", "AGENTGOV_OTEL_PUBLIC_KEY", "AGENTGOV_OTEL_SECRET_KEY"])
def test_enabled_telemetry_rejects_incomplete_configuration(missing: str) -> None:
    environ = _enabled_env()
    del environ[missing]

    with pytest.raises(ValueError, match="LANGFUSE_ENABLED") as error:
        runtime_otel_config(environ)

    assert "test-secret" not in str(error.value)


@pytest.mark.parametrize("header", ["invalid-private-value", "Authorization=secret%0D%0AX-Injected%3Ayes", "Bad%20Name=secret"])
def test_invalid_otlp_headers_fail_without_exposing_values(header: str, caplog: pytest.LogCaptureFixture) -> None:
    with pytest.raises(ValueError, match="OTLP headers") as error:
        runtime_otel_config(_enabled_env() | {"OTEL_EXPORTER_OTLP_HEADERS": header})

    assert header not in str(error.value)
    assert "secret" not in str(error.value)
    assert header not in caplog.text


@pytest.mark.parametrize("endpoint", ["", "not-a-url", "http://user:secret@collector.test"])
def test_invalid_endpoint_fails_without_exposing_values(endpoint: str) -> None:
    with pytest.raises(ValueError, match="OTLP HTTP endpoint") as error:
        runtime_otel_config(_enabled_env() | {"OTEL_EXPORTER_OTLP_ENDPOINT": endpoint})

    assert "secret" not in str(error.value)


def test_invalid_enabled_flag_fails_closed() -> None:
    with pytest.raises(ValueError, match="LANGFUSE_ENABLED must be a boolean"):
        runtime_otel_config(_enabled_env() | {"LANGFUSE_ENABLED": "typo"})


def test_disabled_exporter_does_not_initialize_a_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGFUSE_ENABLED", "false")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.test")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Bearer test-token")

    def unexpected_provider_access():
        pytest.fail("disabled telemetry must not initialize or acquire a provider")

    monkeypatch.setattr(trace, "get_tracer_provider", unexpected_provider_access)

    assert observability.configure_otel_from_env() is None


@pytest.mark.parametrize(
    ("resource_attributes", "service_name", "expected_environment", "expected_service"),
    [
        ("", "", "local", "agent-gov-agentscope-runtime"),
        ("deployment.environment.name=staging", "custom-runtime", "staging", "custom-runtime"),
    ],
)
def test_runtime_exports_with_derived_auth_and_resource_defaults(
    monkeypatch: pytest.MonkeyPatch,
    resource_attributes: str,
    service_name: str,
    expected_environment: str,
    expected_service: str,
) -> None:
    for key, value in _enabled_env().items():
        monkeypatch.setenv(key, value)
    for key in ("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_TRACES_HEADERS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", resource_attributes)
    monkeypatch.setenv("OTEL_SERVICE_NAME", service_name)
    monkeypatch.setattr(observability, "_managed_runtime", None)
    providers = [trace.ProxyTracerProvider()]
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: providers[-1])
    monkeypatch.setattr(trace, "set_tracer_provider", providers.append)
    exported: list[tuple[str, str, str]] = []

    def post(session, url, **kwargs):
        del kwargs
        exported.append((url, session.headers["authorization"], session.headers["x-langfuse-ingestion-version"]))
        response = requests.Response()
        response.status_code = 200
        return response

    monkeypatch.setattr(requests.Session, "post", post)
    runtime = observability.configure_otel_from_env()
    assert runtime is not None
    try:
        assert observability.configure_otel_from_env() is runtime
        assert runtime.provider.resource.attributes["deployment.environment.name"] == expected_environment
        assert runtime.provider.resource.attributes["service.name"] == expected_service
        with runtime.provider.get_tracer(__name__).start_as_current_span("agentgov.run"):
            pass
        assert runtime.provider.force_flush()
        assert exported == [
            (
                "http://langfuse.test/api/public/otel/v1/traces",
                f"Basic {base64.b64encode(b'test-public:test-secret').decode()}",
                "4",
            ),
        ]
    finally:
        runtime.shutdown()
