import asyncio
import base64
import json

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


def test_stream_ag_ui_maps_partial_stream_events_to_text_deltas(tmp_path, monkeypatch):
    from claude_agent_sdk import AssistantMessage, ResultMessage, StreamEvent, TextBlock

    async def fake_query(*, prompt, options, transport=None):
        async for _ in prompt:
            pass
        assert options.include_partial_messages is True
        yield StreamEvent(
            uuid="event-1",
            session_id="sdk-session",
            event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hel"}},
        )
        yield StreamEvent(
            uuid="event-2",
            session_id="sdk-session",
            event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "lo"}},
        )
        yield AssistantMessage(
            content=[TextBlock(text="hello")],
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
            result="hello",
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
    settings.include_partial_messages = True
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))

    events = asyncio.run(collect(runtime))
    text_events = [event for event in events if event["type"] == "TEXT_MESSAGE_CONTENT"]

    assert [event["delta"] for event in text_events] == ["hel", "lo"]


def test_stream_ag_ui_maps_emit_a2ui_tool_call_to_custom_event(tmp_path, monkeypatch):
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    async def fake_query(*, prompt, options, transport=None):
        async for _ in prompt:
            pass
        yield AssistantMessage(
            content=[TextBlock(text="已生成结构化资产概览。")],
            model="<synthetic>",
            session_id="sdk-session",
        )
        yield {
            "type": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu-a2ui",
                    "name": "mcp__ai-soc-ui__emit_a2ui",
                    "input": {
                        "messages": [
                            {"beginRendering": {"surfaceId": "surface-assets", "root": "asset-card"}},
                            {
                                "surfaceUpdate": {
                                    "surfaceId": "surface-assets",
                                    "components": [
                                        {"id": "asset-card", "component": {"Card": {"child": "asset-text"}}},
                                        {
                                            "id": "asset-text",
                                            "component": {
                                                "Text": {"text": {"literal": "当前资产总数 20"}}
                                            },
                                        },
                                    ],
                                }
                            },
                        ]
                    },
                }
            ],
        }
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
            messages=[{"role": "user", "content": "查看资产"}],
        )
        return [item async for item in runtime.stream_ag_ui(req)]

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    settings = _settings(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))

    events = asyncio.run(collect(runtime))
    custom_events = [
        event for event in events if event["type"] == "CUSTOM" and event["name"] == "a2ui.message"
    ]
    text_events = [event for event in events if event["type"] == "TEXT_MESSAGE_CONTENT"]

    assert [event["delta"] for event in text_events] == ["已生成结构化资产概览。"]
    assert len(custom_events) == 1
    assert custom_events[0]["name"] == "a2ui.message"
    assert custom_events[0]["value"]["version"] == "v0_8"
    assert custom_events[0]["value"]["messages"][0]["beginRendering"]["surfaceId"] == "surface-assets"


def test_stream_ag_ui_maps_render_a2ui_raw_mode_to_custom_event(tmp_path, monkeypatch):
    from claude_agent_sdk import ResultMessage

    async def fake_query(*, prompt, options, transport=None):
        async for _ in prompt:
            pass
        yield {
            "type": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu-render-a2ui",
                    "name": "mcp__ai-soc-ui__render_a2ui",
                    "input": {
                        "payload": {
                            "mode": "a2ui",
                            "messages": [
                                {"beginRendering": {"surfaceId": "render-surface", "root": "render-root"}},
                                {
                                    "surfaceUpdate": {
                                        "surfaceId": "render-surface",
                                        "components": [
                                            {
                                                "id": "render-root",
                                                "component": {
                                                    "Text": {"text": {"literal": "原生 A2UI 渲染"}}
                                                },
                                            }
                                        ],
                                    }
                                },
                            ],
                        }
                    },
                }
            ],
        }
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="sdk-session",
            result="rendered",
            usage={"input_tokens": 1, "output_tokens": 2},
            stop_reason="end_turn",
        )

    async def collect(runtime):
        from app.runtime.ag_ui import RunAgentInput

        req = RunAgentInput(
            threadId="thread-test",
            runId="run-render",
            messages=[{"role": "user", "content": "渲染结构化视图"}],
        )
        return [item async for item in runtime.stream_ag_ui(req)]

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    settings = _settings(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))

    events = asyncio.run(collect(runtime))
    custom_events = [
        event for event in events if event["type"] == "CUSTOM" and event["name"] == "a2ui.message"
    ]
    activity_events = [
        event for event in events if event["type"] == "CUSTOM" and event["name"] == "ai_soc.agent.activity"
    ]

    assert len(custom_events) == 1
    assert custom_events[0]["value"]["messages"][0]["beginRendering"]["surfaceId"] == "render-surface"
    assert activity_events[0]["value"]["label"] == "渲染结构化视图"


