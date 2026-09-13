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
    assert "agentId: options.runtimeAgentId" in run
    assert "options.runtimeAgentId," in run
    assert "business_agent_id" in sessions
    assert "runtimeAgentId" in sessions


def test_runtime_activation_is_only_a_release_side_effect() -> None:
    app = _read("frontend/src/App.tsx")
    chat = _read("frontend/src/components/ChatPanel.tsx")
    api = _read("frontend/src/api/runtime.ts")

    assert "provisionRuntimeAgent" not in app
    assert 'data-testid="runtime-provision"' not in chat
    assert "runtimeProvisioning" not in chat
    assert "/provision" not in api
    assert "尚未发布激活" in chat


def test_pending_runtime_session_is_not_fabricated_from_local_messages() -> None:
    sessions = _read("frontend/src/hooks/usePlaygroundSessionScope.ts")

    assert "localOnly" not in sessions
    assert "本地新会话" not in sessions
    assert "messages.find" not in sessions


def test_hitl_continuation_sends_the_exact_expected_run_id() -> None:
    run = _read("frontend/src/hooks/usePlaygroundRun.ts")
    continuation = run + _read("frontend/src/playgroundRunHelpers.ts")
    api = _read("frontend/src/api/runtime.ts")

    assert "expectedRunId: turn.runtimeRunId" in continuation
    assert "expected_run_id: context.expectedRunId" in api
    assert "拒绝提交可能过期的确认结果" in continuation


def test_detached_run_discards_half_initialized_stream_on_identity_or_completion_failure() -> None:
    detached = _read("frontend/src/playgroundDetachedRun.ts")
    connection_bound = detached.index("turn.connection = connection;")
    failure_cleanup = detached.index("} catch (error) {", connection_bound)

    assert detached.index("controller.bindRunHandle(turn, runId);", connection_bound) < failure_cleanup
    assert detached.index("const replyMonitor = connection.armReply()", connection_bound) < failure_cleanup
    assert detached.index('throw new Error("Runtime status 返回了不同的 session_id。");') < failure_cleanup
    assert detached.index("connection.close();", failure_cleanup) > failure_cleanup
    assert "if (turn.connection === connection) turn.connection = undefined;" in detached[failure_cleanup:]
