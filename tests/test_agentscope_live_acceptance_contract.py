from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts/run_agentscope_live_acceptance.py"


def _load_module() -> ModuleType:
    module_name = "_agentgov_agentscope_live_acceptance_test"
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_live_acceptance_fails_closed_without_explicit_authorization(monkeypatch) -> None:
    live = _load_module()
    monkeypatch.delenv("REQUIRE_LIVE_RUNTIME", raising=False)
    monkeypatch.setenv("AGENT_GOV_CONTAINER_ACCEPTANCE_ACTIVE", "1")
    monkeypatch.setenv("AGENT_GOV_ACCEPTANCE_RUN_ID", "acceptance-test")
    monkeypatch.setenv("AGENT_GOV_CONTAINER_ACCEPTANCE_PROFILE", "core")

    with pytest.raises(live.LiveAcceptanceError, match="REQUIRE_LIVE_RUNTIME=1"):
        live._require_explicit_live_authorization(
            {
                "API_KEY": "private-api-key",
                "MODEL_PROVIDER_API_KEY": "private-provider-key",
                "AGENTSCOPE_MODEL_NAME": "real-model",
            }
        )


def test_live_acceptance_rejects_placeholder_credentials(monkeypatch) -> None:
    live = _load_module()
    monkeypatch.setenv("REQUIRE_LIVE_RUNTIME", "1")
    monkeypatch.setenv("AGENT_GOV_CONTAINER_ACCEPTANCE_ACTIVE", "1")
    monkeypatch.setenv("AGENT_GOV_ACCEPTANCE_RUN_ID", "acceptance-test")
    monkeypatch.setenv("AGENT_GOV_CONTAINER_ACCEPTANCE_PROFILE", "core")

    with pytest.raises(live.LiveAcceptanceError, match="MODEL_PROVIDER_API_KEY"):
        live._require_explicit_live_authorization(
            {
                "API_KEY": "private-api-key",
                "MODEL_PROVIDER_API_KEY": "replace-with-private-provider-key",
                "AGENTSCOPE_MODEL_NAME": "real-model",
            }
        )


def test_cutover_acceptance_requires_identity_bound_to_api_key(monkeypatch) -> None:
    live = _load_module()
    monkeypatch.setenv("REQUIRE_LIVE_RUNTIME", "1")
    monkeypatch.setenv("AGENT_GOV_CONTAINER_ACCEPTANCE_ACTIVE", "1")
    monkeypatch.setenv("AGENT_GOV_ACCEPTANCE_RUN_ID", "acceptance-test")
    monkeypatch.setenv("AGENT_GOV_CONTAINER_ACCEPTANCE_PROFILE", "core")
    env = {
        "API_KEY": "one-time-key",
        "MODEL_PROVIDER_API_KEY": "private-provider-key",
        "AGENTSCOPE_MODEL_NAME": "real-model",
        "AGENTGOV_API_MODE": "acceptance",
        "AGENTGOV_ACCEPTANCE_IDENTITY": "cutover-one",
        "AGENTGOV_ACCEPTANCE_API_KEY": "different-key",
    }

    with pytest.raises(live.LiveAcceptanceError, match="Bearer key"):
        live._require_explicit_live_authorization(env)


def test_fifty_run_mode_requires_fifty_substantively_distinct_scenarios(tmp_path) -> None:
    live = _load_module()
    scenario_file = tmp_path / "scenarios.json"
    scenario_file.write_text(
        json.dumps([{"id": "one", "prompt": "first"}, {"id": "two", "prompt": "second"}]),
        encoding="utf-8",
    )

    scenarios = live.load_scenarios(scenario_file)
    with pytest.raises(live.LiveAcceptanceError, match="不得循环复制场景制造 50-run 证据"):
        live.select_scenarios(scenarios, 50, 10)


