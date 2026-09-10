import time

from scripts import langfuse_smoke
from scripts.langfuse_smoke import runtime_trace_observation_errors


def test_queue_check_requires_private_auth_without_guessing_a_default(monkeypatch) -> None:
    monkeypatch.setattr(langfuse_smoke, "container_running", lambda _: True)

    def unexpected_query(*_args):
        raise AssertionError("缺少私有密码时不得连接 Redis")

    monkeypatch.setattr(langfuse_smoke, "redis_queue_count", unexpected_query)
    assert langfuse_smoke.check_queues({}) == ["Langfuse Redis queue check requires private LANGFUSE_REDIS_AUTH"]


def test_queue_check_uses_selected_private_auth_without_printing_it(monkeypatch, capsys) -> None:
    monkeypatch.setattr(langfuse_smoke, "container_running", lambda _: True)
    observed = []

    def query(_container, auth, _queue, _state):
        observed.append(auth)
        return 0

    monkeypatch.setattr(langfuse_smoke, "redis_queue_count", query)
    synthetic_auth = "test-only-private-redis-auth"
    assert langfuse_smoke.check_queues({"LANGFUSE_REDIS_AUTH": synthetic_auth}) == []
    assert observed and set(observed) == {synthetic_auth}
    assert synthetic_auth not in capsys.readouterr().out


def test_smoke_resolves_published_port_defaults_and_explicit_urls(monkeypatch) -> None:
    assert langfuse_smoke.resolve_langfuse_url({}) == "http://localhost:50402"
    assert langfuse_smoke.resolve_langfuse_url({"LANGFUSE_HOST_PORT": "50499"}) == "http://localhost:50499"
    assert langfuse_smoke.resolve_langfuse_url(
        {"LANGFUSE_NEXTAUTH_URL": "https://trace.example.test", "LANGFUSE_HOST_PORT": "50499"},
    ) == "https://trace.example.test"
    requested: list[str] = []

    def get_json(url: str):
        requested.append(url)
        return {}

    monkeypatch.setattr(langfuse_smoke, "get_json", get_json)
    langfuse_smoke.print_runtime_versions({})

    assert requested == ["http://localhost:50400/health"]


def test_agentscope_trace_accepts_agent_model_and_tool_semantics() -> None:
    trace_name = "agentgov.run"
    errors = runtime_trace_observation_errors(
        trace_id="0123456789abcdef0123456789abcdef",
        trace_name=trace_name,
        names={trace_name, "agentgov.run.stage", "invoke_agent", "chat", "execute_tool"},
        root_attribute_keys=set(langfuse_smoke.REQUIRED_ROOT_ATTRIBUTES),
        expected_reply_ids={"reply-1"},
        stage_reply_ids=["reply-1"],
    )

    assert errors == []


def test_agentscope_trace_rejects_non_run_root_and_missing_semantic_spans() -> None:
    errors = runtime_trace_observation_errors(
        trace_id="0123456789abcdef0123456789abcdef",
        trace_name="chat",
        names={"chat"},
        root_attribute_keys=set(langfuse_smoke.REQUIRED_ROOT_ATTRIBUTES),
        expected_reply_ids={"reply-1"},
        stage_reply_ids=["reply-1"],
    )

    assert any("root is not the AgentGov run span" in error for error in errors)
    assert any("does not include the redacted AgentScope agent observation" in error for error in errors)


def test_agentscope_trace_rejects_error_observations() -> None:
    trace_name = "agentgov.run"
    errors = runtime_trace_observation_errors(
        trace_id="0123456789abcdef0123456789abcdef",
        trace_name=trace_name,
        names={trace_name, "agentgov.run.stage", "invoke_agent", "chat", "execute_tool"},
        root_attribute_keys=set(langfuse_smoke.REQUIRED_ROOT_ATTRIBUTES),
        expected_reply_ids={"reply-1"},
        stage_reply_ids=["reply-1"],
        error_observation_names={"execute_tool"},
    )

    assert errors == [
        "trace 0123456789abcdef0123456789abcdef includes error observations: execute_tool",
    ]


def test_agentscope_trace_requires_correlation_attributes_on_run_observation() -> None:
    errors = runtime_trace_observation_errors(
        trace_id="0123456789abcdef0123456789abcdef",
        trace_name="agentgov.run",
        names={"agentgov.run", "agentgov.run.stage", "invoke_agent", "chat"},
        root_attribute_keys={"agentgov.run.id"},
        expected_reply_ids={"reply-1"},
        stage_reply_ids=["reply-1"],
    )

    assert len(errors) == 1
    assert "agentgov.agent.version_id" in errors[0]
    assert "agentgov.run.finished_reason" in errors[0]


