import asyncio
import base64
import json
import os
import sys
import types
from contextlib import contextmanager

import pytest
from app.runtime.agent_job_errors import AGENT_AUTH_REQUIRED, AgentAuthenticationRequiredError
from app.runtime.agent_profiles import build_business_agent_profile, candidate_profile
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.errors import BusinessRuleViolation
from app.runtime.integrations.runtime_langfuse import RuntimeLangfuseClient
from app.runtime.model_provider import LITELLM_SIDECAR_BASE_URL, LOCAL_PROVIDER_DUMMY_API_KEY
from app.runtime.schemas import ChatRequest
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import AppSettings


class FakeOtelSpan:
    def __init__(self):
        self.attributes = {}

    def set_attributes(self, attributes):
        self.attributes.update(attributes)

    def set_attribute(self, key, value):
        self.attributes[key] = value


class FakeLangfuseObservation:
    def __init__(self, kwargs):
        self.kwargs = kwargs
        self.updates = []
        self.trace_io_updates = []
        self.exited = False
        self._otel_span = FakeOtelSpan()

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

    def get_current_trace_id(self):
        return "trace-test"

    def get_trace_url(self, *, trace_id):
        return f"http://langfuse.local/traces/{trace_id}"

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
    )


async def _collect_prompt(prompt):
    items = []
    async for item in prompt:
        items.append(item)
    return items


async def _success_result(options, text=""):
    from claude_agent_sdk import ResultMessage

    await _mirror_entry(options)
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=0,
        is_error=False,
        num_turns=1,
        session_id=options.resume or options.session_id,
        result=text,
    )


async def _mirror_entry(options, *, entry_uuid="test-transcript-entry"):
    sdk_session_id = options.resume or options.session_id
    await options.session_store.append(
        {
            "project_key": options.session_store.binding.project_key,
            "session_id": sdk_session_id,
        },
        [{"type": "user", "uuid": entry_uuid}],
    )


def test_run_without_native_ask_uses_finite_streaming_prompt(tmp_path, monkeypatch):
    seen = {}

    async def fake_query(*, prompt, options, transport=None):
        seen["prompt_is_string"] = isinstance(prompt, str)
        seen["prompt_items"] = await _collect_prompt(prompt)
        yield await _success_result(options)

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


@pytest.mark.parametrize("streaming", [False, True])
def test_runtime_keeps_event_loop_responsive_during_blocking_failure_boundaries(
    tmp_path,
    monkeypatch,
    streaming: bool,
):
    import threading
    import time

    settings = _settings(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))
    provider_started = threading.Event()
    abort_started = threading.Event()
    started_at: dict[str, float] = {}

    def blocking_provider_check() -> None:
        started_at["provider"] = time.monotonic()
        provider_started.set()
        time.sleep(0.25)
        raise RuntimeError("provider unavailable")

    original_abort = runtime._abort_runtime_request

    def blocking_abort(*args, **kwargs) -> None:
        started_at["abort"] = time.monotonic()
        abort_started.set()
        time.sleep(0.25)
        original_abort(*args, **kwargs)

    monkeypatch.setattr(runtime.model_provider_router, "ensure_agent_runtime_ready", blocking_provider_check)
    monkeypatch.setattr(runtime, "_abort_runtime_request", blocking_abort)

    async def scheduling_latency(started: threading.Event, phase: str) -> float:
        assert await asyncio.to_thread(started.wait, 1)
        return time.monotonic() - started_at[phase]

    async def invoke_runtime():
        provider_latency = asyncio.create_task(scheduling_latency(provider_started, "provider"))
        abort_latency = asyncio.create_task(scheduling_latency(abort_started, "abort"))
        await asyncio.sleep(0)
        if streaming:
            result = [event async for event in runtime.stream(ChatRequest(message="hello"))]
        else:
            result = await runtime.run(ChatRequest(message="hello"))
        return result, await provider_latency, await abort_latency

    result, provider_latency, abort_latency = asyncio.run(invoke_runtime())

    assert provider_latency < 0.15
    assert abort_latency < 0.15
    if streaming:
        assert any(event.get("event") == "error" for event in result)
    else:
        assert result.errors == ["RuntimeError: provider unavailable"]