def test_live_acceptance_make_target_uses_isolated_public_runner() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    target = makefile.split("container-live-test:", 1)[1].split("\n\n", 1)[0]
    internal = makefile.split("_container-live-test:", 1)[1].split("\n\n", 1)[0]

    assert "REQUIRE_LIVE_RUNTIME" in target
    assert "$(CONTAINER_ACCEPTANCE) --profile langfuse" in target
    assert "scripts/run_agentscope_live_acceptance.py" in internal
    assert "tests/test_live_runtime_acceptance.py" not in makefile
    assert "docker compose run" not in internal


def test_live_acceptance_quality_gap_is_explicit_and_not_claimed_complete() -> None:
    policy = json.loads((REPO_ROOT / "tests/quality_policy.json").read_text(encoding="utf-8"))
    gap = next(item for item in policy["gaps"] if item["id"] == "agentscope-live-cutover-evidence")

    assert gap["target_lane"] == "container-live-acceptance"
    joined = " ".join(gap["acceptance"])
    for requirement in ("50", "10 并发", "3 次", "2 小时", "OTLP", "trace_status=complete"):
        assert requirement in joined


def test_live_acceptance_script_does_not_embed_mock_or_runtime_admin_access() -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")

    assert "MockTransport" not in source
    assert "monkeypatch" not in source
    assert "/internal/" not in source
    assert "agentscope-runtime:8090" not in source
    assert "http://agentscope-runtime" not in source
    assert "X-AgentGov-Acceptance-Identity" in source
    assert '"client_operation_id": client_operation_id' in source


