import asyncio

from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.schemas import ChatRequest
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import AppSettings


def _settings(tmp_path):
    workspace = tmp_path / "workspace"
    data = tmp_path / "data"
    claude_root = tmp_path / "claude-root"
    claude_home = claude_root / ".claude"
    workspace.mkdir()
    claude_home.mkdir(parents=True)
    return AppSettings(
        _env_file=None,
        WORKSPACE_DIR=workspace,
        DATA_DIR=data,
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
        CLAUDE_HOME=base.claude_home,
        CLAUDE_SETTINGS_PATH=settings_path,
        CLAUDE_MCP_CONFIG_PATH=mcp_path,
        CLAUDE_CONFIG_DIR=config_dir,
        ENABLE_POLICY_HOOKS=True,
    )
    runtime = ClaudeRuntime(settings, LocalSessionStore(settings.session_dir))

    result = asyncio.run(runtime.run(ChatRequest(message="hello")))
    options = seen["options"]

    assert result["errors"] == []
    assert options.settings == str(settings_path)
    assert options.mcp_servers == str(mcp_path)
    assert options.env["CLAUDE_CONFIG_DIR"] == str(config_dir)


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
