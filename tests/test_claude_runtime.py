import asyncio
import base64
import json
import os
import sys
import types
from contextlib import nullcontext

from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.integrations.runtime_langfuse import RuntimeLangfuseClient
from app.runtime.policy import build_profile_pre_tool_use_hook
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
    workspace = tmp_path / "docker" / "volume" / "main-workspace"
    data = tmp_path / "docker" / "volume" / "data"
    claude_root = tmp_path / "docker" / "volume" / "claude-roots" / "main"
    claude_home = claude_root / ".claude"
    workspace.mkdir(parents=True, exist_ok=True)
    claude_home.mkdir(parents=True, exist_ok=True)
    return AppSettings(
        _env_file=None,
        WORKSPACE_DIR=workspace,
        MAIN_WORKSPACE_DIR=workspace,
        DATA_DIR=data,
        CLAUDE_ROOT=claude_root,
        MAIN_CLAUDE_ROOT=claude_root,
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

    assert result.errors == []
    assert seen["prompt_is_string"] is False
    assert seen["prompt_items"] == [
        {
            "type": "user",
            "message": {"role": "user", "content": "hello"},
            "parent_tool_use_id": None,
            "session_id": "default",
        }
    ]


def test_default_options_use_main_runtime_profile(tmp_path, monkeypatch):
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

    assert result.errors == []
    assert options.setting_sources == ["user", "project", "local"]
    assert options.settings is None
    assert options.mcp_servers == {}
    assert options.agents is None
    assert "Skill" in options.allowed_tools
    assert options.cwd == settings.main_workspace_dir
    assert options.env["HOME"] == str(settings.main_claude_root)
    assert options.env["CLAUDE_CONFIG_DIR"] == str(settings.main_claude_root / ".claude")
    assert options.env["DATA_DIR"] == str(settings.data_dir)
    assert options.env["CLAUDE_HOOK_AUDIT_LOG"] == str(settings.data_dir / "transcripts" / "claude-hook-audit.jsonl")
    assert options.env["AGENT_PROFILE"] == "main-agent"
    assert "CLAUDE_CODE_ENABLE_TELEMETRY" not in options.env


def test_main_runtime_profile_filters_mcp_servers(tmp_path, monkeypatch):
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
    (settings.main_workspace_dir / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "sec-ops-data": {"url": "http://127.0.0.1:1/mcp", "transport": "http"},
                    "security-kb": {"url": "http://127.0.0.1:2/mcp", "transport": "http"},
                    "forbidden": {"url": "http://127.0.0.1:3/mcp", "transport": "http"},
                }
            }
        ),
        encoding="utf-8",
    )
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))

    result = asyncio.run(runtime.run(ChatRequest(message="hello")))

    assert result.errors == []
    assert set(seen["options"].mcp_servers) == {"sec-ops-data", "security-kb"}


def test_feedback_attribution_job_options_use_profile_minimum_max_turns(tmp_path):
    settings = _settings(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))

    options = runtime.job_runner.build_options(runtime.profiles["attribution-analyzer"])

    assert options.max_turns == 16


def test_feedback_attribution_job_options_allow_global_max_turn_override(tmp_path):
    settings = _settings(tmp_path)
    settings.max_turns = 20
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))

    options = runtime.job_runner.build_options(runtime.profiles["attribution-analyzer"])

    assert options.max_turns == 20


def test_feedback_job_profile_filters_mcp_servers(tmp_path):
    settings = _settings(tmp_path)
    settings.attribution_analyzer_workspace_dir.mkdir(parents=True, exist_ok=True)
    (settings.attribution_analyzer_workspace_dir / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "feedback-evidence": {"url": "http://127.0.0.1:1/mcp", "transport": "http"},
                    "readonly-trace": {"url": "http://127.0.0.1:2/mcp", "transport": "http"},
                    "main-only": {"url": "http://127.0.0.1:3/mcp", "transport": "http"},
                }
            }
        ),
        encoding="utf-8",
    )
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))

    options = runtime.job_runner.build_options(runtime.profiles["attribution-analyzer"])

    assert set(options.mcp_servers) == {"feedback-evidence", "readonly-trace"}


