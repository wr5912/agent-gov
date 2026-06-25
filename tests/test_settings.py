from pathlib import Path

from app.runtime.settings import AppSettings, runtime_settings_log_fields, settings_env_file_for_mode

REPO_ROOT = Path(__file__).resolve().parents[1]

_PROFILE_ENV_KEYS = (
    "API_PORT",
    "LOG_LEVEL",
    "RUNTIME_VOLUME_MODE",
    "RUNTIME_CONTAINER",
    "HOST_RUNTIME_VOLUME_ROOT",
    "HOST_WORKSPACE_MOUNT",
    "HOST_GOVERNOR_WORKSPACE_MOUNT",
    "HOST_DATA_MOUNT",
    "HOST_CLAUDE_ROOT_MOUNT",
    "HOST_GOVERNOR_CLAUDE_ROOT_MOUNT",
    "WORKSPACE_DIR",
    "MAIN_WORKSPACE_DIR",
    "GOVERNOR_WORKSPACE_DIR",
    "DATA_DIR",
    "CLAUDE_ROOT",
    "MAIN_CLAUDE_ROOT",
    "GOVERNOR_CLAUDE_ROOT",
    "CLAUDE_HOME",
    "LANGFUSE_BASE_URL",
)


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
                "WORKSPACE_DIR=/main-workspace",
                "MAIN_WORKSPACE_DIR=/main-workspace",
                "GOVERNOR_WORKSPACE_DIR=/governor-workspace",
                "DATA_DIR=/data",
                "CLAUDE_ROOT=/claude-roots/main",
                "MAIN_CLAUDE_ROOT=/claude-roots/main",
                "CLAUDE_HOME=/claude-roots/main/.claude",
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
                "WORKSPACE_DIR=${HOST_RUNTIME_VOLUME_ROOT}/main-workspace",
                "DATA_DIR=${HOST_RUNTIME_VOLUME_ROOT}/data",
                "CLAUDE_ROOT=${HOST_RUNTIME_VOLUME_ROOT}/claude-roots/main",
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
    assert settings.workspace_dir == Path("/main-workspace")
    assert settings.main_workspace_dir == settings.workspace_dir
    assert settings.governor_workspace_dir == Path("/governor-workspace")
    assert settings.data_dir == Path("/data")
    assert settings.claude_root == Path("/claude-roots/main")
    assert settings.main_claude_root == settings.claude_root
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
                "WORKSPACE_DIR=/main-workspace",
                "DATA_DIR=/data",
                "CLAUDE_ROOT=/claude-roots/main",
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
                "WORKSPACE_DIR=${HOST_RUNTIME_VOLUME_ROOT}/main-workspace",
                "DATA_DIR=${HOST_RUNTIME_VOLUME_ROOT}/data",
                "CLAUDE_ROOT=${HOST_RUNTIME_VOLUME_ROOT}/claude-roots/main",
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
    assert settings.workspace_dir == Path("/tmp/local-debug-volume-agent-gov/main-workspace")
    assert settings.data_dir == Path("/tmp/local-debug-volume-agent-gov/data")
    assert settings.claude_root == Path("/tmp/local-debug-volume-agent-gov/claude-roots/main")
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
    assert settings.workspace_dir == local_debug_root / "main-workspace"
    assert settings.main_workspace_dir == local_debug_root / "main-workspace"
    assert settings.governor_workspace_dir == local_debug_root / "governor-workspace"
    assert settings.data_dir == local_debug_root / "data"
    assert settings.claude_root == local_debug_root / "claude-roots" / "main"
    assert settings.main_claude_root == local_debug_root / "claude-roots" / "main"
    assert settings.claude_home == local_debug_root / "claude-roots" / "main" / ".claude"
    assert settings.agent_git_worktrees_dir == local_debug_root / "data" / "agent-governance" / "worktrees"
    assert settings.agent_release_archives_dir == local_debug_root / "data" / "agent-governance" / "releases"


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
        "api_host": "0.0.0.0",
        "api_port": 8080,
        "workspace_dir": "/tmp/local-debug-volume-agent-gov/main-workspace",
        "data_dir": "/tmp/local-debug-volume-agent-gov/data",
        "claude_root": "/tmp/local-debug-volume-agent-gov/claude-roots/main",
        "langfuse_base_url": "http://localhost:53000",
    }
    assert fields["provider_api_key_configured"] is False
    assert not any("secret" in name.lower() for name in fields)


def test_get_settings_creates_all_profile_dirs(tmp_path, monkeypatch):
    from app.runtime.settings import get_settings

    for key in _PROFILE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("WORKSPACE_DIR", str(runtime_root / "main-workspace"))
    monkeypatch.setenv("DATA_DIR", str(runtime_root / "data"))
    monkeypatch.setenv("CLAUDE_ROOT", str(runtime_root / "claude-roots" / "main"))
    get_settings.cache_clear()

    settings = get_settings()

    expected_dirs = (
        settings.data_dir,
        settings.main_workspace_dir,
        settings.governor_workspace_dir,
        settings.main_claude_root,
        settings.governor_claude_root,
        settings.claude_home,
        settings.agent_git_repository_dir,
        settings.agent_git_worktrees_dir,
        settings.agent_release_archives_dir,
    )
    assert all(path.is_dir() for path in expected_dirs)

    get_settings.cache_clear()
