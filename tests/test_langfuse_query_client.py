from __future__ import annotations

from types import SimpleNamespace

import pytest
from app.runtime.integrations.runtime_langfuse import RuntimeLangfuseClient
from app.runtime.settings import AppSettings
from langfuse.api import client as langfuse_api


@pytest.mark.parametrize(
    "config",
    [
        {"LANGFUSE_ENABLED": False},
        {"LANGFUSE_PUBLIC_KEY": ""},
        {"LANGFUSE_SECRET_KEY": ""},
    ],
)
def test_unconfigured_query_does_not_open_a_client(monkeypatch: pytest.MonkeyPatch, config: dict[str, object]) -> None:
    def unexpected_client(**kwargs):
        pytest.fail("disabled or unconfigured query must not create a client")

    monkeypatch.setattr(langfuse_api, "LangfuseAPI", unexpected_client)
    settings = AppSettings(
        _env_file=None,
        **({"LANGFUSE_ENABLED": True, "LANGFUSE_PUBLIC_KEY": "test-public", "LANGFUSE_SECRET_KEY": "test-secret"} | config),
    )

    assert RuntimeLangfuseClient(settings).fetch_trace("test-trace") is None


def test_query_uses_shared_project_identity_and_excludes_content_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    def get(trace_id: str, *, fields: str):
        calls.append((trace_id, fields))
        return {"id": trace_id, "observations": []}

    def client(**kwargs):
        assert kwargs["base_url"] == "http://langfuse.test"
        assert kwargs["username"] == "test-public"
        assert kwargs["password"] == "test-secret"
        return SimpleNamespace(trace=SimpleNamespace(get=get))

    monkeypatch.setattr(langfuse_api, "LangfuseAPI", client)
    settings = AppSettings(
        _env_file=None,
        LANGFUSE_ENABLED=True,
        LANGFUSE_BASE_URL="http://langfuse.test",
        LANGFUSE_PUBLIC_KEY="test-public",
        LANGFUSE_SECRET_KEY="test-secret",
    )

    assert RuntimeLangfuseClient(settings).fetch_trace("test-trace") == {"id": "test-trace", "observations": []}
    assert calls == [("test-trace", "core,scores,observations,metrics")]


def test_query_error_does_not_return_credentials_or_upstream_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def client(**kwargs):
        raise RuntimeError("test-secret or upstream body must not escape")

    monkeypatch.setattr(langfuse_api, "LangfuseAPI", client)
    settings = AppSettings(
        _env_file=None,
        LANGFUSE_ENABLED=True,
        LANGFUSE_PUBLIC_KEY="test-public",
        LANGFUSE_SECRET_KEY="test-secret",
    )

    assert RuntimeLangfuseClient(settings).fetch_trace("test-trace") == {"fetch_status": "failed", "error_type": "RuntimeError"}
