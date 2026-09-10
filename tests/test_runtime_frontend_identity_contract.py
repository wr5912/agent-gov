from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_playground_separates_governance_and_agentscope_runtime_ids() -> None:
    app = _read("frontend/src/App.tsx")
    run = _read("frontend/src/hooks/usePlaygroundRun.ts")
    sessions = _read("frontend/src/hooks/usePlaygroundSessionScope.ts")

    assert "getSessions(effectiveClientConfig, agent.agent_id)" in app
    assert "activeBackendSession?.agent_id || selectedBusinessAgent?.runtime_agent_id" in app
    assert "runtimeAgentId: activeRuntimeAgentId" in app
    assert "const agentId = options.runtimeAgentId" in run
    assert "agentId: options.runtimeAgentId" in run
    assert "options.runtimeAgentId," in run
    assert "business_agent_id" in sessions
    assert "runtimeAgentId" in sessions


def test_unprovisioned_agent_requires_an_explicit_runtime_mutation() -> None:
    app = _read("frontend/src/App.tsx")
    chat = _read("frontend/src/components/ChatPanel.tsx")
    api = _read("frontend/src/api/runtime.ts")

    assert "provisionRuntimeAgent(effectiveClientConfig, selectedBusinessAgentId)" in app
    assert 'data-testid="runtime-provision"' in chat
    assert "disabled={!runtimeReady || runtimeProvisioning}" in chat
    assert "/api/runtime/agents/${encodeURIComponent(governanceAgentId)}/provision" in api


def test_hitl_continuation_sends_the_exact_expected_run_id() -> None:
    run = _read("frontend/src/hooks/usePlaygroundRun.ts")
    continuation = run + _read("frontend/src/playgroundRunHelpers.ts")
    api = _read("frontend/src/api/runtime.ts")

    assert "expectedRunId: turn.runtimeRunId" in continuation
    assert "expected_run_id: context.expectedRunId" in api
    assert "拒绝提交可能过期的确认结果" in continuation
