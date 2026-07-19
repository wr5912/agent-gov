from pathlib import Path

import pytest
from app.runtime.protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from app.runtime.settings import (
    AppSettings,
    runtime_settings_log_fields,
    runtime_settings_log_message,
    settings_env_file_for_mode,
    validate_hitl_single_api_process,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

_PROFILE_ENV_KEYS = (
    "API_KEY",
    "API_PORT",
    "LOG_LEVEL",
    "RUNTIME_VOLUME_MODE",
    "RUNTIME_CONTAINER",
    "HOST_RUNTIME_VOLUME_ROOT",
    "HOST_GOVERNOR_WORKSPACE_MOUNT",
    "HOST_DATA_MOUNT",
    "HOST_GOVERNOR_CLAUDE_ROOT_MOUNT",
    "GOVERNOR_WORKSPACE_DIR",
    "DATA_DIR",
    "GOVERNOR_CLAUDE_ROOT",
    "LANGFUSE_BASE_URL",
    "GOVERNANCE_AGENT_TIMEOUT_SECONDS",
    "AGENT_TEST_RUN_TIMEOUT_SECONDS",
    "DSPY_OUTPUT_FORMATTER_TIMEOUT_SECONDS",
    "HITL_TIMEOUT_SECONDS",
)


def test_settings_exposes_only_the_general_api_key() -> None:
    settings = AppSettings(_env_file=None, API_KEY="  general-secret  ")

    assert settings.api_key == "general-secret"
    assert not hasattr(settings, "response_orchestrator_api_key")


def test_settings_selects_container_env_file_when_container_marker_is_set(tmp_path, monkeypatch):
    for key in _PROFILE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("RUNTIME_CONTAINER", "1")
    monkeypatch.chdir(tmp_path)

    docker_dir = tmp_path / "docker"
    docker_dir.mkdir()
    container_env = docker_dir / ".env"
    local_debug_env = docker_dir / ".env.local-debug"
    container_env.write_text(
        "\n".join(
            [
                "API_PORT=58080",
                "GOVERNOR_WORKSPACE_DIR=/governor-workspace",
                "DATA_DIR=/data",
                "LANGFUSE_BASE_URL=http://langfuse-web:3000",
                "",
            ]
        ),
        encoding="utf-8",
    )
    local_debug_env.write_text(
        "\n".join(
            [
                "HOST_RUNTIME_VOLUME_ROOT=/tmp/local-debug-volume-agent-gov",
                "API_PORT=9090",
                "DATA_DIR=${HOST_RUNTIME_VOLUME_ROOT}/data",
                "LANGFUSE_BASE_URL=http://localhost:53000",
                "",
            ]
        ),
        encoding="utf-8",
    )

    settings = AppSettings()

    assert settings_env_file_for_mode() == Path("docker/.env")
    assert AppSettings.model_config["env_file"] is None
    assert settings.runtime_volume_mode == "container"
    assert settings.api_port == 58080
    assert settings.workspace_dir == settings.data_dir / "business-agents" / DEFAULT_BUSINESS_AGENT_ID / "workspace"
    assert settings.default_workspace_dir == settings.workspace_dir
    assert settings.governor_workspace_dir == Path("/governor-workspace")
    assert settings.data_dir == Path("/data")
    assert settings.claude_root == settings.data_dir / "business-agents" / DEFAULT_BUSINESS_AGENT_ID / "claude-root"
    assert settings.default_claude_root == settings.claude_root
    assert settings.governor_claude_root == Path("/claude-roots/governor")
    assert settings.claude_home == settings.claude_root / ".claude"
    assert settings.langfuse_base_url == "http://langfuse-web:3000"
    assert settings.log_level == "info"


def test_settings_selects_local_debug_env_file_for_host_runtime(tmp_path, monkeypatch):
    for key in _PROFILE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("RUNTIME_CONTAINER", "0")
    monkeypatch.chdir(tmp_path)

    docker_dir = tmp_path / "docker"
    docker_dir.mkdir()
    (docker_dir / ".env").write_text(
        "\n".join(
            [
                "API_PORT=58080",
                "DATA_DIR=/data",
                "LANGFUSE_BASE_URL=http://langfuse-web:3000",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (docker_dir / ".env.local-debug").write_text(
        "\n".join(
            [
                "HOST_RUNTIME_VOLUME_ROOT=/tmp/local-debug-volume-agent-gov",
                "API_PORT=8080",
                "DATA_DIR=${HOST_RUNTIME_VOLUME_ROOT}/data",
                "LANGFUSE_BASE_URL=http://localhost:53000",
                "",
            ]
        ),
        encoding="utf-8",
    )

    settings = AppSettings()

    assert settings_env_file_for_mode() == Path("docker/.env.local-debug")
    assert settings.runtime_volume_mode == "local-debug"
    assert settings.api_port == 8080
    assert settings.workspace_dir == settings.data_dir / "business-agents" / DEFAULT_BUSINESS_AGENT_ID / "workspace"
    assert settings.data_dir == Path("/tmp/local-debug-volume-agent-gov/data")
    assert settings.claude_root == settings.data_dir / "business-agents" / DEFAULT_BUSINESS_AGENT_ID / "claude-root"
    assert settings.langfuse_base_url == "http://localhost:53000"


def test_settings_local_debug_env_uses_tmp_runtime_root(monkeypatch):
    for key in _PROFILE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    settings = AppSettings(
        _env_file=REPO_ROOT / "docker/.env.local-debug.example",
    )

    local_debug_root = Path("/tmp/local-debug-volume-agent-gov")
    assert settings.runtime_volume_mode == "local-debug"
    assert settings.api_host == "0.0.0.0"
    assert settings.host_runtime_volume_root == local_debug_root.as_posix()
    default_layout_root = local_debug_root / "data" / "business-agents" / DEFAULT_BUSINESS_AGENT_ID
    assert settings.workspace_dir == default_layout_root / "workspace"
    assert settings.default_workspace_dir == default_layout_root / "workspace"
    assert settings.governor_workspace_dir == local_debug_root / "governor-workspace"
    assert settings.data_dir == local_debug_root / "data"
    assert settings.claude_root == default_layout_root / "claude-root"
    assert settings.default_claude_root == default_layout_root / "claude-root"
    assert settings.claude_home == default_layout_root / "claude-root" / ".claude"
    assert settings.agent_git_worktrees_dir == default_layout_root / "version" / "worktrees"
    assert settings.agent_release_archives_dir == default_layout_root / "version" / "releases"


def test_runtime_settings_log_fields_are_explicit_and_non_secret(monkeypatch):
    for key in _PROFILE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    settings = AppSettings(
        _env_file=REPO_ROOT / "docker/.env.local-debug.example",
    )
    fields = runtime_settings_log_fields(settings)

    assert fields == {
        "runtime_volume_mode": "local-debug",
        "log_level": "debug",
        "settings_env_file": (REPO_ROOT / "docker/.env.local-debug.example").as_posix(),
        "settings_env_file_exists": True,
        "model_provider_backend": "anthropic_compatible",
        "model_provider_vllm_sidecar_threshold": "0.23.0",
        "model_provider_vllm_allow_direct": False,
        "provider_api_key_configured": False,
        "provider_api_url_configured": False,
        "governance_agent_timeout_seconds": 300,
        "dspy_output_formatter_timeout_seconds": 300,
        "agent_test_run_timeout_seconds": 1800,
        "prompt_suggestion_source": "backend",
        "claude_web_hitl_enabled": False,
        "hitl_timeout_seconds": 300,
        "api_host": "0.0.0.0",
        "api_port": 8080,
        "workspace_dir": f"/tmp/local-debug-volume-agent-gov/data/business-agents/{DEFAULT_BUSINESS_AGENT_ID}/workspace",
        "data_dir": "/tmp/local-debug-volume-agent-gov/data",
        "claude_root": f"/tmp/local-debug-volume-agent-gov/data/business-agents/{DEFAULT_BUSINESS_AGENT_ID}/claude-root",
        "langfuse_base_url": "http://localhost:53000",
    }
    assert fields["provider_api_key_configured"] is False
    assert not any("secret" in name.lower() for name in fields)


def test_runtime_settings_log_exposes_prompt_suggestion_source(monkeypatch):
    for key in _PROFILE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("ENABLE_BACKEND_PROMPT_SUGGESTION", raising=False)

    native = AppSettings(_env_file=None)
    backend = AppSettings(_env_file=None, ENABLE_BACKEND_PROMPT_SUGGESTION=True)

    assert runtime_settings_log_fields(native)["prompt_suggestion_source"] == "claude_native"
    assert runtime_settings_log_fields(backend)["prompt_suggestion_source"] == "backend"
    assert "prompt_suggestion_source=backend" in runtime_settings_log_message(backend)


def test_governance_and_hitl_timeout_defaults_and_overrides(monkeypatch):
    for key in _PROFILE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    defaults = AppSettings(_env_file=None)
    inherited_formatter = AppSettings(_env_file=None, GOVERNANCE_AGENT_TIMEOUT_SECONDS=123)
    overridden_formatter = AppSettings(
        _env_file=None,
        GOVERNANCE_AGENT_TIMEOUT_SECONDS=123,
        DSPY_OUTPUT_FORMATTER_TIMEOUT_SECONDS=45,
        HITL_TIMEOUT_SECONDS=67,
    )

    assert defaults.governance_agent_timeout_seconds == 300
    assert defaults.dspy_output_formatter_timeout_seconds == 300
    assert defaults.agent_test_run_timeout_seconds == 1800
    assert defaults.hitl_timeout_seconds == 300
    assert inherited_formatter.dspy_output_formatter_timeout_seconds == 123
    assert overridden_formatter.governance_agent_timeout_seconds == 123
    assert overridden_formatter.dspy_output_formatter_timeout_seconds == 45
    assert overridden_formatter.hitl_timeout_seconds == 67


def test_validate_hitl_single_api_process_rejects_multi_worker_env():
    enabled = AppSettings(_env_file=None, ENABLE_CLAUDE_WEB_HITL=True)
    disabled = AppSettings(_env_file=None, ENABLE_CLAUDE_WEB_HITL=False)

    validate_hitl_single_api_process(enabled, env={"WEB_CONCURRENCY": "1"})
    validate_hitl_single_api_process(disabled, env={"WEB_CONCURRENCY": "4"})
    with pytest.raises(RuntimeError, match="single API process"):
        validate_hitl_single_api_process(enabled, env={"WEB_CONCURRENCY": "2"})
    with pytest.raises(RuntimeError, match="must be an integer"):
        validate_hitl_single_api_process(enabled, env={"API_WORKERS": "many"})


def test_get_settings_is_pure_and_does_not_create_runtime_dirs(tmp_path, monkeypatch):
    from app.runtime.settings import get_settings

    for key in _PROFILE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("DATA_DIR", str(runtime_root / "data"))
    get_settings.cache_clear()

    settings = get_settings()

    expected_dirs = (
        settings.data_dir,
        settings.default_workspace_dir,
        settings.governor_workspace_dir,
        settings.default_claude_root,
        settings.governor_claude_root,
        settings.claude_home,
        settings.agent_git_repository_dir,
        settings.agent_git_worktrees_dir,
        settings.agent_release_archives_dir,
    )
    assert all(not path.exists() for path in expected_dirs)

    get_settings.cache_clear()
