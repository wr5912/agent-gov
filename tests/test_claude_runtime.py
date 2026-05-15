import asyncio
import base64

from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.schemas import ChatRequest
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import AppSettings


class FakeLangfuseObservation:
    def __init__(self, kwargs):
        self.kwargs = kwargs
        self.updates = []
        self.trace_io_updates = []
        self.exited = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.exited = True
        return False

    def update(self, **kwargs):
        self.updates.append(kwargs)

    def set_trace_io(self, **kwargs):
        self.trace_io_updates.append(kwargs)


class FakeLangfuseClient:
    def __init__(self):
        self.observations = []
        self.flushed = False

    def start_as_current_observation(self, **kwargs):
        observation = FakeLangfuseObservation(kwargs)
        self.observations.append(observation)
        return observation

    def flush(self):
        self.flushed = True


def _settings(tmp_path):
    workspace = tmp_path / "docker" / "volume" / "workspace"
    data = tmp_path / "docker" / "volume" / "data"
    claude_root = tmp_path / "docker" / "volume" / "claude-root"
    claude_home = claude_root / ".claude"
    workspace.mkdir(parents=True, exist_ok=True)
    claude_home.mkdir(parents=True, exist_ok=True)
    return AppSettings(
        _env_file=None,
        WORKSPACE_DIR=workspace,
        DATA_DIR=data,
        CLAUDE_ROOT=claude_root,
        CLAUDE_HOME=claude_home,
        ENABLE_POLICY_HOOKS=True,
    )


async def _collect_prompt(prompt):
    items = []
    async for item in prompt:
        items.append(item)
    return items


def test_run_uses_streaming_prompt_for_policy_hooks(tmp_path, monkeypatch):
    seen = {}

    async def fake_query(*, prompt, options, transport=None):
        seen["prompt_is_string"] = isinstance(prompt, str)
        seen["prompt_items"] = await _collect_prompt(prompt)
        if False:
            yield None

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    settings = _settings(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))

    result = asyncio.run(runtime.run(ChatRequest(message="hello")))

    assert result["errors"] == []
    assert seen["prompt_is_string"] is False
    assert seen["prompt_items"] == [
        {
            "type": "user",
            "message": {"role": "user", "content": "hello"},
            "parent_tool_use_id": None,
            "session_id": "default",
        }
    ]


def test_default_options_use_native_claude_code_config(tmp_path, monkeypatch):
    seen = {}

    async def fake_query(*, prompt, options, transport=None):
        seen["options"] = options
        async for _ in prompt:
            pass
        if False:
            yield None

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    settings = _settings(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/tmp/should-not-leak")
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))

    result = asyncio.run(runtime.run(ChatRequest(message="hello")))
    options = seen["options"]

    assert result["errors"] == []
    assert options.setting_sources == ["user", "project", "local"]
    assert options.settings is None
    assert options.mcp_servers == {}
    assert options.agents is None
    assert "CLAUDE_CONFIG_DIR" not in options.env
    assert "CLAUDE_CODE_ENABLE_TELEMETRY" not in options.env


def test_explicit_config_overrides_are_passed_to_sdk(tmp_path, monkeypatch):
    seen = {}

    async def fake_query(*, prompt, options, transport=None):
        seen["options"] = options
        async for _ in prompt:
            pass
        if False:
            yield None

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    settings_path = tmp_path / "settings.override.json"
    mcp_path = tmp_path / "mcp.override.json"
    config_dir = tmp_path / "claude-config"
    settings_path.write_text("{}", encoding="utf-8")
    mcp_path.write_text('{"mcpServers": {}}', encoding="utf-8")

    base = _settings(tmp_path)
    settings = AppSettings(
        _env_file=None,
        WORKSPACE_DIR=base.workspace_dir,
        DATA_DIR=base.data_dir,
        CLAUDE_ROOT=base.claude_root,
        CLAUDE_HOME=base.claude_home,
        CLAUDE_SETTINGS_PATH=settings_path,
        CLAUDE_MCP_CONFIG_PATH=mcp_path,
        CLAUDE_CONFIG_DIR=config_dir,
        ENABLE_POLICY_HOOKS=True,
    )
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))
    monkeypatch.setattr(runtime, "_get_langfuse_client", lambda: None)

    result = asyncio.run(runtime.run(ChatRequest(message="hello")))
    options = seen["options"]

    assert result["errors"] == []
    assert options.settings == str(settings_path)
    assert options.mcp_servers == str(mcp_path)
    assert options.env["CLAUDE_CONFIG_DIR"] == str(config_dir)