def test_profile_path_hook_blocks_denied_read_path(tmp_path):
    settings = _settings(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))
    profile = runtime.profiles["attribution-analyzer"]
    hook = build_profile_pre_tool_use_hook(profile)

    result = asyncio.run(
        hook(
            {
                "tool_name": "Read",
                "tool_input": {"file_path": str(settings.main_workspace_dir / "CLAUDE.md")},
            },
            None,
            {},
        )
    )

    output = result["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert "denied for profile attribution-analyzer" in output["permissionDecisionReason"]


def test_profile_path_hook_allows_declared_read_path(tmp_path):
    settings = _settings(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))
    profile = runtime.profiles["attribution-analyzer"]
    hook = build_profile_pre_tool_use_hook(profile)

    result = asyncio.run(
        hook(
            {
                "tool_name": "Read",
                "tool_input": {"file_path": str(settings.data_dir / "evidence" / "feedback.json")},
            },
            None,
            {},
        )
    )

    assert result == {}


def test_profile_path_hook_blocks_write_outside_writable_paths(tmp_path):
    settings = _settings(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))
    profile = runtime.profiles["execution-optimizer"]
    hook = build_profile_pre_tool_use_hook(profile)

    result = asyncio.run(
        hook(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": str(settings.main_workspace_dir / "CLAUDE.md")},
            },
            None,
            {},
        )
    )

    output = result["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert "outside allowed paths for profile execution-optimizer" in output["permissionDecisionReason"]


def test_feedback_batch_plan_job_options_use_proposal_generator_profile_minimum_max_turns(tmp_path):
    settings = _settings(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))

    options = runtime.job_runner.build_options(runtime.profiles["proposal-generator"])

    assert options.max_turns == 16
    assert options.allowed_tools == []
    assert set(options.disallowed_tools) >= {"Read", "Grep", "Glob"}


def test_feedback_batch_plan_job_options_allow_global_max_turn_override(tmp_path):
    settings = _settings(tmp_path)
    settings.max_turns = 20
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))

    options = runtime.job_runner.build_options(runtime.profiles["proposal-generator"])

    assert options.max_turns == 20


def test_explicit_main_mcp_config_override_is_used_by_main_profile(tmp_path, monkeypatch):
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
    mcp_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "sec-ops-data": {"url": "http://127.0.0.1:1/mcp", "transport": "http"},
                    "forbidden": {"url": "http://127.0.0.1:2/mcp", "transport": "http"},
                }
            }
        ),
        encoding="utf-8",
    )

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
    monkeypatch.setattr(runtime.langfuse, "get_client", lambda: None)

    result = asyncio.run(runtime.run(ChatRequest(message="hello")))
    options = seen["options"]

    assert result.errors == []
    assert options.settings is None
    assert set(options.mcp_servers) == {"sec-ops-data"}
    assert options.env["CLAUDE_CONFIG_DIR"] == str(settings.main_claude_root / ".claude")