def test_live_acceptance_cli_rejects_default_execution_before_network(tmp_path) -> None:
    env_file = tmp_path / "compose.env"
    env_file.write_text(
        "API_KEY=private-api-key\nMODEL_PROVIDER_API_KEY=private-provider-key\nAGENTSCOPE_MODEL_NAME=real-model\nAPI_BASE=http://127.0.0.1:1\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env.pop("REQUIRE_LIVE_RUNTIME", None)
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--env-file", str(env_file)],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "REQUIRE_LIVE_RUNTIME=1" in result.stderr


def test_complete_trace_requires_langfuse_profile(monkeypatch) -> None:
    live = _load_module()
    monkeypatch.setenv("REQUIRE_LIVE_RUNTIME", "1")
    monkeypatch.setenv("AGENT_GOV_CONTAINER_ACCEPTANCE_ACTIVE", "1")
    monkeypatch.setenv("AGENT_GOV_ACCEPTANCE_RUN_ID", "acceptance-test")
    monkeypatch.setenv("AGENT_GOV_CONTAINER_ACCEPTANCE_PROFILE", "core")
    env = {"API_KEY": "private-api-key", "MODEL_PROVIDER_API_KEY": "private-provider-key", "AGENTSCOPE_MODEL_NAME": "real-model"}

    with pytest.raises(live.LiveAcceptanceError, match="langfuse"):
        live._require_explicit_live_authorization(env, require_trace_complete=True)
    monkeypatch.setenv("AGENT_GOV_CONTAINER_ACCEPTANCE_PROFILE", "langfuse")
    live._require_explicit_live_authorization(env, require_trace_complete=True)


class PublicApiFixture:
    """仅用于脚本契约单测；这些响应不构成真实 Runtime 验收证据。"""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.governance_agent_id = "security-operations-expert"
        self.trace_states = ["complete"]
        self.binding = {
            "governance_agent_id": "security-operations-expert",
            "runtime_agent_id": "runtime-published-one",
            "agent_version_id": "version-one",
            "harness_digest": "b" * 64,
            "provisioned": True,
        }
        self.run = {
            "run_id": "run-one",
            "session_id": "session-one",
            "agent_id": self.binding["governance_agent_id"],
            "runtime_agent_id": self.binding["runtime_agent_id"],
            "agent_version_id": self.binding["agent_version_id"],
            "harness_digest": self.binding["harness_digest"],
            "status": "succeeded",
            "reply_ids": ["reply-one"],
            "persisted_reply_ids": ["reply-one"],
            "trace_id": "a" * 32,
        }
        self.messages = [{"id": "reply-one", "role": "assistant", "finished_reason": "completed", "content": [{"type": "text", "text": "已收到。"}]}]
        self.signal = {
            "signal_id": "signal-one",
            "source_type": "explicit_feedback",
            "run_id": "run-one",
            "matched_run_id": "run-one",
            "session_id": "session-one",
            "agent_id": self.binding["governance_agent_id"],
        }
        self.persisted_signal = dict(self.signal)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/health/ready":
            return httpx.Response(200, json={"ready": True})
        if path.endswith("/provision"):
            assert path == f"/api/runtime/agents/{self.governance_agent_id}/provision"
            return httpx.Response(200, json=self.binding)
        if path == "/api/runtime/sessions/":
            assert json.loads(request.content)["agent_id"] == "runtime-published-one"
            return httpx.Response(200, json={"session_id": "session-one"}, headers={"X-AgentGov-Session-Id": "session-one"})
        if path == "/api/runtime/chat/":
            body = json.loads(request.content)
            assert body["agent_id"] == "runtime-published-one"
            assert body["session_id"] == "session-one"
            return httpx.Response(200, json={}, headers={"X-AgentGov-Run-Id": "run-one", "X-AgentGov-Session-Id": "session-one"})
        if path == "/api/runtime/sessions/session-one/stream":
            assert request.url.params["agent_id"] == "runtime-published-one"
            return httpx.Response(200, content=b'data: {"type":"REPLY_END"}\n\n', headers={"Content-Type": "text/event-stream"})
        if path == "/api/runtime/sessions/session-one/messages":
            assert request.url.params["agent_id"] == "runtime-published-one"
            return httpx.Response(200, json={"messages": self.messages})
        if path == "/api/agent-runs/run-one":
            return httpx.Response(200, json=self.run)
        if path == "/api/agent-runs/run-one/trace":
            status = self.trace_states.pop(0) if len(self.trace_states) > 1 else self.trace_states[0]
            return httpx.Response(200, json={"run_id": "run-one", "trace_id": "a" * 32, "trace_status": status})
        if path == "/api/feedback-signals":
            return httpx.Response(200, json=self.signal)
        if path == "/api/feedback-signals/signal-one":
            return httpx.Response(200, json=self.persisted_signal)
        if path == "/api/runtime/sessions/session-one" and request.method == "DELETE":
            assert request.url.params["agent_id"] == "runtime-published-one"
            return httpx.Response(200, json={})
        raise AssertionError(f"Unexpected public request: {request.method} {path}")


@pytest.fixture
def public_api(monkeypatch):
    live = _load_module()
    api = PublicApiFixture()
    client_factory = httpx.AsyncClient
    transport = httpx.MockTransport(api)
    monkeypatch.setattr(live.httpx, "AsyncClient", lambda **kwargs: client_factory(transport=transport, **kwargs))
    monkeypatch.setenv("AGENT_GOV_ACCEPTANCE_RUN_ID", "acceptance-test")
    return api


def _execute_public_acceptance(*, fixture_agent=False):
    live = _load_module()
    args = argparse.Namespace(runs=1, concurrency=1, timeout_seconds=0.05, require_trace_complete=True, fixture_agent=fixture_agent)
    env = {"API_BASE": "http://127.0.0.1:50400", "API_KEY": "private-api-key"}
    return asyncio.run(live.run_live_acceptance(args, env, live.DEFAULT_SCENARIOS))


def test_live_script_provisions_exact_binding_and_verifies_public_evidence(public_api) -> None:
    public_api.trace_states = ["pending", "complete"]
    evidence = _execute_public_acceptance()

    assert len(evidence) == 1
    assert evidence[0].binding.governance_agent_id == "security-operations-expert"
    assert evidence[0].binding.runtime_agent_id == "runtime-published-one"
    assert evidence[0].reply_ids == ("reply-one",)
    assert evidence[0].trace_status == "complete"
    assert evidence[0].feedback_signal_id == "signal-one"
    assert public_api.requests[-1].method == "DELETE"
    assert sum(request.url.path.endswith("/provision") for request in public_api.requests) == 1
    assert any(request.url.path == "/api/feedback-signals/signal-one" for request in public_api.requests)


@pytest.mark.parametrize(
    ("field", "value"),
    [("governance_agent_id", "other-agent"), ("runtime_agent_id", ""), ("agent_version_id", None), ("provisioned", False)],
)
def test_live_script_rejects_unconfirmed_provision_binding(public_api, field, value) -> None:
    public_api.binding[field] = value
    with pytest.raises(_load_module().LiveAcceptanceError, match="provision"):
        _execute_public_acceptance()
    assert not any(request.url.path == "/api/runtime/sessions/" for request in public_api.requests)


@pytest.mark.parametrize("field", ["run_id", "session_id", "agent_id", "runtime_agent_id", "agent_version_id", "harness_digest"])
def test_live_script_rejects_terminal_run_binding_mismatch(public_api, field) -> None:
    public_api.run[field] = "other-binding"
    with pytest.raises(_load_module().LiveAcceptanceError, match="发布绑定"):
        _execute_public_acceptance()
    assert not any(request.url.path == "/api/feedback-signals" for request in public_api.requests)


@pytest.mark.parametrize("status", ["failed", "cancelled", "interrupted"])
def test_live_script_rejects_non_success_terminal(public_api, status) -> None:
    public_api.run["status"] = status
    with pytest.raises(_load_module().LiveAcceptanceError, match="未成功"):
        _execute_public_acceptance()


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [{"id": "reply-one", "role": "user", "content": []}],
        [{"id": "unrelated-reply", "role": "assistant", "finished_reason": "completed", "content": [{"type": "text", "text": "历史结果"}]}],
        [{"id": "reply-one", "role": "assistant", "finished_reason": "interrupted", "content": []}],
        [{"id": "reply-one", "role": "assistant", "finished_reason": "completed", "content": [{"type": "text", "text": " "}]}],
    ],
)
def test_live_script_rejects_empty_or_unrelated_canonical_output(public_api, messages) -> None:
    public_api.messages = messages
    with pytest.raises(_load_module().LiveAcceptanceError, match="canonical"):
        _execute_public_acceptance()