def test_agentscope_trace_matches_stage_replies_to_terminal_run_exactly() -> None:
    assert "agentscope.agent.reply_id" not in langfuse_smoke.REQUIRED_ROOT_ATTRIBUTES
    errors = runtime_trace_observation_errors(
        trace_id="0123456789abcdef0123456789abcdef",
        trace_name="agentgov.run",
        names={"agentgov.run", "agentgov.run.stage", "invoke_agent", "chat"},
        root_attribute_keys=set(langfuse_smoke.REQUIRED_ROOT_ATTRIBUTES),
        expected_reply_ids={"reply-1"},
        stage_reply_ids=["reply-1", "reply-unpersisted"],
    )

    assert errors == [
        "trace 0123456789abcdef0123456789abcdef run stages do not exactly match persisted reply_ids",
    ]


def test_semantic_trace_poll_is_bound_to_the_triggered_agentgov_run(monkeypatch) -> None:
    trace_id = "0123456789abcdef0123456789abcdef"
    responses = iter(
        [
            ({"run_id": "run-1", "trace_id": trace_id, "trace_status": "pending", "trace": None}, {}),
            (
                {
                    "run_id": "run-1",
                    "trace_id": trace_id,
                    "trace_status": "complete",
                    "trace": {
                        "name": "agentgov.run",
                        "observations": [
                            {
                                "name": "agentgov.run",
                                "metadata": {key: "present" for key in langfuse_smoke.REQUIRED_ROOT_ATTRIBUTES},
                            },
                            {
                                "name": "agentgov.run.stage",
                                "metadata": {"agentscope.agent.reply_id": "reply-1"},
                            },
                            {"name": "invoke_agent"},
                            {"name": "chat"},
                        ],
                    },
                },
                {},
            ),
        ]
    )
    requested: list[str] = []

    def request(url: str, **_kwargs):
        requested.append(url)
        return next(responses)

    monkeypatch.setattr(langfuse_smoke, "request_agentgov_json", request)
    monkeypatch.setattr(langfuse_smoke.time, "sleep", lambda _seconds: None)

    errors = langfuse_smoke.wait_for_semantic_trace(
        api_base="http://agent-gov.test",
        api_key="secret",
        run_id="run-1",
        expected_reply_ids=frozenset({"reply-1"}),
        deadline=time.monotonic() + 10,
    )

    assert errors == []
    assert requested == [
        "http://agent-gov.test/api/agent-runs/run-1/trace",
        "http://agent-gov.test/api/agent-runs/run-1/trace",
    ]


def test_runtime_smoke_provisions_and_uses_explicit_runtime_identity(monkeypatch) -> None:
    trace_id = "0123456789abcdef0123456789abcdef"
    responses = iter(
        [
            ({"runtime_agent_id": "runtime-1"}, {}),
            ({"session_id": "session-1"}, {}),
            ({"status": "started", "session_id": "session-1"}, {"X-AgentGov-Run-Id": "run-1"}),
            ({"run_id": "run-1", "status": "succeeded", "reply_ids": ["reply-1"]}, {}),
            (
                {
                    "run_id": "run-1",
                    "trace_id": trace_id,
                    "trace_status": "complete",
                    "trace": {
                        "name": "agentgov.run",
                        "observations": [
                            {
                                "name": "agentgov.run",
                                "metadata": {key: "present" for key in langfuse_smoke.REQUIRED_ROOT_ATTRIBUTES},
                            },
                            {
                                "name": "agentgov.run.stage",
                                "metadata": {"agentscope.agent.reply_id": "reply-1"},
                            },
                            {"name": "invoke_agent"},
                            {"name": "chat"},
                        ],
                    },
                },
                {},
            ),
            ({"run_id": "run-1", "status": "succeeded"}, {}),
            ({}, {}),
        ],
    )
    requests: list[tuple[str, dict[str, object]]] = []

    def request(url: str, **kwargs):
        requests.append((url, kwargs))
        return next(responses)

    monkeypatch.setattr(langfuse_smoke, "request_agentgov_json", request)

    errors = langfuse_smoke.trigger_and_check_runtime_trace(
        {"API_BASE": "http://agent-gov.test", "LANGFUSE_SMOKE_AGENT_ID": "business/agent"},
        timeout_seconds=10,
    )

    assert errors == []
    assert requests[0][0] == "http://agent-gov.test/api/runtime/agents/business%2Fagent/provision"
    assert requests[1][1]["payload"]["agent_id"] == "runtime-1"
    assert str(requests[1][1]["extra_headers"]["Idempotency-Key"]).startswith("langfuse-smoke-session-")
    chat_payload = requests[2][1]["payload"]
    assert chat_payload["agent_id"] == "runtime-1"
    assert str(chat_payload["client_operation_id"]).startswith("langfuse-smoke-turn-")
    assert requests[-1][0].endswith("/api/runtime/sessions/session-1?agent_id=runtime-1")
