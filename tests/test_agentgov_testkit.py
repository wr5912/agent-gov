from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

TESTKIT_SRC = Path(__file__).resolve().parents[1] / "packages" / "agentgov-testkit" / "src"
if str(TESTKIT_SRC) not in sys.path:
    sys.path.insert(0, str(TESTKIT_SRC))

from agentgov_testkit import (  # noqa: E402
    AgentGovTestkitError,
    AgentInvocation,
    invoke_agent,
    pytest_plugin,  # noqa: E402
)
from agentgov_testkit import _reporting as testkit_reporting  # noqa: E402
from agentgov_testkit import _transport as testkit_transport  # noqa: E402


class _Response:
    def __init__(self, payload: object, *, error: Exception | None = None) -> None:
        self._payload = payload
        self._error = error

    def raise_for_status(self) -> None:
        if self._error is not None:
            raise self._error

    def json(self) -> object:
        return self._payload


def test_invoke_agent_uses_explicit_session_and_preserves_typed_response(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, object] = {}

    def fake_post(url: str, **kwargs: object) -> _Response:
        observed.update(url=url, **kwargs)
        return _Response(
            {
                "answer": "已完成",
                "run_id": "run-1",
                "session_id": "session-1",
                "agent_version_id": "commit-1",
                "langfuse_trace_id": "trace-1",
                "langfuse_trace_url": "http://langfuse.local/project/demo/traces/trace-1",
                "errors": ["warning"],
                "extra": {"kept": True},
            }
        )

    monkeypatch.setattr(testkit_transport.httpx, "post", fake_post)
    result = invoke_agent(
        "  核验告警  ",
        metadata={"case": "one"},
        api_base="http://agent-gov.local/",
        api_key="test-key",
        test_session_id="ats-1",
        timeout_seconds=12,
    )

    assert observed == {
        "url": "http://agent-gov.local/api/agent-test-sessions/ats-1/messages",
        "json": {"message": "核验告警", "metadata": {"case": "one"}},
        "headers": {"Authorization": "Bearer test-key"},
        "timeout": 12,
    }
    assert result.text == "已完成"
    assert result.run_id == "run-1"
    assert result.session_id == "session-1"
    assert result.agent_version_id == "commit-1"
    assert result.langfuse_trace_id == "trace-1"
    assert result.langfuse_trace_url == "http://langfuse.local/project/demo/traces/trace-1"
    assert result.errors == ("warning",)
    assert result.raw["extra"] == {"kept": True}


def test_invoke_agent_rejects_missing_context_and_wraps_http_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTGOV_API_BASE", raising=False)
    monkeypatch.delenv("AGENTGOV_TEST_SESSION_ID", raising=False)
    with pytest.raises(ValueError, match="message must not be empty"):
        invoke_agent(" ", api_base="http://agent-gov.local", test_session_id="ats-1")
    with pytest.raises(AgentGovTestkitError, match="AGENTGOV_API_BASE is required"):
        invoke_agent("hello", test_session_id="ats-1")

    request = httpx.Request("POST", "http://agent-gov.local/api/agent-test-sessions/ats-1/messages")
    failure = httpx.ConnectError("offline", request=request)
    monkeypatch.setattr(testkit_transport.httpx, "post", lambda *args, **kwargs: (_ for _ in ()).throw(failure))
    with pytest.raises(AgentGovTestkitError, match="AgentGov test invocation failed"):
        invoke_agent("hello", api_base="http://agent-gov.local", test_session_id="ats-1")


def test_pytest_fixture_isolates_each_test_and_pins_one_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    assert pytest_plugin.agent._fixture_function_marker.scope == "function"
    assert pytest_plugin._agentgov_pytest_context._fixture_function_marker.scope == "session"
    closed: list[str] = []
    created: list[tuple[str, str | None]] = []
    monkeypatch.setattr(pytest_plugin.AgentTestAgent, "close", lambda self: closed.append(self.test_session_id))
    monkeypatch.setenv("AGENTGOV_API_BASE", "http://agent-gov.local")
    monkeypatch.setenv("AGENTGOV_AGENT_ID", "soc-agent")
    monkeypatch.delenv("AGENTGOV_COMMIT_SHA", raising=False)
    monkeypatch.setenv("AGENTGOV_TEST_SESSION_ID", "external-session-must-not-be-reused")

    def fake_create(api_base: str, api_key: str | None, *, commit_sha: str | None) -> tuple[str, str]:
        created.append((api_base, commit_sha))
        return f"created-session-{len(created)}", "b" * 40

    monkeypatch.setattr(pytest_plugin, "_create_session", fake_create)
    request = SimpleNamespace(config=SimpleNamespace(pluginmanager=SimpleNamespace(get_plugin=lambda _name: None)))
    context = pytest_plugin._agentgov_pytest_context.__wrapped__(request)

    first = pytest_plugin.agent.__wrapped__(context)
    assert next(first).test_session_id == "created-session-1"
    with pytest.raises(StopIteration):
        next(first)
    second = pytest_plugin.agent.__wrapped__(context)
    assert next(second).test_session_id == "created-session-2"
    with pytest.raises(StopIteration):
        next(second)

    assert created == [
        ("http://agent-gov.local", None),
        ("http://agent-gov.local", "b" * 40),
    ]
    assert closed == ["created-session-1", "created-session-2"]


def test_pytest_context_rejects_commit_drift() -> None:
    context = pytest_plugin._AgentGovPytestContext(
        api_base="http://agent-gov.local",
        api_key=None,
        resolved_commit_sha="a" * 40,
        reporter=None,
    )
    with pytest.raises(AgentGovTestkitError, match="different commit"):
        context.pin_commit("b" * 40)


def test_pytest_plugin_writes_machine_readable_call_and_setup_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "report.json"
    monkeypatch.setenv("AGENTGOV_TEST_REPORT_PATH", str(report_path))
    pytest_plugin.pytest_configure(SimpleNamespace())
    testkit_reporting.record_invocation(
        AgentInvocation(
            text="完成",
            run_id="run-1",
            session_id="session-1",
            agent_version_id="commit-1",
            langfuse_trace_id="trace-1",
            langfuse_trace_url="http://langfuse.local/project/demo/traces/trace-1",
            errors=(),
            raw={},
        )
    )
    pytest_plugin.pytest_runtest_logreport(
        SimpleNamespace(
            when="call",
            nodeid="tests/test_case.py::test_pass",
            outcome="passed",
            duration=0.1,
            failed=False,
            longrepr="",
        )
    )
    pytest_plugin.pytest_runtest_logreport(
        SimpleNamespace(
            when="setup",
            nodeid="tests/test_case.py::test_setup",
            outcome="failed",
            duration=0.2,
            failed=True,
            longrepr="fixture failed",
        )
    )
    pytest_plugin.pytest_sessionfinish(SimpleNamespace(), 1)

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["exit_code"] == 1
    assert payload["invocations"] == [
        {
            "run_id": "run-1",
            "session_id": "session-1",
            "agent_version_id": "commit-1",
            "langfuse_trace_id": "trace-1",
            "langfuse_trace_url": "http://langfuse.local/project/demo/traces/trace-1",
            "errors": [],
        }
    ]
    assert payload["items"] == [
        {
            "nodeid": "tests/test_case.py::test_pass",
            "outcome": "passed",
            "duration_seconds": 0.1,
            "phase": "call",
            "detail": None,
        },
        {
            "nodeid": "tests/test_case.py::test_setup",
            "outcome": "failed",
            "duration_seconds": 0.2,
            "phase": "setup",
            "detail": "fixture failed",
        },
    ]