def test_live_script_requires_durable_reply_confirmation(public_api) -> None:
    public_api.run["persisted_reply_ids"] = ["unrelated-reply"]
    with pytest.raises(_load_module().LiveAcceptanceError, match="持久化确认"):
        _execute_public_acceptance()


def test_live_script_fails_when_trace_does_not_become_complete(public_api) -> None:
    public_api.trace_states = ["pending"]
    with pytest.raises(_load_module().LiveAcceptanceError, match="时限内达到 complete"):
        _execute_public_acceptance()


@pytest.mark.parametrize("record", ["signal", "persisted_signal"])
@pytest.mark.parametrize("field", ["source_type", "run_id", "matched_run_id", "session_id", "agent_id"])
def test_live_script_rejects_feedback_source_mismatch(public_api, record, field) -> None:
    getattr(public_api, record)[field] = "other-source"
    with pytest.raises(_load_module().LiveAcceptanceError, match="反馈"):
        _execute_public_acceptance()


def test_fixture_agent_option_is_explicit_without_environment_default(monkeypatch) -> None:
    live = _load_module()
    monkeypatch.setenv("LIVE_ACCEPTANCE_FIXTURE_AGENT", "1")
    assert live.parse_args(["--env-file", "unused"]).fixture_agent is False
    assert live.parse_args(["--env-file", "unused", "--fixture-agent"]).fixture_agent is True


def _install_fixture_selection(monkeypatch, public_api, *, version="version-one"):
    live = _load_module()
    agent_id = "runtime-acceptance-new-agent"
    public_api.governance_agent_id = agent_id
    public_api.binding["governance_agent_id"] = agent_id
    public_api.run["agent_id"] = agent_id
    public_api.signal["agent_id"] = agent_id
    public_api.persisted_signal["agent_id"] = agent_id
    events = []

    @asynccontextmanager
    async def temporary_agent(client):
        events.append("create")
        try:
            yield SimpleNamespace(agent=SimpleNamespace(agent_id=agent_id), current_commit_sha=version)
        finally:
            events.append("cleanup")

    monkeypatch.setattr(live, "temporary_runtime_agent", temporary_agent)
    return events