def test_runtime_rejects_file_checkpointing_with_durable_session_store(tmp_path):
    from app.runtime.errors import RuntimeUnavailableError

    settings = _settings(tmp_path)
    settings.enable_file_checkpointing = True

    with pytest.raises(RuntimeUnavailableError, match="incompatible"):
        ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))


def test_runtime_resolves_non_main_agent_for_internal_callers(tmp_path, monkeypatch):
    seen = {}

    async def fake_query(*, prompt, options, transport=None):
        seen["cwd"] = options.cwd
        seen["profile"] = options.env["AGENT_PROFILE"]
        async for _ in prompt:
            pass
        yield await _success_result(options)

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    settings = _settings(tmp_path)
    workspace = settings.data_dir / "business-agents" / "soc-ops" / "workspace"
    workspace.mkdir(parents=True)
    profile = build_business_agent_profile(settings, agent_id="soc-ops", workspace_dir=workspace)
    resolved = []
    runtime = ClaudeRuntime(
        settings,
        LocalSessionStore(settings.session_dir),
        business_profile_resolver=lambda agent_id: resolved.append(agent_id) or profile,
    )

    result = asyncio.run(runtime.run(ChatRequest(message="evaluate", agent_id="soc-ops")))

    assert resolved == ["soc-ops"]
    assert seen == {"cwd": workspace, "profile": "soc-ops"}
    assert runtime.session_store.get(result.session_id).agent_id == "soc-ops"


def test_runtime_rejects_explicit_profile_agent_mismatch(tmp_path):
    settings = _settings(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))

    with pytest.raises(BusinessRuleViolation, match="does not match"):
        asyncio.run(
            runtime.run(
                ChatRequest(message="wrong owner", agent_id="soc-ops"),
                profile=runtime.profiles["main-agent"],
            )
        )


def test_default_options_use_main_runtime_profile(tmp_path, monkeypatch):
    seen = {}

    async def fake_query(*, prompt, options, transport=None):
        seen["options"] = options
        async for _ in prompt:
            pass
        yield await _success_result(options)

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    settings = _settings(tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/tmp/should-not-leak")
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))

    result = asyncio.run(runtime.run(ChatRequest(message="hello")))
    options = seen["options"]

    assert result.errors == []
    assert list(getattr(options, "setting_sources", None) or []) == ["project"]
    assert getattr(options, "settings", None) is None
    assert getattr(options, "mcp_servers", None) in (None, {})
    assert getattr(options, "agents", None) is None
    assert getattr(options, "allowed_tools", None) in (None, [])
    assert getattr(options, "disallowed_tools", None) in (None, [])
    assert getattr(options, "permission_mode", None) is None
    assert getattr(options, "can_use_tool", None) is None
    assert getattr(options, "permission_prompt_tool_name", None) is None
    assert getattr(options, "hooks", None) is None
    assert options.cwd == settings.main_workspace_dir
    assert options.env["HOME"] == str(settings.main_claude_root)
    assert options.env["CLAUDE_CONFIG_DIR"] == str(settings.main_claude_root / ".claude")
    assert options.env["DATA_DIR"] == str(settings.data_dir)
    assert options.env["CLAUDE_HOOK_AUDIT_LOG"] == str(settings.data_dir / "transcripts" / "claude-hook-audit.jsonl")
    assert options.env["AGENT_PROFILE"] == "main-agent"
    assert "CLAUDE_CODE_ENABLE_TELEMETRY" not in options.env