def test_langfuse_env_is_passed_to_claude_sdk(tmp_path, monkeypatch):
    seen = {}

    async def fake_query(*, prompt, options, transport=None):
        seen["options"] = options
        async for _ in prompt:
            pass
        if False:
            yield None

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    base = _settings(tmp_path)
    settings = AppSettings(
        _env_file=None,
        WORKSPACE_DIR=base.workspace_dir,
        DATA_DIR=base.data_dir,
        CLAUDE_ROOT=base.claude_root,
        CLAUDE_HOME=base.claude_home,
        ENABLE_POLICY_HOOKS=True,
        LANGFUSE_ENABLED=True,
        LANGFUSE_PUBLIC_KEY="pk-test",
        LANGFUSE_SECRET_KEY="sk-test",
        LANGFUSE_BASE_URL="https://us.cloud.langfuse.com",
        LANGFUSE_OTEL_SIGNALS="traces,metrics,logs",
        LANGFUSE_SERVICE_NAME="runtime-test",
        LANGFUSE_DEPLOYMENT_ENVIRONMENT="test",
        LANGFUSE_RESOURCE_ATTRIBUTES="service.version=0.1.0",
        LANGFUSE_EXPORT_INTERVAL_MS=500,
    )
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))
    monkeypatch.setattr(runtime, "_get_langfuse_client", lambda: None)

    result = asyncio.run(runtime.run(ChatRequest(message="hello")))
    env = seen["options"].env
    expected_auth = base64.b64encode(b"pk-test:sk-test").decode()

    assert result["errors"] == []
    assert env["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
    assert env["CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"] == "1"
    assert env["OTEL_TRACES_EXPORTER"] == "otlp"
    assert env["OTEL_METRICS_EXPORTER"] == "otlp"
    assert env["OTEL_LOGS_EXPORTER"] == "otlp"
    assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/protobuf"
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://us.cloud.langfuse.com/api/public/otel"
    assert env["OTEL_EXPORTER_OTLP_HEADERS"] == (
        f"Authorization=Basic {expected_auth},x-langfuse-ingestion-version=4"
    )
    assert env["OTEL_SERVICE_NAME"] == "runtime-test"
    assert env["OTEL_RESOURCE_ATTRIBUTES"] == "deployment.environment=test,service.version=0.1.0"
    assert env["OTEL_TRACES_EXPORT_INTERVAL"] == "500"
    assert env["OTEL_LOG_USER_PROMPTS"] == "1"
    assert env["OTEL_LOG_TOOL_DETAILS"] == "1"
    assert env["OTEL_LOG_TOOL_CONTENT"] == "1"
    assert env["OTEL_LOG_RAW_API_BODIES"] == "1"


def test_langfuse_requires_keys_when_enabled(tmp_path):
    base = _settings(tmp_path)
    settings = AppSettings(
        _env_file=None,
        WORKSPACE_DIR=base.workspace_dir,
        DATA_DIR=base.data_dir,
        CLAUDE_ROOT=base.claude_root,
        CLAUDE_HOME=base.claude_home,
        LANGFUSE_ENABLED=True,
        LANGFUSE_PUBLIC_KEY="pk-test",
    )
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))

    result = asyncio.run(runtime.run(ChatRequest(message="hello")))

    assert result["answer"] == ""
    assert result["errors"] == [
        "ValueError: LANGFUSE_ENABLED=true requires LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY"
    ]