def test_stream_ag_ui_maps_render_a2ui_card_mode_to_a2ui_messages(tmp_path, monkeypatch):
    from claude_agent_sdk import ResultMessage

    async def fake_query(*, prompt, options, transport=None):
        async for _ in prompt:
            pass
        yield {
            "type": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu-render-card",
                    "name": "mcp__ai-soc-ui__render_a2ui",
                    "input": {
                        "payload": {
                            "mode": "card",
                            "surfaceId": "render-card-surface",
                            "cards": [
                                {
                                    "title": "风险资产摘要",
                                    "subtitle": "通过 render_a2ui card mode 生成",
                                    "sections": [
                                        {
                                            "title": "高危资产",
                                            "type": "table",
                                            "columns": ["资产", "评分"],
                                            "rows": [["vpn-05", "95"]],
                                        }
                                    ],
                                    "actions": [
                                        {
                                            "label": "查看 vpn-05",
                                            "name": "ai_soc.asset.select",
                                            "primary": True,
                                            "context": {"assetId": "vpn-05"},
                                        }
                                    ],
                                }
                            ],
                        }
                    },
                }
            ],
        }
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="sdk-session",
            result="rendered",
            usage={"input_tokens": 1, "output_tokens": 2},
            stop_reason="end_turn",
        )

    async def collect(runtime):
        from app.runtime.ag_ui import RunAgentInput

        req = RunAgentInput(
            threadId="thread-test",
            runId="run-render-card",
            messages=[{"role": "user", "content": "以卡片展示风险资产"}],
        )
        return [item async for item in runtime.stream_ag_ui(req)]

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    settings = _settings(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))

    events = asyncio.run(collect(runtime))
    custom_events = [
        event for event in events if event["type"] == "CUSTOM" and event["name"] == "a2ui.message"
    ]

    assert len(custom_events) == 1
    messages = custom_events[0]["value"]["messages"]
    assert messages[0]["beginRendering"]["surfaceId"] == "render-card-surface"
    components = messages[1]["surfaceUpdate"]["components"]
    assert any(component["id"] == "render-card-surface-card-1-title" for component in components)
    assert any("Button" in component["component"] for component in components)