def test_candidate_runtime_uses_business_agent_owner_for_session_and_maintenance(tmp_path, monkeypatch):
    seen_maintenance_agents = []

    async def fake_query(*, prompt, options, transport=None):
        async for _ in prompt:
            pass
        yield await _success_result(options)

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    settings = _settings(tmp_path)
    session_store = LocalSessionStore(settings.session_dir)
    runtime = ClaudeRuntime(
        settings,
        session_store,
        agent_version_maintenance_provider=lambda agent_id: seen_maintenance_agents.append(agent_id) or False,
    )
    worktree = tmp_path / "candidate-worktree"
    worktree.mkdir()
    profile = candidate_profile(settings, agent_id="soc-ops", workspace_dir=worktree, candidate_id="agc-test")

    result = asyncio.run(
        runtime.run(
            ChatRequest(message="candidate regression", session_id="candidate-session", agent_id="soc-ops"),
            profile=profile,
            agent_version_id_override="candidate-sha",
        )
    )

    session = session_store.get("candidate-session")
    assert result.agent_version_id == "candidate-sha"
    assert seen_maintenance_agents == ["soc-ops"]
    assert session is not None
    assert session.agent_id == "soc-ops"


def test_profile_env_marks_backend_owned_workspace_trusted(tmp_path):
    settings = _settings(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))
    profile = runtime.profiles["main-agent"]
    state_path = profile.claude_config_dir / ".claude.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "cachedGrowthBookFeatures": {"keep": True},
                "projects": {"/other/workspace": {"hasTrustDialogAccepted": False}},
            }
        ),
        encoding="utf-8",
    )

    env = runtime._profile_env(profile)

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert env["CLAUDE_CONFIG_DIR"] == str(profile.claude_config_dir)
    assert state["cachedGrowthBookFeatures"] == {"keep": True}
    assert state["projects"]["/other/workspace"]["hasTrustDialogAccepted"] is False
    assert state["projects"][profile.workspace_dir.as_posix()]["hasTrustDialogAccepted"] is True


def test_main_runtime_profile_does_not_inject_mcp_servers(tmp_path, monkeypatch):
    seen = {}

    async def fake_query(*, prompt, options, transport=None):
        seen["options"] = options
        async for _ in prompt:
            pass
        yield await _success_result(options)

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)

    settings = _settings(tmp_path)
    settings.main_workspace_dir.mkdir(parents=True, exist_ok=True)
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
    assert getattr(seen["options"], "mcp_servers", None) in (None, {})


def test_feedback_attribution_job_options_use_profile_minimum_max_turns(tmp_path):
    settings = _settings(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))

    options = runtime.job_runner.build_options(runtime.profiles["governor"])

    assert options.max_turns == 16


def test_feedback_attribution_job_options_allow_global_max_turn_override(tmp_path):
    settings = _settings(tmp_path)
    settings.max_turns = 20
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))

    options = runtime.job_runner.build_options(runtime.profiles["governor"])

    assert options.max_turns == 20


def test_feedback_job_profile_does_not_inject_mcp_servers(tmp_path):
    settings = _settings(tmp_path)
    settings.governor_workspace_dir.mkdir(parents=True, exist_ok=True)
    (settings.governor_workspace_dir / ".mcp.json").write_text(
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

    options = runtime.job_runner.build_options(runtime.profiles["governor"])

    assert getattr(options, "mcp_servers", None) in (None, {})


def test_feedback_job_options_inject_model_provider_credentials(tmp_path):
    base = _settings(tmp_path)
    settings = AppSettings(
        _env_file=None,
        WORKSPACE_DIR=base.workspace_dir,
        DATA_DIR=base.data_dir,
        CLAUDE_ROOT=base.claude_root,
        CLAUDE_HOME=base.claude_home,
        MODEL_PROVIDER_API_KEY="sk-test-provider",
        MODEL_PROVIDER_API_URL="https://model-gateway.example.test/anthropic",
    )
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))

    options = runtime.job_runner.build_options(runtime.profiles["governor"])

    assert options.env["ANTHROPIC_API_KEY"] == "sk-test-provider"
    assert options.env["ANTHROPIC_BASE_URL"] == "https://model-gateway.example.test/anthropic"