def test_claude_env_json_overrides_langfuse_defaults(tmp_path, monkeypatch):
    seen = {}

    async def fake_query(*, prompt, options, transport=None):
        seen["options"] = options
        async for _ in prompt:
            pass
        if False:
            yield None

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    base = _settings(tmp_path)
    settings = AppSettings(
        _env_file=None,
        WORKSPACE_DIR=base.workspace_dir,
        DATA_DIR=base.data_dir,
        CLAUDE_ROOT=base.claude_root,
        CLAUDE_HOME=base.claude_home,
        ENABLE_POLICY_HOOKS=True,
        LANGFUSE_ENABLED=True,
        LANGFUSE_PUBLIC_KEY="pk-test",
        LANGFUSE_SECRET_KEY="sk-test",
        CLAUDE_ENV_JSON='{"OTEL_EXPORTER_OTLP_ENDPOINT":"http://collector:4318","OTEL_LOG_USER_PROMPTS":"1"}',
    )
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))

    result = asyncio.run(runtime.run(ChatRequest(message="hello")))
    env = seen["options"].env

    assert result["errors"] == []
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://collector:4318"
    assert env["OTEL_LOG_USER_PROMPTS"] == "1"
    assert env["OTEL_LOG_TOOL_DETAILS"] == "1"
    assert env["OTEL_LOG_TOOL_CONTENT"] == "1"
    assert env["OTEL_LOG_RAW_API_BODIES"] == "1"


def test_health_reports_langfuse_state_without_secrets(tmp_path, monkeypatch):
    from app.runtime.settings import get_settings

    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "docker" / "volume" / "workspace"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "docker" / "volume" / "data"))
    monkeypatch.setenv("CLAUDE_ROOT", str(tmp_path / "docker" / "volume" / "claude-root"))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "docker" / "volume" / "claude-root" / ".claude"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    get_settings.cache_clear()

    from app import main

    base = _settings(tmp_path)
    settings = AppSettings(
        _env_file=None,
        WORKSPACE_DIR=base.workspace_dir,
        DATA_DIR=base.data_dir,
        CLAUDE_ROOT=base.claude_root,
        CLAUDE_HOME=base.claude_home,
        LANGFUSE_ENABLED=True,
        LANGFUSE_PUBLIC_KEY="pk-test",
        LANGFUSE_SECRET_KEY="sk-test",
        LANGFUSE_OTEL_ENDPOINT="http://langfuse.local/api/public/otel",
        LANGFUSE_OTEL_SIGNALS="traces,logs",
    )
    monkeypatch.setattr(main, "settings", settings)

    result = asyncio.run(main.health())

    assert result["langfuse_enabled"] is True
    assert result["langfuse_otel_endpoint_configured"] is True
    assert result["langfuse_public_key_configured"] is True
    assert result["langfuse_secret_key_configured"] is True
    assert result["langfuse_otel_signals"] == ["traces", "logs"]
    serialized = str(result)
    assert "pk-test" not in serialized
    assert "sk-test" not in serialized
    assert "OTEL_EXPORTER_OTLP_HEADERS" not in serialized


def test_run_normalizes_result_error_and_dedupes_answer(tmp_path, monkeypatch):
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    async def fake_query(*, prompt, options, transport=None):
        async for _ in prompt:
            pass
        yield AssistantMessage(
            content=[TextBlock(text="bad model")],
            model="<synthetic>",
            error="invalid_request",
            session_id="sdk-session",
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=True,
            num_turns=1,
            session_id="sdk-session",
            result="bad model",
            api_error_status=404,
        )
        raise Exception("Claude Code returned an error result: success")

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    settings = _settings(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))

    result = asyncio.run(runtime.run(ChatRequest(message="hello")))

    assert result["answer"] == "bad model"
    assert result["errors"] == ["Claude Code API error (404): bad model"]