def test_fixture_selection_reuses_run_canonical_trace_and_feedback_checks(public_api, monkeypatch) -> None:
    events = _install_fixture_selection(monkeypatch, public_api)
    evidence = _execute_public_acceptance(fixture_agent=True)
    assert events == ["create", "cleanup"]
    assert evidence[0].binding.governance_agent_id == "runtime-acceptance-new-agent"
    assert evidence[0].trace_status == "complete"
    assert evidence[0].feedback_signal_id == "signal-one"
    assert any(request.url.path.endswith("/messages") for request in public_api.requests)
    assert any(request.url.path.endswith("/trace") for request in public_api.requests)


def test_fixture_selection_preserves_existing_failure_and_cleans_up(public_api, monkeypatch) -> None:
    events = _install_fixture_selection(monkeypatch, public_api)
    public_api.messages = []
    with pytest.raises(_load_module().LiveAcceptanceError, match="canonical"):
        _execute_public_acceptance(fixture_agent=True)
    assert events == ["create", "cleanup"]


def test_fixture_selection_checks_import_commit_against_provision(public_api, monkeypatch) -> None:
    events = _install_fixture_selection(monkeypatch, public_api, version="other-commit")
    with pytest.raises(_load_module().LiveAcceptanceError, match="导入 Git"):
        _execute_public_acceptance(fixture_agent=True)
    assert events == ["create", "cleanup"]
    assert not any(request.url.path == "/api/runtime/sessions/" for request in public_api.requests)


def test_default_agent_failure_does_not_activate_fixture(public_api, monkeypatch) -> None:
    def forbidden_fixture(_client):
        pytest.fail("Fixture must never be an automatic fallback")

    live = _load_module()
    monkeypatch.setattr(live, "temporary_runtime_agent", forbidden_fixture)
    public_api.binding["provisioned"] = False
    with pytest.raises(live.LiveAcceptanceError, match="provision"):
        _execute_public_acceptance()


def test_fixture_summary_names_only_generic_scope(public_api, monkeypatch, capsys) -> None:
    live = _load_module()
    _install_fixture_selection(monkeypatch, public_api)
    monkeypatch.setattr(live, "_read_env_file", lambda _path: {"API_BASE": "http://127.0.0.1:50400", "API_KEY": "private-api-key"})
    monkeypatch.setattr(live, "_require_explicit_live_authorization", lambda *_args, **_kwargs: None)
    assert live.main(["--env-file", "unused", "--fixture-agent", "--require-trace-complete"]) == 0
    output = capsys.readouterr().out
    summary = json.loads(output)
    assert summary["acceptance_scope"] == "generic-runtime"
    assert {"mcp", "subagents", "hitl", "session-resume", "runtime-restart-recovery", "model-effect-improvement"}.issubset(summary["excluded_claims"])
    assert "private-api-key" not in output
    assert "已收到。" not in output


def test_parallel_failure_waits_for_sibling_cleanup_before_return(monkeypatch) -> None:
    live = _load_module()
    sibling_started = asyncio.Event()
    sibling_cleaned = []

    async def scenario(_client, item, **_kwargs):
        if item.scenario_id == "first":
            await sibling_started.wait()
            raise live.LiveAcceptanceError("first failed")
        try:
            sibling_started.set()
            await asyncio.Event().wait()
        finally:
            sibling_cleaned.append(True)

    monkeypatch.setattr(live, "run_scenario", scenario)
    args = argparse.Namespace(concurrency=2, timeout_seconds=1, require_trace_complete=True)
    selected = (live.Scenario("first", "one", "feedback"), live.Scenario("second", "two", "feedback"))
    with pytest.raises(live.LiveAcceptanceError, match="first failed"):
        asyncio.run(live._run_selected_scenarios(None, args, selected, None))
    assert sibling_cleaned == [True]


def test_live_cli_help_loads_fixture_module_without_pythonpath() -> None:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--help"],
        cwd="/tmp",
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "--fixture-agent" in result.stdout
