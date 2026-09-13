from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, Request

from agentgov_testkit import AgentGovTestkitError, invoke_agent, pytest_plugin
from runtime_loopback import serve_loopback


def test_invoke_agent_rejects_empty_input_and_missing_live_context(process_environment) -> None:
    process_environment.remove("AGENTGOV_API_BASE")
    process_environment.remove("AGENTGOV_TEST_SESSION_ID")

    with pytest.raises(ValueError, match="message must not be empty"):
        invoke_agent(" ", api_base="http://127.0.0.1:50401", test_session_id="ats-1")
    with pytest.raises(AgentGovTestkitError, match="AGENTGOV_API_BASE is required"):
        invoke_agent("hello", test_session_id="ats-1")


def test_pytest_context_rejects_commit_drift() -> None:
    context = pytest_plugin._AgentGovPytestContext(
        api_base="http://127.0.0.1:50401",
        api_key=None,
        resolved_commit_sha="a" * 40,
        reporter=None,
    )

    with pytest.raises(AgentGovTestkitError, match="different commit"):
        context.pin_commit("b" * 40)


def test_pytest_session_forwards_ephemeral_run_attestation_over_real_http(process_environment) -> None:
    app = FastAPI()
    captured: dict[str, str] = {}

    @app.post("/api/agent-test-sessions")
    async def create_session(request: Request) -> dict[str, str]:
        captured["run_id"] = request.headers.get("X-AgentGov-Test-Run-Id", "")
        captured["attestation"] = request.headers.get("X-AgentGov-Test-Run-Attestation", "")
        return {"test_session_id": "ats-real", "commit_sha": "a" * 40}

    process_environment.set("AGENTGOV_AGENT_ID", "agent-a")
    process_environment.set("AGENTGOV_TEST_RUN_ID", "atr-real")
    process_environment.set("AGENTGOV_TEST_RUN_ATTESTATION", "ephemeral-proof")
    with serve_loopback(app) as api_base:
        session_id, commit_sha = pytest_plugin._create_session(api_base, None, commit_sha="a" * 40)

    assert (session_id, commit_sha) == ("ats-real", "a" * 40)
    assert captured == {"run_id": "atr-real", "attestation": "ephemeral-proof"}


def test_invoke_agent_accepts_canonical_trace_field_names_over_real_http() -> None:
    app = FastAPI()

    @app.post("/api/agent-test-sessions/ats-real/messages")
    def invoke() -> dict[str, object]:
        return {
            "answer": "real answer",
            "run_id": "run-real",
            "session_id": "session-real",
            "agent_version_id": "a" * 40,
            "trace_id": "b" * 32,
            "trace_url": "http://127.0.0.1:50402/trace/real",
            "errors": [],
        }

    with serve_loopback(app) as api_base:
        result = invoke_agent("real input", api_base=api_base, test_session_id="ats-real")

    assert result.langfuse_trace_id == "b" * 32
    assert result.langfuse_trace_url == "http://127.0.0.1:50402/trace/real"


def test_pytest_plugin_records_real_pytest_call_and_setup_failure(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    test_module = tmp_path / "test_real_pytest_protocol.py"
    test_module.write_text(
        """import pytest


def test_pass():
    assert 2 + 2 == 4


@pytest.fixture
def broken_resource():
    raise RuntimeError("resource setup failed")


def test_setup_failure(broken_resource):
    raise AssertionError("call phase must not run")
""",
        encoding="utf-8",
    )
    package_src = Path(__file__).resolve().parents[1] / "packages" / "agentgov-testkit" / "src"
    environment = dict(os.environ)
    environment["AGENTGOV_TEST_REPORT_PATH"] = str(report_path)
    environment["PYTHONPATH"] = os.pathsep.join(item for item in (str(package_src), environment.get("PYTHONPATH", "")) if item)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "agentgov_testkit.pytest_plugin",
            str(test_module),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["exit_code"] == 1
    assert payload["invocations"] == []
    assert [(item["outcome"], item["phase"]) for item in payload["items"]] == [
        ("passed", "call"),
        ("failed", "setup"),
    ]
    assert "resource setup failed" in payload["items"][1]["detail"]