def test_main_mcp_override_does_not_replace_feedback_job_profile_mcp(tmp_path):
    base = _settings(tmp_path)
    main_mcp_path = tmp_path / "main.mcp.override.json"
    main_mcp_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "sec-ops-data": {"url": "http://127.0.0.1:1/mcp", "transport": "http"},
                }
            }
        ),
        encoding="utf-8",
    )
    settings = AppSettings(
        _env_file=None,
        WORKSPACE_DIR=base.workspace_dir,
        DATA_DIR=base.data_dir,
        CLAUDE_ROOT=base.claude_root,
        CLAUDE_HOME=base.claude_home,
        CLAUDE_MCP_CONFIG_PATH=main_mcp_path,
        ENABLE_POLICY_HOOKS=True,
    )
    settings.attribution_analyzer_workspace_dir.mkdir(parents=True, exist_ok=True)
    (settings.attribution_analyzer_workspace_dir / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "feedback-evidence": {"url": "http://127.0.0.1:2/mcp", "transport": "http"},
                    "sec-ops-data": {"url": "http://127.0.0.1:3/mcp", "transport": "http"},
                }
            }
        ),
        encoding="utf-8",
    )
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))

    options = runtime.job_runner.build_options(runtime.profiles["attribution-analyzer"])

    assert set(options.mcp_servers) == {"feedback-evidence"}


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
        LANGFUSE_BASE_URL="http://langfuse-web:3000",
        LANGFUSE_OTEL_SIGNALS="traces,metrics,logs",
        LANGFUSE_SERVICE_NAME="runtime-test",
        LANGFUSE_DEPLOYMENT_ENVIRONMENT="test",
        LANGFUSE_RESOURCE_ATTRIBUTES="service.version=0.1.0",
        LANGFUSE_EXPORT_INTERVAL_MS=500,
    )
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))
    monkeypatch.setattr(runtime.langfuse, "get_client", lambda: None)

    result = asyncio.run(runtime.run(ChatRequest(message="hello")))
    env = seen["options"].env
    expected_auth = base64.b64encode(b"pk-test:sk-test").decode()

    assert result.errors == []
    assert env["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
    assert env["CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"] == "1"
    assert env["OTEL_TRACES_EXPORTER"] == "otlp"
    assert env["OTEL_METRICS_EXPORTER"] == "otlp"
    assert env["OTEL_LOGS_EXPORTER"] == "otlp"
    assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/protobuf"
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://langfuse-web:3000/api/public/otel"
    assert env["OTEL_EXPORTER_OTLP_HEADERS"] == (f"Authorization=Basic {expected_auth},x-langfuse-ingestion-version=4")
    assert env["OTEL_SERVICE_NAME"] == "runtime-test"
    assert env["OTEL_RESOURCE_ATTRIBUTES"] == "deployment.environment=test,service.version=0.1.0"
    assert env["OTEL_TRACES_EXPORT_INTERVAL"] == "500"
    assert env["OTEL_LOG_USER_PROMPTS"] == "1"
    assert env["OTEL_LOG_TOOL_DETAILS"] == "1"
    assert env["OTEL_LOG_TOOL_CONTENT"] == "1"
    assert env["OTEL_LOG_RAW_API_BODIES"] == "1"


def test_langfuse_dspy_instrumentation_uses_current_process_otel_env(tmp_path, monkeypatch):
    from app.runtime.integrations import runtime_langfuse

    calls = []

    class FakeDSPyInstrumentor:
        def instrument(self):
            calls.append("instrument")

    openinference_module = types.ModuleType("openinference")
    instrumentation_module = types.ModuleType("openinference.instrumentation")
    dspy_module = types.ModuleType("openinference.instrumentation.dspy")
    dspy_module.DSPyInstrumentor = FakeDSPyInstrumentor
    monkeypatch.setitem(sys.modules, "openinference", openinference_module)
    monkeypatch.setitem(sys.modules, "openinference.instrumentation", instrumentation_module)
    monkeypatch.setitem(sys.modules, "openinference.instrumentation.dspy", dspy_module)
    monkeypatch.setattr(runtime_langfuse, "_DSPY_INSTRUMENTED", False)

    for key in (
        "OTEL_EXPORTER_OTLP_PROTOCOL",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_SERVICE_NAME",
        "OTEL_RESOURCE_ATTRIBUTES",
        "OTEL_TRACES_EXPORTER",
    ):
        monkeypatch.delenv(key, raising=False)

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
        LANGFUSE_BASE_URL="http://langfuse-web:3000",
        LANGFUSE_OTEL_SIGNALS="traces",
        LANGFUSE_SERVICE_NAME="runtime-test",
        LANGFUSE_DEPLOYMENT_ENVIRONMENT="test",
    )

    client = RuntimeLangfuseClient(settings)

    assert client.instrument_dspy() is True
    assert client.instrument_dspy() is True
    assert calls == ["instrument"]
    assert os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/protobuf"
    assert os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://langfuse-web:3000/api/public/otel"
    assert os.environ["OTEL_SERVICE_NAME"] == "runtime-test"
    assert os.environ["OTEL_RESOURCE_ATTRIBUTES"] == "deployment.environment=test"
    assert os.environ["OTEL_TRACES_EXPORTER"] == "otlp"