def test_stream_ag_ui_maps_render_a2ui_catalog_mode_to_a2ui_messages(tmp_path, monkeypatch):
    from claude_agent_sdk import ResultMessage

    async def fake_query(*, prompt, options, transport=None):
        async for _ in prompt:
            pass
        yield {
            "type": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu-render-catalog",
                    "name": "mcp__ai-soc-ui__render_a2ui",
                    "input": {
                        "payload": {
                            "mode": "catalog",
                            "catalog": "ai-soc",
                            "surfaceId": "render-catalog-surface",
                            "components": [
                                {
                                    "type": "RiskMetricGroup",
                                    "props": {
                                        "title": "风险指标",
                                        "metrics": [
                                            {"label": "高危资产", "value": "3"},
                                            {"label": "待处理告警", "value": "7"},
                                        ],
                                    },
                                },
                                {
                                    "type": "RiskAssetTable",
                                    "props": {
                                        "title": "风险资产",
                                        "assets": [
                                            {
                                                "assetId": "vpn-05",
                                                "hostname": "vpn-05",
                                                "ip": "10.1.2.5",
                                                "riskScore": 95,
                                                "asset_type": "gateway",
                                            }
                                        ],
                                    },
                                },
                                {
                                    "type": "AlertTriageCard",
                                    "props": {
                                        "title": "异常登录研判",
                                        "severity": "High",
                                        "confidence": "82%",
                                        "summary": "检测到异常登录后出现横向移动迹象。",
                                        "evidence": ["异地登录", "短时间内访问多台主机"],
                                        "recommendations": ["确认账号归属", "临时收敛高危权限"],
                                    },
                                },
                            ],
                        }
                    },
                }
            ],
        }
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="sdk-session",
            result="rendered",
            usage={"input_tokens": 1, "output_tokens": 2},
            stop_reason="end_turn",
        )

    async def collect(runtime):
        from app.runtime.ag_ui import RunAgentInput

        req = RunAgentInput(
            threadId="thread-test",
            runId="run-render-catalog",
            messages=[{"role": "user", "content": "展示 SOC catalog 组件"}],
        )
        return [item async for item in runtime.stream_ag_ui(req)]

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    settings = _settings(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))

    events = asyncio.run(collect(runtime))
    custom_events = [
        event for event in events if event["type"] == "CUSTOM" and event["name"] == "a2ui.message"
    ]

    assert len(custom_events) == 1
    messages = custom_events[0]["value"]["messages"]
    assert messages[0]["beginRendering"]["surfaceId"] == "render-catalog-surface"
    components = messages[1]["surfaceUpdate"]["components"]
    assert any(component["id"] == "render-catalog-surface-card-1-title" for component in components)
    assert any(component["id"] == "render-catalog-surface-card-2-actions" for component in components)
    assert any(component["id"] == "render-catalog-surface-card-3-section-2-item-1" for component in components)


