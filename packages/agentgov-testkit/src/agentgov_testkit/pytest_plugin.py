from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

import httpx
import pytest

from ._reporting import clear_invocations, invocation_records
from ._transport import AgentGovTestkitError
from .invoke import AgentTestAgent


class _PytestItemResult(TypedDict):
    nodeid: str
    outcome: str
    duration_seconds: float
    phase: str
    detail: str | None


_RESULTS: list[_PytestItemResult] = []


@dataclass
class _AgentGovPytestContext:
    api_base: str
    api_key: str | None
    resolved_commit_sha: str | None
    reporter: object | None

    def pin_commit(self, commit_sha: str) -> None:
        if self.resolved_commit_sha is None:
            self.resolved_commit_sha = commit_sha
            if self.reporter is not None:
                self.reporter.write_line(f"AgentGov resolved commit: {commit_sha}")
            return
        if self.resolved_commit_sha != commit_sha:
            raise AgentGovTestkitError(
                f"AgentGov test session resolved a different commit inside one pytest session: expected {self.resolved_commit_sha}, got {commit_sha}"
            )


def pytest_configure(config: pytest.Config) -> None:
    del config
    _RESULTS.clear()
    clear_invocations()


@pytest.fixture(scope="session")
def _agentgov_pytest_context(request: pytest.FixtureRequest) -> _AgentGovPytestContext:
    api_base = _required_env("AGENTGOV_API_BASE").rstrip("/")
    api_key = os.getenv("AGENTGOV_API_KEY")
    reporter = request.config.pluginmanager.get_plugin("terminalreporter")
    resolved_commit_sha = (os.getenv("AGENTGOV_COMMIT_SHA") or "").strip() or None
    if reporter is not None and resolved_commit_sha:
        reporter.write_line(f"AgentGov resolved commit: {resolved_commit_sha}")
    return _AgentGovPytestContext(
        api_base=api_base,
        api_key=api_key,
        resolved_commit_sha=resolved_commit_sha,
        reporter=reporter,
    )


@pytest.fixture
def agent(_agentgov_pytest_context: _AgentGovPytestContext) -> Iterator[AgentTestAgent]:
    context = _agentgov_pytest_context
    session_id, resolved_commit_sha = _create_session(
        context.api_base,
        context.api_key,
        commit_sha=context.resolved_commit_sha,
    )
    context.pin_commit(resolved_commit_sha)
    client = AgentTestAgent(
        api_base=context.api_base,
        test_session_id=session_id,
        resolved_commit_sha=resolved_commit_sha,
        api_key=context.api_key,
    )
    try:
        yield client
    finally:
        client.close()


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.when != "call" and not (report.when == "setup" and report.failed):
        return
    _RESULTS.append(
        {
            "nodeid": report.nodeid,
            "outcome": report.outcome,
            "duration_seconds": report.duration,
            "phase": report.when,
            "detail": str(report.longrepr) if report.failed else None,
        }
    )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    raw_path = os.getenv("AGENTGOV_TEST_REPORT_PATH")
    if not raw_path:
        return
    payload = {
        "exit_code": int(exitstatus),
        "items": _RESULTS,
        "invocations": invocation_records(),
    }
    path = Path(raw_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _create_session(
    api_base: str,
    api_key: str | None,
    *,
    commit_sha: str | None,
) -> tuple[str, str]:
    agent_id = _required_env("AGENTGOV_AGENT_ID")
    body = {
        "agent_id": agent_id,
        "commit_sha": commit_sha,
        "change_set_id": os.getenv("AGENTGOV_CHANGE_SET_ID") or None,
    }
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        response = httpx.post(
            f"{api_base}/api/agent-test-sessions",
            json=body,
            headers=headers,
            timeout=30.0,
        )
        response.raise_for_status()
        payload = response.json()
        session_id = payload.get("test_session_id")
        commit_sha = payload.get("commit_sha")
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        raise AgentGovTestkitError(f"Cannot create AgentGov test session: {exc}") from exc
    if not isinstance(session_id, str) or not session_id:
        raise AgentGovTestkitError("AgentGov test session response has no test_session_id")
    if not isinstance(commit_sha, str) or not commit_sha:
        raise AgentGovTestkitError("AgentGov test session response has no resolved commit_sha")
    return session_id, commit_sha


def _required_env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise AgentGovTestkitError(f"{name} is required")
    return value
