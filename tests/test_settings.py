from __future__ import annotations

from pathlib import Path

import pytest
from app.runtime.protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from app.runtime.settings import (
    AppSettings,
    runtime_settings_log_fields,
    runtime_settings_log_message,
    settings_env_file_for_mode,
)

_PROFILE_ENV_KEYS = (
    "API_KEY",
    "AGENTGOV_API_MODE",
    "AGENTGOV_ACCEPTANCE_IDENTITY",
    "AGENTGOV_ACCEPTANCE_API_KEY",
    "AGENTGOV_API_GATE_STATE_FILE",
    "API_PORT",
    "LOG_LEVEL",
    "RUNTIME_VOLUME_MODE",
    "RUNTIME_CONTAINER",
    "HOST_RUNTIME_VOLUME_ROOT",
    "HOST_GOVERNOR_WORKSPACE_MOUNT",
    "HOST_DATA_MOUNT",
    "GOVERNOR_WORKSPACE_DIR",
    "DATA_DIR",
    "AGENTSCOPE_RUNTIME_URL",
    "AGENTGOV_RUNTIME_SHARED_SECRET",
    "AGENTSCOPE_MODEL_NAME",
    "AGENTSCOPE_MODEL_PARAMETERS_JSON",
    "LANGFUSE_BASE_URL",
)


def _clear_profile_env(process_environment) -> None:
    for key in _PROFILE_ENV_KEYS:
        process_environment.remove(key)


def test_settings_exposes_only_control_plane_credentials() -> None:
    settings = AppSettings(
        _env_file=None,
        API_KEY="  general-secret  ",
        AGENTGOV_RUNTIME_SHARED_SECRET="runtime-secret-with-safe-length",
    )

    assert settings.api_key == "general-secret"
    assert settings.runtime_shared_secret == "runtime-secret-with-safe-length"
    assert not hasattr(settings, "anthropic_api_key")
    assert not hasattr(settings, "model_provider_api_key")


def test_settings_selects_container_env_file_when_container_marker_is_set(tmp_path, process_environment) -> None:
    _clear_profile_env(process_environment)
    process_environment.set("RUNTIME_CONTAINER", "1")
    process_environment.chdir(tmp_path)
    docker_dir = tmp_path / "docker"
    docker_dir.mkdir()
    (docker_dir / ".env").write_text(
        "API_PORT=58080\nGOVERNOR_WORKSPACE_DIR=/governor-workspace\nDATA_DIR=/data\n",
        encoding="utf-8",
    )
    (docker_dir / ".env.local-debug").write_text("API_PORT=9090\n", encoding="utf-8")

    settings = AppSettings()

    assert settings_env_file_for_mode() == Path("docker/.env")
    assert settings.runtime_volume_mode == "container"
    assert settings.api_port == 58080
    assert settings.default_workspace_dir == Path(f"/data/business-agents/{DEFAULT_BUSINESS_AGENT_ID}/workspace")
    assert settings.governor_workspace_dir == Path("/governor-workspace")


def test_settings_selects_local_debug_env_file_for_host_runtime(tmp_path, process_environment) -> None:
    _clear_profile_env(process_environment)
    process_environment.set("RUNTIME_CONTAINER", "0")
    process_environment.chdir(tmp_path)
    docker_dir = tmp_path / "docker"
    docker_dir.mkdir()
    (docker_dir / ".env").write_text("API_PORT=58080\n", encoding="utf-8")
    (docker_dir / ".env.local-debug").write_text(
        "RUNTIME_VOLUME_MODE=local-debug\nAPI_PORT=8080\nDATA_DIR=/tmp/test-agentgov/data\n",
        encoding="utf-8",
    )

    settings = AppSettings()

    assert settings_env_file_for_mode() == Path("docker/.env.local-debug")
    assert settings.runtime_volume_mode == "local-debug"
    assert settings.api_port == 8080
    assert settings.data_dir == Path("/tmp/test-agentgov/data")


def test_explicit_env_file_name_selects_its_runtime_mode(tmp_path, process_environment) -> None:
    _clear_profile_env(process_environment)
    env_file = tmp_path / ".env.local-debug.example"
    env_file.write_text(
        "HOST_RUNTIME_VOLUME_ROOT=/tmp/local-agentgov\n"
        "HOST_DATA_MOUNT=/tmp/local-agentgov/data\n"
        "HOST_GOVERNOR_WORKSPACE_MOUNT=/tmp/local-agentgov/governor-workspace\n"
        "DATA_DIR=/tmp/local-agentgov/data\n"
        "GOVERNOR_WORKSPACE_DIR=/tmp/local-agentgov/governor-workspace\n",
        encoding="utf-8",
    )

    settings = AppSettings(_env_file=env_file)

    assert settings.runtime_volume_mode == "local-debug"
    assert settings.settings_env_file == env_file
    assert settings.default_workspace_dir == Path(f"/tmp/local-agentgov/data/business-agents/{DEFAULT_BUSINESS_AGENT_ID}/workspace")