def test_stream_ag_ui_converts_emit_a2ui_card_specs_to_a2ui_messages(tmp_path, monkeypatch):
    from claude_agent_sdk import ResultMessage

    async def fake_query(*, prompt, options, transport=None):
        async for _ in prompt:
            pass
        yield {
            "type": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu-a2ui-card",
                    "name": "mcp__ai-soc-ui__emit_a2ui",
                    "input": {
                        "surfaceId": "asset-cards",
                        "messages": [
                            {
                                "type": "card",
                                "title": "资产查询结果概览",
                                "subtitle": "共 20 条资产 | 高危 7",
                                "sections": [
                                    {
                                        "title": "风险分布",
                                        "type": "metric_group",
                                        "items": [
                                            {"label": "高危", "value": "7"},
                                            {"label": "低危", "value": "7"},
                                        ],
                                    },
                                    {
                                        "title": "高危资产",
                                        "type": "table",
                                        "columns": ["主机名", "风险评分"],
                                        "rows": [["edr-gateway-20", "97"]],
                                    },
                                ],
                                "actions": [
                                    {
                                        "label": "查看 vpn-05",
                                        "name": "ai_soc.asset.select",
                                        "primary": True,
                                        "context": {
                                            "assetId": "vpn-05",
                                            "assetName": "vpn-05",
                                            "riskScore": 95,
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                }
            ],
        }
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
            messages=[{"role": "user", "content": "以卡片形式展示资产"}],
        )
        return [item async for item in runtime.stream_ag_ui(req)]

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    settings = _settings(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))

    events = asyncio.run(collect(runtime))
    custom_events = [
        event for event in events if event["type"] == "CUSTOM" and event["name"] == "a2ui.message"
    ]
    error_events = [event for event in events if event["type"] == "RUN_ERROR"]

    assert error_events == []
    assert len(custom_events) == 1
    messages = custom_events[0]["value"]["messages"]
    assert messages[0]["beginRendering"]["surfaceId"] == "asset-cards"
    assert messages[1]["surfaceUpdate"]["surfaceId"] == "asset-cards"
    assert any(component["id"] == "asset-cards-card-1-title" for component in messages[1]["surfaceUpdate"]["components"])


def test_stream_ag_ui_converts_emit_cards_tool_call_to_a2ui_messages(tmp_path, monkeypatch):
    from claude_agent_sdk import ResultMessage

    async def fake_query(*, prompt, options, transport=None):
        async for _ in prompt:
            pass
        yield {
            "type": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu-cards",
                    "name": "mcp__ai-soc-ui__emit_cards",
                    "input": {
                        "surfaceId": "asset-risk-cards",
                        "cards": [
                            {
                                "title": "资产风险概览",
                                "subtitle": "共 20 台资产，高风险 5 台",
                                "sections": [
                                    {
                                        "title": "风险分布",
                                        "type": "metric_group",
                                        "items": [
                                            {"label": "高风险", "value": "5"},
                                            {"label": "中风险", "value": "8"},
                                        ],
                                    },
                                    {
                                        "title": "建议动作",
                                        "type": "action_list",
                                        "items": ["优先排查 vpn-05", "确认 DMZ 暴露面"],
                                    },
                                ],
                                "actions": [
                                    {
                                        "label": "查看 vpn-05",
                                        "name": "ai_soc.asset.select",
                                        "primary": True,
                                        "context": {
                                            "assetId": "vpn-05",
                                            "assetName": "vpn-05",
                                            "riskScore": 95,
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                }
            ],
        }
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
            messages=[{"role": "user", "content": "查看资产风险"}],
        )
        return [item async for item in runtime.stream_ag_ui(req)]

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    settings = _settings(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))

    events = asyncio.run(collect(runtime))
    custom_events = [
        event for event in events if event["type"] == "CUSTOM" and event["name"] == "a2ui.message"
    ]
    error_events = [event for event in events if event["type"] == "RUN_ERROR"]

    assert error_events == []
    assert len(custom_events) == 1
    messages = custom_events[0]["value"]["messages"]
    assert messages[0]["beginRendering"]["surfaceId"] == "asset-risk-cards"
    assert any(
        component["id"] == "asset-risk-cards-card-1-section-2-item-1"
        for component in messages[1]["surfaceUpdate"]["components"]
    )
    button_components = [
        component["component"]["Button"]
        for component in messages[1]["surfaceUpdate"]["components"]
        if "Button" in component["component"]
    ]
    assert button_components[0]["primary"] is True
    assert button_components[0]["action"]["name"] == "ai_soc.asset.select"
    assert {"key": "assetId", "value": {"literalString": "vpn-05"}} in button_components[0]["action"]["context"]


def test_stream_ag_ui_emits_agent_activity_for_tool_calls(tmp_path, monkeypatch):
    from claude_agent_sdk import ResultMessage

    async def fake_query(*, prompt, options, transport=None):
        async for _ in prompt:
            pass
        yield {
            "type": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu-assets",
                    "name": "mcp__sec-ops-data__local_api__list_assets_api_v1_assets_get",
                    "input": {"limit": 20},
                }
            ],
        }
        yield {
            "type": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu-assets",
                    "content": [{"asset": "vpn-05"}],
                }
            ],
        }
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="sdk-session",
            result="assets",
            usage={"input_tokens": 1, "output_tokens": 2},
            stop_reason="end_turn",
        )

    async def collect(runtime):
        from app.runtime.ag_ui import RunAgentInput

        req = RunAgentInput(
            threadId="thread-test",
            runId="run-test",
            messages=[{"role": "user", "content": "查看资产"}],
        )
        return [item async for item in runtime.stream_ag_ui(req)]

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    settings = _settings(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))

    events = asyncio.run(collect(runtime))
    activity_events = [
        event
        for event in events
        if event["type"] == "CUSTOM" and event["name"] == "ai_soc.agent.activity"
    ]

    assert len(activity_events) == 2
    assert activity_events[0]["value"]["status"] == "running"
    assert activity_events[0]["value"]["label"] == "查询资产列表"
    assert activity_events[1]["value"]["status"] == "finished"
    assert activity_events[1]["value"]["label"] == "查询资产列表"