def test_langfuse_dspy_instrumentation_skips_when_disabled(tmp_path, monkeypatch):
    from app.runtime.integrations import runtime_langfuse

    monkeypatch.setattr(runtime_langfuse, "_DSPY_INSTRUMENTED", False)
    base = _settings(tmp_path)
    settings = AppSettings(
        _env_file=None,
        WORKSPACE_DIR=base.workspace_dir,
        DATA_DIR=base.data_dir,
        CLAUDE_ROOT=base.claude_root,
        CLAUDE_HOME=base.claude_home,
        LANGFUSE_ENABLED=False,
    )

    assert RuntimeLangfuseClient(settings).instrument_dspy() is False


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

    assert result.answer == ""
    assert result.errors == ["ValueError: LANGFUSE_ENABLED=true requires LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY"]


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

    assert result.errors == []
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://collector:4318"
    assert env["OTEL_LOG_USER_PROMPTS"] == "1"
    assert env["OTEL_LOG_TOOL_DETAILS"] == "1"
    assert env["OTEL_LOG_TOOL_CONTENT"] == "1"
    assert env["OTEL_LOG_RAW_API_BODIES"] == "1"


def test_settings_derives_profile_dirs_from_custom_main_paths(tmp_path):
    main_workspace = tmp_path / "runtime" / "main-workspace"
    main_root = tmp_path / "runtime" / "claude-roots" / "main"
    explicit_proposal_workspace = tmp_path / "custom-proposal-workspace"
    settings = AppSettings(
        _env_file=None,
        WORKSPACE_DIR=main_workspace,
        CLAUDE_ROOT=main_root,
        PROPOSAL_GENERATOR_WORKSPACE_DIR=explicit_proposal_workspace,
        ENABLE_POLICY_HOOKS=True,
    )

    assert settings.main_workspace_dir == main_workspace
    assert settings.attribution_analyzer_workspace_dir == main_workspace.parent / "attribution-analyzer-workspace"
    assert settings.proposal_generator_workspace_dir == explicit_proposal_workspace
    assert settings.execution_optimizer_workspace_dir == main_workspace.parent / "execution-optimizer-workspace"
    assert settings.main_claude_root == main_root
    assert settings.attribution_analyzer_claude_root == main_root.parent / "attribution-analyzer"
    assert settings.proposal_generator_claude_root == main_root.parent / "proposal-generator"
    assert settings.execution_optimizer_claude_root == main_root.parent / "execution-optimizer"
    assert settings.claude_home == main_root / ".claude"


def test_health_reports_langfuse_state_without_secrets(tmp_path, monkeypatch):
    from app.runtime.settings import get_settings

    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "docker" / "volume" / "main-workspace"))
    monkeypatch.setenv("MAIN_WORKSPACE_DIR", str(tmp_path / "docker" / "volume" / "main-workspace"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "docker" / "volume" / "data"))
    monkeypatch.setenv("CLAUDE_ROOT", str(tmp_path / "docker" / "volume" / "claude-roots" / "main"))
    monkeypatch.setenv("MAIN_CLAUDE_ROOT", str(tmp_path / "docker" / "volume" / "claude-roots" / "main"))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "docker" / "volume" / "claude-roots" / "main" / ".claude"))
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

    from app.routers.core import build_health_payload

    result = build_health_payload(settings=settings, app=main.app, agent_version_store=main.agent_version_store)

    assert result.langfuse_enabled is True
    assert result.langfuse_otel_endpoint_configured is True
    assert result.langfuse_public_key_configured is True
    assert result.langfuse_secret_key_configured is True
    assert result.langfuse_otel_signals == ["traces", "logs"]
    assert result.runtime_dependency_versions.claude_agent_sdk
    assert result.runtime_dependency_versions.langfuse
    assert result.runtime_dependency_versions.opentelemetry_sdk
    serialized = str(result)
    assert "pk-test" not in serialized
    assert "sk-test" not in serialized
    assert "OTEL_EXPORTER_OTLP_HEADERS" not in serialized