def test_vllm_feedback_job_options_use_derived_litellm_sidecar(tmp_path, monkeypatch):
    import app.runtime.model_provider as model_provider

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def getcode(self):
            return 200

        def read(self, _):
            return b'{"version":"0.14.0"}'

    def fake_urlopen(*_, **__):
        return FakeResponse()

    monkeypatch.setattr(model_provider, "urlopen", fake_urlopen)
    base = _settings(tmp_path)
    settings = AppSettings(
        _env_file=None,
        WORKSPACE_DIR=base.workspace_dir,
        DATA_DIR=base.data_dir,
        CLAUDE_ROOT=base.claude_root,
        CLAUDE_HOME=base.claude_home,
        MODEL_PROVIDER_BACKEND="vllm",
        MODEL_PROVIDER_API_URL="http://vllm:8000",
    )
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))

    options = runtime.job_runner.build_options(runtime.profiles["governor"])

    assert options.env["ANTHROPIC_API_KEY"] == LOCAL_PROVIDER_DUMMY_API_KEY
    assert options.env["ANTHROPIC_BASE_URL"] == LITELLM_SIDECAR_BASE_URL


def test_background_agent_job_requires_model_credentials_before_query(tmp_path, monkeypatch):
    called = False

    async def fake_query(*, prompt, options, transport=None):
        nonlocal called
        called = True
        async for _ in prompt:
            pass
        yield await _success_result(options)

    import claude_agent_sdk

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    settings = _settings(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))

    with pytest.raises(AgentAuthenticationRequiredError) as exc_info:
        asyncio.run(
            runtime._run_profile_json(
                profile_name="governor",
                prompt="assess regression coverage",
                job_type="regression_assessment",
                job_input={},
            )
        )

    assert called is False
    assert exc_info.value.error_code == AGENT_AUTH_REQUIRED
    assert exc_info.value.raw_output_json is not None
    assert exc_info.value.raw_output_json["profile_name"] == "governor"
    assert exc_info.value.raw_output_json["missing"] == ["MODEL_PROVIDER_API_KEY", "ANTHROPIC_API_KEY"]


def test_governor_job_options_use_profile_minimum_max_turns(tmp_path):
    settings = _settings(tmp_path)
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))

    options = runtime.job_runner.build_options(runtime.profiles["governor"])

    assert options.max_turns == 16
    assert getattr(options, "allowed_tools", None) in (None, [])
    assert getattr(options, "disallowed_tools", None) in (None, [])


def test_governor_job_options_allow_global_max_turn_override(tmp_path):
    settings = _settings(tmp_path)
    settings.max_turns = 20
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))

    options = runtime.job_runner.build_options(runtime.profiles["governor"])

    assert options.max_turns == 20


def test_explicit_main_mcp_config_override_is_not_injected_into_options(tmp_path, monkeypatch):
    seen = {}

    async def fake_query(*, prompt, options, transport=None):
        seen["options"] = options
        async for _ in prompt:
            pass
        yield await _success_result(options)

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
    )
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))
    monkeypatch.setattr(runtime.langfuse, "get_client", lambda: None)

    result = asyncio.run(runtime.run(ChatRequest(message="hello")))
    options = seen["options"]

    assert result.errors == []
    assert getattr(options, "settings", None) is None
    assert getattr(options, "mcp_servers", None) in (None, {})
    assert options.env["CLAUDE_CONFIG_DIR"] == str(settings.main_claude_root / ".claude")