def test_stream_ag_ui_emits_progressive_a2ui_for_asset_results(tmp_path, monkeypatch):
    from claude_agent_sdk import ResultMessage

    async def fake_query(*, prompt, options, transport=None):
        async for _ in prompt:
            pass
        yield {
            "type": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu-assets",
                    "name": "mcp__sec-ops-data__local_api__list_assets_api_v1_assets_get",
                    "input": {"limit": 20},
                }
            ],
        }
        yield {
            "type": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu-assets",
                    "content": {
                        "count": 2,
                        "items": [
                            {
                                "assetId": "vpn-05",
                                "assetName": "vpn-05",
                                "riskScore": 95,
                                "zone": "办公网",
                            },
                            {
                                "assetId": "db-core-01",
                                "assetName": "db-core-01",
                                "riskScore": 62,
                                "zone": "生产网",
                            },
                        ],
                    },
                }
            ],
        }
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="sdk-session",
            result="assets",
            usage={"input_tokens": 1, "output_tokens": 2},
            stop_reason="end_turn",
        )

    async def collect(runtime):
        from app.runtime.ag_ui import RunAgentInput

        req = RunAgentInput(
            threadId="thread-test",
            runId="run-progressive",
            messages=[{"role": "user", "content": "查看当前资产风险"}],
        )
        return [item async for item in runtime.stream_ag_ui(req)]

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    settings = _settings(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))

    events = asyncio.run(collect(runtime))
    custom_events = [
        event for event in events if event["type"] == "CUSTOM" and event["name"] == "a2ui.message"
    ]

    assert len(custom_events) >= 4
    surface_ids = {
        message.get("beginRendering", {}).get("surfaceId")
        or message.get("surfaceUpdate", {}).get("surfaceId")
        for event in custom_events
        for message in event["value"]["messages"]
    }
    assert surface_ids == {"ai-soc-asset-risk-run-progressive"}
    assert custom_events[0]["value"]["messages"][0]["beginRendering"]["root"] == "ai-soc-asset-risk-run-progressive-root"

    final_components = custom_events[-1]["value"]["messages"][0]["surfaceUpdate"]["components"]
    button_components = [
        component["component"]["Button"]
        for component in final_components
        if "Button" in component["component"]
    ]
    text_values = [
        component["component"]["Text"]["text"]["literal"]
        for component in final_components
        if "Text" in component["component"]
    ]
    assert "资产风险视图已生成" in text_values
    assert button_components[0]["action"]["name"] == "ai_soc.asset.select"
    assert {"key": "assetId", "value": {"literalString": "vpn-05"}} in button_components[0]["action"]["context"]