def test_run_enriches_langfuse_input_output(tmp_path, monkeypatch):
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    async def fake_query(*, prompt, options, transport=None):
        async for _ in prompt:
            pass
        yield {
            "type": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu-skill",
                    "name": "Skill",
                    "input": {"skill": "trace-debugger", "prompt": "inspect trace"},
                },
                {
                    "type": "tool_use",
                    "id": "toolu-read",
                    "name": "Read",
                    "input": {"file_path": "README.md"},
                },
                {
                    "id": "call-mcp-assets",
                    "name": "mcp__sec-ops-data__local_api__list_assets_api_v1_assets_get",
                    "input": {"count": 1},
                },
            ],
        }
        yield {
            "type": "user",
            "content": [
                {
                    "tool_use_id": "toolu-read",
                    "content": "README contents",
                }
            ],
        }
        yield {
            "hook_event_name": "PostToolUse",
            "hook_name": "PostToolUse:mcp__sec-ops-data__local_api__list_assets_api_v1_assets_get",
        }
        yield AssistantMessage(
            content=[TextBlock(text="hello answer")],
            model="<synthetic>",
            session_id="sdk-session",
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="sdk-session",
            result="hello answer",
            usage={"input_tokens": 3, "output_tokens": 5},
            total_cost_usd=0.01,
            stop_reason="end_turn",
        )

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    base = _settings(tmp_path)
    settings = AppSettings(
        _env_file=None,
        WORKSPACE_DIR=base.workspace_dir,
        DATA_DIR=base.data_dir,
        CLAUDE_ROOT=base.claude_root,
        CLAUDE_HOME=base.claude_home,
        LANGFUSE_ENABLED=True,
        LANGFUSE_PUBLIC_KEY="pk-test",
        LANGFUSE_SECRET_KEY="sk-test",
        ENABLE_POLICY_HOOKS=True,
    )
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))
    fake_langfuse = FakeLangfuseClient()
    monkeypatch.setattr(runtime, "_get_langfuse_client", lambda: fake_langfuse)

    result = asyncio.run(runtime.run(ChatRequest(message="hello", metadata={"api_key": "secret"})))

    assert result["answer"] == "hello answer"
    assert result["agent_activity"]["requested_skills"] == []
    assert result["agent_activity"]["tool_names"] == [
        "Skill",
        "Read",
        "mcp__sec-ops-data__local_api__list_assets_api_v1_assets_get",
    ]
    assert result["agent_activity"]["skill_calls"][0]["name"] == "trace-debugger"
    assert result["agent_activity"]["tool_results"][0]["tool_use_id"] == "toolu-read"
    assert result["agent_activity"]["tool_results"][1]["name"] == (
        "mcp__sec-ops-data__local_api__list_assets_api_v1_assets_get"
    )
    assert fake_langfuse.flushed is True
    assert [obs.kwargs["name"] for obs in fake_langfuse.observations] == [
        "runtime.chat",
        "runtime.claude_sdk_query",
    ]
    root, generation = fake_langfuse.observations
    assert root.kwargs["input"]["message"] == "hello"
    assert root.kwargs["input"]["metadata"]["api_key"] == "secret"
    assert root.updates[-1]["output"]["answer"] == "hello answer"
    assert root.updates[-1]["output"]["messages"][3]["event"] == "AssistantMessage"
    assert root.updates[-1]["output"]["agent_activity"]["tool_names"] == [
        "Skill",
        "Read",
        "mcp__sec-ops-data__local_api__list_assets_api_v1_assets_get",
    ]
    assert root.trace_io_updates[-1]["input"]["metadata"]["api_key"] == "secret"
    assert root.trace_io_updates[-1]["output"]["answer"] == "hello answer"
    assert root.trace_io_updates[-1]["output"]["agent_activity"]["skill_calls"][0]["name"] == "trace-debugger"
    assert generation.updates[-1]["usage_details"] == {"input_tokens": 3, "output_tokens": 5}
    assert generation.updates[-1]["cost_details"] == {"total_cost_usd": 0.01}