def test_run_normalizes_result_error_and_dedupes_answer(tmp_path, monkeypatch):
    from app.runtime.agent_job_runner import ClaudeCodeResultError
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
        raise ClaudeCodeResultError("Claude Code API error (404): bad model")

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    settings = _settings(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))

    result = asyncio.run(runtime.run(ChatRequest(message="hello")))

    assert result.answer == "bad model"
    assert result.errors == ["Claude Code API error (404): bad model"]


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
    propagations = []

    def fake_propagate_attributes(**kwargs):
        propagations.append(kwargs)
        return nullcontext()

    monkeypatch.setattr(runtime.langfuse, "get_client", lambda: fake_langfuse)
    monkeypatch.setattr(runtime.langfuse, "propagate_attributes", fake_propagate_attributes)

    result = asyncio.run(
        runtime.run(
            ChatRequest(
                message="hello",
                alert_id="alert-1",
                case_id="case-1",
                metadata={"api_key": "secret", "tenant_id": "tenant-a", "user_id": "user-a"},
            )
        )
    )

    assert result.answer == "hello answer"
    assert result.agent_activity["requested_skills"] == []
    assert result.agent_activity["tool_names"] == [
        "Skill",
        "Read",
        "mcp__sec-ops-data__local_api__list_assets_api_v1_assets_get",
    ]
    assert result.agent_activity["skill_calls"][0]["name"] == "trace-debugger"
    assert result.agent_activity["tool_results"][0]["tool_use_id"] == "toolu-read"
    assert result.agent_activity["tool_results"][1]["name"] == ("mcp__sec-ops-data__local_api__list_assets_api_v1_assets_get")
    assert fake_langfuse.flushed is True
    assert [obs.kwargs["name"] for obs in fake_langfuse.observations] == [
        "runtime.main_agent",
        "runtime.main_agent.claude_sdk_query",
    ]
    root, generation = fake_langfuse.observations
    assert root.kwargs["input"]["message"] == "hello"
    assert root.kwargs["input"]["metadata"]["api_key"] == "secret"
    assert root.kwargs["metadata"]["run_id"] == result.run_id
    assert root.kwargs["metadata"]["api_session_id"] == result.session_id
    assert root.kwargs["metadata"]["alert_id"] == "alert-1"
    assert root.kwargs["metadata"]["case_id"] == "case-1"
    assert generation.kwargs["metadata"]["run_id"] == result.run_id
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
    assert propagations == [
        {
            "user_id": "user-a",
            "session_id": result.session_id,
            "metadata": {
                "api_session_id": result.session_id,
                "run_id": result.run_id,
                "alert_id": "alert-1",
                "case_id": "case-1",
                "mode": "non_stream",
                "profile": "main-agent",
                "skills_mode": "default",
                "tenant_id": "tenant-a",
            },
            "trace_name": "runtime.main_agent",
        }
    ]
    assert "api_key" not in propagations[0]["metadata"]


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
    monkeypatch.setattr(runtime.langfuse, "get_client", lambda: fake_langfuse)

    events = asyncio.run(collect(runtime))

    assert [event["event"] for event in events] == ["session", "message", "message", "message", "result", "done"]
    result_event = next(event for event in events if event["event"] == "result")
    assert result_event["data"]["agent_activity"]["tool_names"] == ["Read"]
    assert fake_langfuse.flushed is True
    root, generation = fake_langfuse.observations
    assert [obs.kwargs["name"] for obs in fake_langfuse.observations] == [
        "runtime.main_agent",
        "runtime.main_agent.claude_sdk_query",
    ]
    assert root.updates[-1]["output"]["answer"] == "stream answer"
    assert root.updates[-1]["output"]["stop_reason"] == "end_turn"
    assert root.trace_io_updates[-1]["input"]["message"] == "stream"
    assert root.trace_io_updates[-1]["output"]["answer"] == "stream answer"
    assert generation.updates[-1]["usage_details"] == {"input_tokens": 7, "output_tokens": 11}