def test_stream_ag_ui_parses_text_tool_result_and_retargets_final_cards(tmp_path, monkeypatch):
    from claude_agent_sdk import ResultMessage

    assets = [
        {
            "asset_id": "asset-0005",
            "hostname": "edr-gateway-05",
            "ip": "10.211.93.73",
            "risk_score": 100,
            "business_unit": "办公网",
            "asset_type": "kubernetes",
        },
        {
            "asset_id": "asset-0010",
            "hostname": "db-core-10",
            "ip": "10.148.54.10",
            "risk_score": 90,
            "business_unit": "生产网",
            "asset_type": "firewall",
        },
    ]

    async def fake_query(*, prompt, options, transport=None):
        async for _ in prompt:
            pass
        yield {
            "type": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu-assets",
                    "name": "mcp__sec-ops-data__local_api__list_assets_api_v1_assets_get",
                    "input": {"count": 50},
                }
            ],
        }
        yield {
            "type": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu-assets",
                    "content": [{"type": "text", "text": json.dumps(assets)}],
                }
            ],
        }
        yield {
            "type": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu-cards",
                    "name": "mcp__ai-soc-ui__emit_cards",
                    "input": {
                        "surfaceId": "asset-risk-overview",
                        "cards": [
                            {
                                "title": "最终风险资产卡片",
                                "subtitle": "已合并到 progressive surface",
                                "sections": [
                                    {
                                        "title": "TOP 资产",
                                        "type": "table",
                                        "rows": [["edr-gateway-05", "100"]],
                                    }
                                ],
                            }
                        ],
                    },
                }
            ],
        }
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id="sdk-session",
            result="assets",
            usage={"input_tokens": 1, "output_tokens": 2},
            stop_reason="end_turn",
        )

    async def collect(runtime):
        from app.runtime.ag_ui import RunAgentInput

        req = RunAgentInput(
            threadId="thread-test",
            runId="run-retarget",
            messages=[{"role": "user", "content": "查看当前风险资产"}],
        )
        return [item async for item in runtime.stream_ag_ui(req)]

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    settings = _settings(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))

    events = asyncio.run(collect(runtime))
    custom_events = [
        event for event in events if event["type"] == "CUSTOM" and event["name"] == "a2ui.message"
    ]
    surface_ids = {
        message.get("beginRendering", {}).get("surfaceId")
        or message.get("surfaceUpdate", {}).get("surfaceId")
        for event in custom_events
        for message in event["value"]["messages"]
    }

    assert surface_ids == {"ai-soc-asset-risk-run-retarget"}
    assert all("ai-soc-generated-cards" not in json.dumps(event, ensure_ascii=False) for event in custom_events)
    assert all("asset-risk-overview" not in json.dumps(event, ensure_ascii=False) for event in custom_events)

    rendered_text = "\n".join(json.dumps(event, ensure_ascii=False) for event in custom_events)
    assert "edr-gateway-05" in rendered_text
    assert "已获取资产数据，正在生成风险视图" in rendered_text
    assert "最终风险资产卡片" in rendered_text


def test_stream_ag_ui_accepts_emit_a2ui_messages_json_string(tmp_path, monkeypatch):
    from claude_agent_sdk import ResultMessage

    async def fake_query(*, prompt, options, transport=None):
        async for _ in prompt:
            pass
        yield {
            "type": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu-a2ui-string",
                    "name": "mcp__ai-soc-ui__emit_a2ui",
                    "input": {
                        "messages": (
                            '[{"beginRendering":{"surfaceId":"surface-string","root":"card-root"}},'
                            '{"surfaceUpdate":{"surfaceId":"surface-string","components":['
                            '{"id":"card-root","component":{"Card":{"child":"card-text"}}},'
                            '{"id":"card-text","component":{"Text":{"text":{"literal":"字符串入参"}}}}'
                            ']}}]'
                        )
                    },
                }
            ],
        }
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
            messages=[{"role": "user", "content": "查询资产"}],
        )
        return [item async for item in runtime.stream_ag_ui(req)]

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    settings = _settings(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))

    events = asyncio.run(collect(runtime))
    custom_events = [
        event for event in events if event["type"] == "CUSTOM" and event["name"] == "a2ui.message"
    ]
    error_events = [event for event in events if event["type"] == "RUN_ERROR"]

    assert error_events == []
    assert len(custom_events) == 1
    assert custom_events[0]["value"]["messages"][0]["beginRendering"]["surfaceId"] == "surface-string"


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