def test_stream_enriches_langfuse_input_output(tmp_path, monkeypatch):
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    async def fake_query(*, prompt, options, transport=None):
        async for _ in prompt:
            pass
        yield {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "README.md"},
            "tool_use_id": "toolu-read",
        }
        yield AssistantMessage(
            content=[TextBlock(text="stream answer")],
            model="<synthetic>",
            session_id="sdk-session",
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="sdk-session",
            result="stream answer",
            usage={"input_tokens": 7, "output_tokens": 11},
            stop_reason="end_turn",
        )

    async def collect(runtime):
        return [item async for item in runtime.stream(ChatRequest(message="stream"))]

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    base = _settings(tmp_path)
    settings = AppSettings(
        _env_file=None,
        WORKSPACE_DIR=base.workspace_dir,
        DATA_DIR=base.data_dir,
        CLAUDE_ROOT=base.claude_root,
        CLAUDE_HOME=base.claude_home,
        LANGFUSE_ENABLED=True,
        LANGFUSE_PUBLIC_KEY="pk-test",
        LANGFUSE_SECRET_KEY="sk-test",
        ENABLE_POLICY_HOOKS=True,
    )
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))
    fake_langfuse = FakeLangfuseClient()
    monkeypatch.setattr(runtime, "_get_langfuse_client", lambda: fake_langfuse)

    events = asyncio.run(collect(runtime))

    assert [event["event"] for event in events] == ["session", "message", "message", "message", "result", "done"]
    result_event = next(event for event in events if event["event"] == "result")
    assert result_event["data"]["agent_activity"]["tool_names"] == ["Read"]
    assert fake_langfuse.flushed is True
    root, generation = fake_langfuse.observations
    assert root.updates[-1]["output"]["answer"] == "stream answer"
    assert root.updates[-1]["output"]["stop_reason"] == "end_turn"
    assert root.trace_io_updates[-1]["input"]["message"] == "stream"
    assert root.trace_io_updates[-1]["output"]["answer"] == "stream answer"
    assert generation.updates[-1]["usage_details"] == {"input_tokens": 7, "output_tokens": 11}


def test_stream_ag_ui_maps_text_lifecycle(tmp_path, monkeypatch):
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    async def fake_query(*, prompt, options, transport=None):
        async for _ in prompt:
            pass
        yield AssistantMessage(
            content=[TextBlock(text="hello from ag-ui")],
            model="<synthetic>",
            session_id="sdk-session",
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="sdk-session",
            result="hello from ag-ui",
            usage={"input_tokens": 1, "output_tokens": 2},
            stop_reason="end_turn",
        )

    async def collect(runtime):
        from app.runtime.ag_ui import RunAgentInput

        req = RunAgentInput(
            threadId="thread-test",
            runId="run-test",
            messages=[{"role": "user", "content": "hello"}],
        )
        return [item async for item in runtime.stream_ag_ui(req)]

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    settings = _settings(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))

    events = asyncio.run(collect(runtime))

    assert [event["type"] for event in events] == [
        "RUN_STARTED",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]
    assert events[0]["threadId"] == "thread-test"
    assert events[0]["runId"] == "run-test"
    assert events[2]["messageId"] == "run-test-assistant-1"
    assert events[2]["delta"] == "hello from ag-ui"
    assert events[-1]["outcome"] == {"type": "success"}


def test_stream_ag_ui_extracts_a2ui_custom_event(tmp_path, monkeypatch):
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    a2ui_block = """Here is a generated card.
<a2ui-json>
[
  {"beginRendering": {"surfaceId": "surface-alert", "root": "card-root"}},
  {
    "surfaceUpdate": {
      "surfaceId": "surface-alert",
      "components": [
        {"id": "card-root", "component": {"Card": {"child": "card-text"}}},
        {"id": "card-text", "component": {"Text": {"text": {"literalString": "High risk alert"}}}}
      ]
    }
  }
]
</a2ui-json>
The card is ready."""

    async def fake_query(*, prompt, options, transport=None):
        async for _ in prompt:
            pass
        yield AssistantMessage(
            content=[TextBlock(text=a2ui_block)],
            model="<synthetic>",
            session_id="sdk-session",
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="sdk-session",
            result="card",
            usage={"input_tokens": 1, "output_tokens": 2},
            stop_reason="end_turn",
        )

    async def collect(runtime):
        from app.runtime.ag_ui import RunAgentInput

        req = RunAgentInput(
            threadId="thread-test",
            runId="run-test",
            messages=[{"role": "user", "content": "show card"}],
        )
        return [item async for item in runtime.stream_ag_ui(req)]

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    settings = _settings(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))

    events = asyncio.run(collect(runtime))
    custom_events = [event for event in events if event["type"] == "CUSTOM"]
    text_events = [event for event in events if event["type"] == "TEXT_MESSAGE_CONTENT"]

    assert len(custom_events) == 1
    assert custom_events[0]["name"] == "a2ui.message"
    assert custom_events[0]["value"]["version"] == "v0_8"
    assert len(custom_events[0]["value"]["messages"]) == 2
    assert "a2ui-json" not in text_events[0]["delta"]
    assert text_events[0]["delta"] == "Here is a generated card.\n\nThe card is ready."


