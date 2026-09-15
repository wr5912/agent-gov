"""AgentGov control plane 配置。

模型凭据只由独立 AgentScope Runtime 消费；AgentGov 仅持有 Runtime 内网地址和
治理数据，不直接调用模型 provider。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Optional, Self, TypedDict

from pydantic import Field, PrivateAttr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .agent_paths import business_agent_layout
from .json_types import JsonObject
from .protected_business_agents import DEFAULT_BUSINESS_AGENT_ID

RuntimeVolumeMode = Literal["container", "local-debug"]
LOCAL_DEBUG_RUNTIME_VOLUME_ROOT = Path("/tmp/local-debug-volume-agent-gov")
CONTAINER_RUNTIME_VOLUME_ROOT = Path.home() / "volume-agent-gov"
_SETTINGS_ENV_FILES = {
    "container": Path("docker/.env"),
    "local-debug": Path("docker/.env.local-debug"),
}
_CONTAINER_MARKER_ENV = "RUNTIME_CONTAINER"
_TRUTHY_CONTAINER_MARKERS = {"1", "true", "yes", "on", "container"}


@dataclass(frozen=True)
class SettingsEnvSelection:
    runtime_volume_mode: RuntimeVolumeMode
    env_file: Path


class RuntimeSettingsLogFields(TypedDict):
    log_level: str
    runtime_volume_mode: RuntimeVolumeMode
    settings_env_file: str | None
    settings_env_file_exists: bool | None
    api_host: str
    api_port: int
    api_mode: Literal["open", "drain", "acceptance"]
    data_dir: str
    governor_workspace_dir: str
    agentscope_runtime_url: str
    agentscope_model_name: str
    langfuse_base_url: str


def running_in_container(
    environ: Mapping[str, str] = os.environ,
    *,
    dockerenv_path: Path = Path("/.dockerenv"),
) -> bool:
    marker = environ.get(_CONTAINER_MARKER_ENV)
    if marker is not None:
        return marker.strip().lower() in _TRUTHY_CONTAINER_MARKERS
    return dockerenv_path.exists()


def settings_env_selection(environ: Mapping[str, str] = os.environ) -> SettingsEnvSelection:
    mode: RuntimeVolumeMode = "container" if running_in_container(environ) else "local-debug"
    return SettingsEnvSelection(mode, _SETTINGS_ENV_FILES[mode])


def settings_env_file_for_mode(mode: str | None = None) -> Path:
    if mode is None:
        return settings_env_selection().env_file
    normalized = mode.strip()
    if normalized not in _SETTINGS_ENV_FILES:
        raise ValueError(f"Unsupported RUNTIME_VOLUME_MODE={normalized!r}; expected container or local-debug")
    return _SETTINGS_ENV_FILES[normalized]


def _mode_for_env_file(value: object) -> RuntimeVolumeMode | None:
    candidates = tuple(value) if isinstance(value, (tuple, list)) else (value,)
    for candidate in reversed(candidates):
        if not isinstance(candidate, (str, Path)):
            continue
        path = Path(candidate)
        for mode, known in _SETTINGS_ENV_FILES.items():
            if path.name in {known.name, f"{known.name}.example"}:
                return mode
    return None


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", hide_input_in_errors=True)
    _settings_env_file: Path | None = PrivateAttr(default=None)

    def __init__(self, **values: Any) -> None:
        explicit = "_env_file" in values
        env_file = values.get("_env_file")
        if explicit:
            mode = _mode_for_env_file(env_file)
            if mode is not None:
                values.setdefault("RUNTIME_VOLUME_MODE", mode)
        else:
            selection = settings_env_selection()
            env_file = selection.env_file
            values["_env_file"] = env_file
            values.setdefault("RUNTIME_VOLUME_MODE", selection.runtime_volume_mode)
        super().__init__(**values)
        self._settings_env_file = Path(env_file) if isinstance(env_file, (str, Path)) else None

    api_host: str = Field(default="0.0.0.0", alias="API_HOST")
    api_port: int = Field(default=8080, alias="API_PORT")
    host_port: int = Field(default=50400, alias="HOST_PORT")
    log_level: str = Field(default="info", alias="LOG_LEVEL")
    runtime_volume_mode: RuntimeVolumeMode = Field(default="container", alias="RUNTIME_VOLUME_MODE")
    host_runtime_volume_root: str = Field(default=str(CONTAINER_RUNTIME_VOLUME_ROOT), alias="HOST_RUNTIME_VOLUME_ROOT")
    host_data_mount: str = Field(default=str(CONTAINER_RUNTIME_VOLUME_ROOT / "data"), alias="HOST_DATA_MOUNT")
    host_governor_workspace_mount: str = Field(
        default=str(CONTAINER_RUNTIME_VOLUME_ROOT / "governor-workspace"),
        alias="HOST_GOVERNOR_WORKSPACE_MOUNT",
    )
    data_dir: Path = Field(default=Path("/data"), alias="DATA_DIR")
    governor_workspace_dir: Path = Field(default=Path("/governor-workspace"), alias="GOVERNOR_WORKSPACE_DIR")
    runtime_candidates_dir: Path = Field(default=Path("/candidate-workspaces"), alias="RUNTIME_CANDIDATES_DIR")

    api_key: Optional[str] = Field(default=None, alias="API_KEY")
    api_mode: Literal["open", "drain", "acceptance"] = Field(default="open", alias="AGENTGOV_API_MODE")
    acceptance_identity: Optional[str] = Field(default=None, alias="AGENTGOV_ACCEPTANCE_IDENTITY")
    acceptance_api_key: Optional[str] = Field(default=None, alias="AGENTGOV_ACCEPTANCE_API_KEY")
    api_gate_state_file: Optional[Path] = Field(default=None, alias="AGENTGOV_API_GATE_STATE_FILE")
    agentscope_runtime_url: str = Field(default="http://agentscope-runtime:8090", alias="AGENTSCOPE_RUNTIME_URL")
    agentscope_runtime_user_id: str = Field(default="agentgov-runtime", alias="AGENTSCOPE_RUNTIME_USER_ID")
    runtime_shared_secret: str = Field(
        min_length=16,
        alias="AGENTGOV_RUNTIME_SHARED_SECRET",
        repr=False,
    )
    agentscope_model_type: str = Field(default="openai_credential", alias="AGENTSCOPE_MODEL_TYPE")
    agentscope_credential_id: str = Field(default="agentgov-runtime-provider", alias="AGENTSCOPE_CREDENTIAL_ID")
    agentscope_model_name: str = Field(default="deepseek-chat", alias="AGENTSCOPE_MODEL_NAME")
    agentscope_model_parameters_json: str = Field(default="{}", alias="AGENTSCOPE_MODEL_PARAMETERS_JSON")
    runtime_request_timeout_seconds: float = Field(default=30.0, gt=0, le=300, alias="RUNTIME_REQUEST_TIMEOUT_SECONDS")

    governance_agent_timeout_seconds: int = Field(default=300, ge=1, le=3600, alias="GOVERNANCE_AGENT_TIMEOUT_SECONDS")
    agent_test_run_timeout_seconds: int = Field(default=1800, ge=1, le=86400, alias="AGENT_TEST_RUN_TIMEOUT_SECONDS")
    enable_feedback_debug_evidence: bool = Field(default=False, alias="ENABLE_FEEDBACK_DEBUG_EVIDENCE")

    agent_git_service_provider: Literal["local", "gitea"] = Field(default="local", alias="AGENT_GIT_SERVICE_PROVIDER")
    agent_git_service_url: Optional[str] = Field(default=None, alias="AGENT_GIT_SERVICE_URL")
    agent_git_service_public_url: Optional[str] = Field(default=None, alias="AGENT_GIT_SERVICE_PUBLIC_URL")
    agent_git_repository_name: str = Field(default=f"{DEFAULT_BUSINESS_AGENT_ID}-config", alias="AGENT_GIT_REPOSITORY_NAME")
    agent_git_repository_dir_override: Optional[Path] = Field(default=None, alias="AGENT_GIT_REPOSITORY_DIR")
    agent_git_worktrees_dir_override: Optional[Path] = Field(default=None, alias="AGENT_GIT_WORKTREES_DIR")
    agent_release_archives_dir_override: Optional[Path] = Field(default=None, alias="AGENT_RELEASE_ARCHIVES_DIR")
    agent_git_user_name: str = Field(default="AgentGov", alias="AGENT_GIT_USER_NAME")
    agent_git_user_email: str = Field(default="agent-runtime@example.local", alias="AGENT_GIT_USER_EMAIL")

    langfuse_enabled: bool = Field(default=False, alias="LANGFUSE_ENABLED")
    langfuse_public_key: Optional[str] = Field(default=None, alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: Optional[str] = Field(default=None, alias="LANGFUSE_SECRET_KEY")
    langfuse_base_url: str = Field(default="http://langfuse-web:3000", alias="LANGFUSE_BASE_URL")

    @field_validator("runtime_shared_secret")
    @classmethod
    def _require_private_shared_secret(cls, value: str) -> str:
        if not value.strip() or value in {"replace-with-at-least-32-random-characters", "local-dev-insecure-change-me-32"}:
            raise ValueError("AGENTGOV_RUNTIME_SHARED_SECRET 必须先在所选私有 env 中初始化")
        return value

    @field_validator(
        "agent_git_repository_dir_override",
        "agent_git_worktrees_dir_override",
        "agent_release_archives_dir_override",
        "api_gate_state_file",
        mode="before",
    )
    @classmethod
    def _blank_optional_path(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("api_key", "acceptance_identity", "acceptance_api_key", mode="before")
    @classmethod
    def _blank_optional_string(cls, value: object) -> object:
        return value.strip() or None if isinstance(value, str) else value

    @field_validator("agentscope_model_parameters_json")
    @classmethod
    def _model_parameters_are_object(cls, value: str) -> str:
        loaded = json.loads(value)
        if not isinstance(loaded, dict):
            raise ValueError("AGENTSCOPE_MODEL_PARAMETERS_JSON must be a JSON object")
        return value

    @model_validator(mode="after")
    def _acceptance_mode_requires_one_time_identity(self) -> Self:
        if self.api_mode == "acceptance" and (not self.acceptance_identity or not self.acceptance_api_key):
            raise ValueError("AGENTGOV_API_MODE=acceptance requires one-time acceptance identity and API key")
        return self

    @property
    def settings_env_file(self) -> Path | None:
        return self._settings_env_file

    @property
    def default_workspace_dir(self) -> Path:
        return business_agent_layout(self.data_dir, DEFAULT_BUSINESS_AGENT_ID).workspace

    @property
    def workspace_dir(self) -> Path:
        return self.default_workspace_dir

    @property
    def agentscope_model_parameters(self) -> JsonObject:
        return dict(json.loads(self.agentscope_model_parameters_json))

    @property
    def agent_git_repository_dir(self) -> Path:
        return self.agent_git_repository_dir_override or self.default_workspace_dir

    @property
    def agent_git_worktrees_dir(self) -> Path:
        return self.agent_git_worktrees_dir_override or business_agent_layout(self.data_dir, DEFAULT_BUSINESS_AGENT_ID).version_base / "worktrees"

    @property
    def agent_release_archives_dir(self) -> Path:
        return self.agent_release_archives_dir_override or business_agent_layout(self.data_dir, DEFAULT_BUSINESS_AGENT_ID).version_base / "releases"

    @property
    def runtime_db_path(self) -> Path:
        return self.data_dir / "runtime.sqlite3"


@lru_cache
def get_settings() -> AppSettings:
    return AppSettings()


def runtime_settings_log_fields(settings: AppSettings) -> RuntimeSettingsLogFields:
    env_file = settings.settings_env_file
    return {
        "log_level": settings.log_level,
        "runtime_volume_mode": settings.runtime_volume_mode,
        "settings_env_file": env_file.as_posix() if env_file else None,
        "settings_env_file_exists": env_file.exists() if env_file else None,
        "api_host": settings.api_host,
        "api_port": settings.api_port,
        "api_mode": settings.api_mode,
        "data_dir": settings.data_dir.as_posix(),
        "governor_workspace_dir": settings.governor_workspace_dir.as_posix(),
        "agentscope_runtime_url": settings.agentscope_runtime_url,
        "agentscope_model_name": settings.agentscope_model_name,
        "langfuse_base_url": settings.langfuse_base_url,
    }


def runtime_settings_log_message(settings: AppSettings) -> str:
    fields = runtime_settings_log_fields(settings)
    return "runtime settings configured " + " ".join(f"{key}={value}" for key, value in fields.items())