def test_main_mcp_override_does_not_inject_feedback_job_profile_mcp(tmp_path):
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
    )
    settings.governor_workspace_dir.mkdir(parents=True, exist_ok=True)
    (settings.governor_workspace_dir / ".mcp.json").write_text(
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

    options = runtime.job_runner.build_options(runtime.profiles["governor"])

    assert getattr(options, "mcp_servers", None) in (None, {})


def test_langfuse_env_is_passed_to_claude_sdk(tmp_path, monkeypatch):
    seen = {}

    async def fake_query(*, prompt, options, transport=None):
        seen["options"] = options
        async for _ in prompt:
            pass
        yield await _success_result(options)

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
        yield await _success_result(options)

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


def test_settings_derives_profile_dirs_from_data_dir(tmp_path):
    # main 已并入业务模型：其 workspace/claude-root 由 data_dir 下的 main-agent layout 派生
    # （不再有独立 WORKSPACE_DIR/CLAUDE_ROOT 字段）；governor 顶层目录随运行卷根派生。
    data_dir = tmp_path / "runtime" / "data"
    settings = AppSettings(_env_file=None, DATA_DIR=data_dir)

    assert settings.main_workspace_dir == data_dir / "business-agents" / "main-agent" / "workspace"
    assert settings.main_claude_root == data_dir / "business-agents" / "main-agent" / "claude-root"
    assert settings.claude_home == settings.main_claude_root / ".claude"
    assert settings.governor_workspace_dir == data_dir.parent / "governor-workspace"
    assert settings.governor_claude_root == data_dir.parent / "claude-roots" / "governor"


def test_settings_honors_explicit_governor_workspace_override(tmp_path):
    data_dir = tmp_path / "runtime" / "data"
    explicit_governor_workspace = tmp_path / "custom-governor-workspace"
    settings = AppSettings(
        _env_file=None,
        DATA_DIR=data_dir,
        GOVERNOR_WORKSPACE_DIR=explicit_governor_workspace,
    )

    assert settings.governor_workspace_dir == explicit_governor_workspace
    # governor_claude_root 未显式给则随运行卷根（data_dir.parent）派生。
    assert settings.governor_claude_root == data_dir.parent / "claude-roots" / "governor"


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

    result = build_health_payload(settings=settings, app=main.app)

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
        sdk_session_id = options.resume or options.session_id
        await _mirror_entry(options, entry_uuid="result-error-entry")
        yield AssistantMessage(
            content=[TextBlock(text="bad model")],
            model="<synthetic>",
            error="invalid_request",
            session_id=sdk_session_id,
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=True,
            num_turns=1,
            session_id=sdk_session_id,
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
        sdk_session_id = options.resume or options.session_id
        await _mirror_entry(options, entry_uuid="langfuse-run-entry")
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
            session_id=sdk_session_id,
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id=sdk_session_id,
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
    )
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))
    fake_langfuse = FakeLangfuseClient()
    propagations = []
    trace_upserts = []
    propagation_active = {"value": False}
    observation_started_under_propagation: list[bool] = []

    def fake_propagate_attributes(**kwargs):
        propagations.append(kwargs)

        @contextmanager
        def active_context():
            propagation_active["value"] = True
            try:
                yield
            finally:
                propagation_active["value"] = False

        return active_context()

    def tracked_start_as_current_observation(**kwargs):
        observation_started_under_propagation.append(propagation_active["value"])
        return FakeLangfuseClient.start_as_current_observation(fake_langfuse, **kwargs)

    monkeypatch.setattr(runtime.langfuse, "get_client", lambda: fake_langfuse)
    monkeypatch.setattr(runtime.langfuse, "propagate_attributes", fake_propagate_attributes)
    monkeypatch.setattr(runtime.langfuse, "upsert_trace", lambda trace_id, **kwargs: trace_upserts.append({"trace_id": trace_id, **kwargs}))
    fake_langfuse.start_as_current_observation = tracked_start_as_current_observation

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
    assert "permission_policy_source" not in result.agent_activity
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
        "runtime.business_agent.main-agent",
        "runtime.business_agent.main-agent.claude_sdk_query",
    ]
    assert observation_started_under_propagation == [True, True]
    root, generation = fake_langfuse.observations
    assert root._otel_span.attributes["session.id"] == result.session_id
    assert root._otel_span.attributes["langfuse.trace.name"] == "runtime.business_agent.main-agent"
    assert generation._otel_span.attributes["session.id"] == result.session_id
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
    assert trace_upserts == [
        {
            "trace_id": "trace-test",
            "name": "runtime.business_agent.main-agent",
            "session_id": result.session_id,
            "user_id": "user-a",
            "input": root.kwargs["input"],
            "output": root.updates[-1]["output"],
            "metadata": propagations[0]["metadata"],
            "tags": ["role:business", "agent:main-agent"],
        }
    ]
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
                "claude_web_hitl_enabled": "false",
                "profile": "main-agent",
                "tenant_id": "tenant-a",
            },
            "trace_name": "runtime.business_agent.main-agent",
            "tags": ["role:business", "agent:main-agent"],
        }
    ]
    assert "api_key" not in propagations[0]["metadata"]