def test_stream_ag_ui_suppresses_internal_skill_payload(tmp_path, monkeypatch):
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    internal_skill_payload = """Base directory for this skill: /root/.claude/skills/ai-soc-a2ui-response

# AI-SOC A2UI Runtime Response

Do not show this internal skill file to the user.

ARGUMENTS: analyze alert"""

    async def fake_query(*, prompt, options, transport=None):
        async for _ in prompt:
            pass
        yield AssistantMessage(
            content=[TextBlock(text="I will analyze the alert.")],
            model="<synthetic>",
            session_id="sdk-session",
        )
        yield AssistantMessage(
            content=[TextBlock(text=internal_skill_payload)],
            model="<synthetic>",
            session_id="sdk-session",
        )
        yield AssistantMessage(
            content=[TextBlock(text="Final triage summary.")],
            model="<synthetic>",
            session_id="sdk-session",
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="sdk-session",
            result="triage",
            usage={"input_tokens": 1, "output_tokens": 2},
            stop_reason="end_turn",
        )

    async def collect(runtime):
        from app.runtime.ag_ui import RunAgentInput

        req = RunAgentInput(
            threadId="thread-test",
            runId="run-test",
            messages=[{"role": "user", "content": "analyze alert"}],
        )
        return [item async for item in runtime.stream_ag_ui(req)]

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    settings = _settings(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))

    events = asyncio.run(collect(runtime))
    text_events = [event for event in events if event["type"] == "TEXT_MESSAGE_CONTENT"]

    assert [event["delta"] for event in text_events] == [
        "I will analyze the alert.",
        "Final triage summary.",
    ]
    assert not any("Base directory for this skill" in event["delta"] for event in text_events)


def test_stream_ag_ui_rejects_invalid_a2ui_payload(tmp_path, monkeypatch):
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    async def fake_query(*, prompt, options, transport=None):
        async for _ in prompt:
            pass
        yield AssistantMessage(
            content=[TextBlock(text='<a2ui-json>{"surfaceUpdate": {"surfaceId": "x", "components": []}}</a2ui-json>')],
            model="<synthetic>",
            session_id="sdk-session",
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="sdk-session",
            result="invalid",
            usage={"input_tokens": 1, "output_tokens": 2},
            stop_reason="end_turn",
        )

    async def collect(runtime):
        from app.runtime.ag_ui import RunAgentInput

        req = RunAgentInput(
            threadId="thread-test",
            runId="run-test",
            messages=[{"role": "user", "content": "show card"}],
        )
        return [item async for item in runtime.stream_ag_ui(req)]

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    settings = _settings(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))

    events = asyncio.run(collect(runtime))

    assert not [event for event in events if event["type"] == "CUSTOM"]
    error_events = [event for event in events if event["type"] == "RUN_ERROR"]
    assert len(error_events) == 1
    assert error_events[0]["code"] == "a2ui-invalid"


def test_notification_store_lists_after_cursor():
    from app.runtime.notification_store import InMemoryNotificationStore

    store = InMemoryNotificationStore()
    first = store.publish(
        name="ai_soc.briefing.available",
        value={"briefingId": "briefing-1"},
        notification_id="notification-1",
        workspace_id="workspace-a",
    )
    second = store.publish(
        name="ai_soc.briefing.available",
        value={"briefingId": "briefing-2"},
        notification_id="notification-2",
        workspace_id="workspace-a",
    )
    store.publish(
        name="ai_soc.briefing.available",
        value={"briefingId": "briefing-3"},
        notification_id="notification-3",
        workspace_id="workspace-b",
    )

    assert store.list_after(workspace_id="workspace-a") == [first, second]
    assert store.list_after("notification-1", workspace_id="workspace-a") == [second]