def test_stream_ag_ui_suppresses_partial_internal_skill_payload(tmp_path, monkeypatch):
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    internal_skill_payload = """Base directory for this skill: /root/.claude/skills/ai-soc-a2ui-response

# AI-SOC A2UI Runtime Response

This partial skill payload has not streamed its ARGUMENTS section yet."""

    async def fake_query(*, prompt, options, transport=None):
        async for _ in prompt:
            pass
        yield AssistantMessage(
            content=[TextBlock(text=internal_skill_payload)],
            model="<synthetic>",
            session_id="sdk-session",
        )
        yield AssistantMessage(
            content=[TextBlock(text="Final answer.")],
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

    assert [event["delta"] for event in text_events] == ["Final answer."]


def test_stream_ag_ui_skips_invalid_emit_a2ui_tool_payload_without_failing_run(tmp_path, monkeypatch):
    from claude_agent_sdk import ResultMessage

    async def fake_query(*, prompt, options, transport=None):
        async for _ in prompt:
            pass
        yield {
            "type": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu-a2ui-invalid",
                    "name": "mcp__ai-soc-ui__emit_a2ui",
                    "input": {
                        "messages": [
                            {"surfaceUpdate": {"surfaceId": "x", "components": []}},
                        ]
                    },
                }
            ],
        }
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

    assert not [
        event for event in events if event["type"] == "CUSTOM" and event["name"] == "a2ui.message"
    ]
    diagnostic_events = [
        event for event in events if event["type"] == "CUSTOM" and event["name"] == "a2ui.diagnostic"
    ]
    assert [event["value"]["code"] for event in diagnostic_events] == [
        "a2ui-payload-invalid",
        "a2ui-retry-started",
        "a2ui-payload-invalid",
        "a2ui-retry-failed",
    ]
    assert diagnostic_events[0]["value"]["retryEligible"] is True
    assert diagnostic_events[2]["value"]["retryEligible"] is False
    assert diagnostic_events[2]["value"]["retryAttempt"] == 1
    assert not [event for event in events if event["type"] == "RUN_ERROR"]
    assert events[-1]["type"] == "RUN_FINISHED"


def test_stream_ag_ui_retries_invalid_a2ui_payload_once(tmp_path, monkeypatch):
    from claude_agent_sdk import ResultMessage

    calls = {"count": 0, "retry_prompt": ""}

    async def fake_query(*, prompt, options, transport=None):
        calls["count"] += 1
        prompt_items = await _collect_prompt(prompt)
        if calls["count"] == 2:
            calls["retry_prompt"] = prompt_items[0]["message"]["content"]

        if calls["count"] == 1:
            yield {
                "type": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu-a2ui-invalid",
                        "name": "mcp__ai-soc-ui__emit_a2ui",
                        "input": {
                            "messages": [
                                {"surfaceUpdate": {"surfaceId": "x", "components": []}},
                            ]
                        },
                    }
                ],
            }
        else:
            yield {
                "type": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu-a2ui-retry",
                        "name": "mcp__ai-soc-ui__render_a2ui",
                        "input": {
                            "mode": "card",
                            "payload": {
                                "surfaceId": "retry-surface",
                                "cards": [
                                    {
                                        "type": "card",
                                        "title": "修正后的 UI",
                                        "sections": [{"items": ["已生成有效 A2UI payload"]}],
                                    }
                                ],
                            },
                        },
                    }
                ],
            }
            yield {
                "type": "assistant",
                "content": [{"type": "text", "text": "这段补偿文字不应进入 AG-UI 文本消息。"}],
            }

        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id=f"sdk-session-{calls['count']}",
            result="retry",
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

    assert calls["count"] == 2
    assert "只修正用户界面 payload" in calls["retry_prompt"]
    assert "show card" in calls["retry_prompt"]
    assert not [
        event
        for event in events
        if event["type"] == "TEXT_MESSAGE_CONTENT" and "补偿文字" in event["delta"]
    ]

    custom_events = [
        event for event in events if event["type"] == "CUSTOM" and event["name"] == "a2ui.message"
    ]
    assert len(custom_events) == 1
    assert custom_events[0]["value"]["messages"][0]["beginRendering"]["surfaceId"] == "retry-surface"

    diagnostic_events = [
        event for event in events if event["type"] == "CUSTOM" and event["name"] == "a2ui.diagnostic"
    ]
    assert [event["value"]["code"] for event in diagnostic_events] == [
        "a2ui-payload-invalid",
        "a2ui-retry-started",
        "a2ui-retry-succeeded",
    ]
    assert not [event for event in events if event["type"] == "RUN_ERROR"]
    assert events[-1]["type"] == "RUN_FINISHED"


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