def test_stream_enriches_langfuse_input_output(tmp_path, monkeypatch):
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    async def fake_query(*, prompt, options, transport=None):
        async for _ in prompt:
            pass
        sdk_session_id = options.resume or options.session_id
        await _mirror_entry(options, entry_uuid="langfuse-stream-entry")
        yield {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "README.md"},
            "tool_use_id": "toolu-read",
        }
        yield AssistantMessage(
            content=[TextBlock(text="stream answer")],
            model="<synthetic>",
            session_id=sdk_session_id,
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=0,
            is_error=False,
            num_turns=1,
            session_id=sdk_session_id,
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
    )
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))
    fake_langfuse = FakeLangfuseClient()
    propagations = []
    trace_upserts = []
    propagation_active = {"value": False}
    observation_started_under_propagation: list[bool] = []

    def fake_propagate_attributes(**kwargs):
        propagations.append(kwargs)

        @contextmanager
        def active_context():
            propagation_active["value"] = True
            try:
                yield
            finally:
                propagation_active["value"] = False

        return active_context()

    def tracked_start_as_current_observation(**kwargs):
        observation_started_under_propagation.append(propagation_active["value"])
        return FakeLangfuseClient.start_as_current_observation(fake_langfuse, **kwargs)

    monkeypatch.setattr(runtime.langfuse, "get_client", lambda: fake_langfuse)
    monkeypatch.setattr(runtime.langfuse, "propagate_attributes", fake_propagate_attributes)
    monkeypatch.setattr(runtime.langfuse, "upsert_trace", lambda trace_id, **kwargs: trace_upserts.append({"trace_id": trace_id, **kwargs}))
    fake_langfuse.start_as_current_observation = tracked_start_as_current_observation

    events = asyncio.run(collect(runtime))

    assert [event["event"] for event in events] == ["session", "message", "message", "message", "result", "done"]
    result_event = next(event for event in events if event["event"] == "result")
    assert result_event["data"]["agent_activity"]["tool_names"] == ["Read"]
    assert fake_langfuse.flushed is True
    root, generation = fake_langfuse.observations
    assert [obs.kwargs["name"] for obs in fake_langfuse.observations] == [
        "runtime.business_agent.main-agent",
        "runtime.business_agent.main-agent.claude_sdk_query",
    ]
    assert observation_started_under_propagation == [True, True]
    assert propagations[0]["session_id"] == result_event["data"]["session_id"]
    assert propagations[0]["metadata"]["api_session_id"] == result_event["data"]["session_id"]
    assert root._otel_span.attributes["session.id"] == result_event["data"]["session_id"]
    assert root._otel_span.attributes["langfuse.trace.name"] == "runtime.business_agent.main-agent"
    assert generation._otel_span.attributes["session.id"] == result_event["data"]["session_id"]
    assert root.updates[-1]["output"]["answer"] == "stream answer"
    assert root.updates[-1]["output"]["stop_reason"] == "end_turn"
    assert root.trace_io_updates[-1]["input"]["message"] == "stream"
    assert root.trace_io_updates[-1]["output"]["answer"] == "stream answer"
    assert generation.updates[-1]["usage_details"] == {"input_tokens": 7, "output_tokens": 11}
    assert trace_upserts == [
        {
            "trace_id": "trace-test",
            "name": "runtime.business_agent.main-agent",
            "session_id": result_event["data"]["session_id"],
            "user_id": None,
            "input": root.kwargs["input"],
            "output": root.updates[-1]["output"],
            "metadata": propagations[0]["metadata"],
            "tags": ["role:business", "agent:main-agent"],
        }
    ]