def test_runtime_settings_log_fields_are_explicit_and_non_secret(tmp_path) -> None:
    env_file = tmp_path / ".env.local-debug"
    env_file.write_text("", encoding="utf-8")
    settings = AppSettings(
        _env_file=env_file,
        DATA_DIR=tmp_path / "data",
        GOVERNOR_WORKSPACE_DIR=tmp_path / "governor",
        AGENTSCOPE_RUNTIME_URL="http://runtime.internal:8090",
        AGENTGOV_RUNTIME_SHARED_SECRET="must-never-appear-in-log",
        AGENTSCOPE_MODEL_NAME="governed-model",
        LANGFUSE_SECRET_KEY="also-must-not-appear",
    )

    fields = runtime_settings_log_fields(settings)
    message = runtime_settings_log_message(settings)

    assert fields == {
        "log_level": "info",
        "runtime_volume_mode": "local-debug",
        "settings_env_file": env_file.as_posix(),
        "settings_env_file_exists": True,
        "api_host": "0.0.0.0",
        "api_port": 8080,
        "api_mode": "open",
        "data_dir": (tmp_path / "data").as_posix(),
        "governor_workspace_dir": (tmp_path / "governor").as_posix(),
        "agentscope_runtime_url": "http://runtime.internal:8090",
        "agentscope_model_name": "governed-model",
        "langfuse_base_url": "http://langfuse-web:3000",
    }
    assert "must-never-appear" not in message
    assert "also-must-not-appear" not in message


def test_agentscope_model_parameters_require_a_json_object() -> None:
    settings = AppSettings(
        _env_file=None,
        AGENTSCOPE_MODEL_PARAMETERS_JSON='{"temperature":0.2,"stream":true}',
    )
    assert settings.agentscope_model_parameters == {"temperature": 0.2, "stream": True}

    with pytest.raises(ValueError, match="AGENTSCOPE_MODEL_PARAMETERS_JSON"):
        AppSettings(_env_file=None, AGENTSCOPE_MODEL_PARAMETERS_JSON="[]")
    with pytest.raises(ValueError):
        AppSettings(_env_file=None, AGENTSCOPE_MODEL_PARAMETERS_JSON="{")


def test_api_mode_is_closed_for_unknown_or_incomplete_acceptance_identity() -> None:
    with pytest.raises(ValueError, match="AGENTGOV_API_MODE"):
        AppSettings(_env_file=None, AGENTGOV_API_MODE="unknown")
    with pytest.raises(ValueError, match="one-time acceptance identity"):
        AppSettings(_env_file=None, AGENTGOV_API_MODE="acceptance")

    settings = AppSettings(
        _env_file=None,
        AGENTGOV_API_MODE="acceptance",
        AGENTGOV_ACCEPTANCE_IDENTITY="cutover-one",
        AGENTGOV_ACCEPTANCE_API_KEY="one-time-key",
    )
    assert settings.api_mode == "acceptance"


def test_runtime_and_governance_timeouts_are_bounded() -> None:
    defaults = AppSettings(_env_file=None)
    overridden = AppSettings(
        _env_file=None,
        RUNTIME_REQUEST_TIMEOUT_SECONDS=45,
        GOVERNANCE_AGENT_TIMEOUT_SECONDS=123,
        AGENT_TEST_RUN_TIMEOUT_SECONDS=456,
    )

    assert defaults.runtime_request_timeout_seconds == 30
    assert defaults.governance_agent_timeout_seconds == 300
    assert defaults.agent_test_run_timeout_seconds == 1800
    assert overridden.runtime_request_timeout_seconds == 45
    assert overridden.governance_agent_timeout_seconds == 123
    assert overridden.agent_test_run_timeout_seconds == 456
    with pytest.raises(ValueError):
        AppSettings(_env_file=None, RUNTIME_REQUEST_TIMEOUT_SECONDS=0)


def test_get_settings_is_pure_and_does_not_create_runtime_dirs(tmp_path, process_environment) -> None:
    from app.runtime.settings import get_settings

    _clear_profile_env(process_environment)
    process_environment.chdir(tmp_path)
    runtime_root = tmp_path / "runtime"
    process_environment.set("DATA_DIR", str(runtime_root / "data"))
    process_environment.set("GOVERNOR_WORKSPACE_DIR", str(runtime_root / "governor"))
    get_settings.cache_clear()

    settings = get_settings()

    expected_dirs = (
        settings.data_dir,
        settings.default_workspace_dir,
        settings.governor_workspace_dir,
        settings.runtime_candidates_dir,
        settings.agent_git_repository_dir,
        settings.agent_git_worktrees_dir,
        settings.agent_release_archives_dir,
    )
    assert all(not path.exists() for path in expected_dirs)
    get_settings.cache_clear()
