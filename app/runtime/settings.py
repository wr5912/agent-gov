import base64
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Optional, TypedDict, cast

from pydantic import Field, PrivateAttr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .agent_job_errors import provider_api_key_configured
from .agent_paths import business_agent_layout
from .json_types import JsonObject
from .model_provider import ModelProviderBackend
from .protected_business_agents import DEFAULT_BUSINESS_AGENT_ID

RuntimeVolumeMode = Literal["container", "local-debug"]


def _csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _json_object(value: str | None) -> JsonObject:
    if not value:
        return {}
    loaded = json.loads(value)
    if not isinstance(loaded, dict):
        raise ValueError("Expected a JSON object")
    return cast(JsonObject, loaded)


def _string_dict(value: JsonObject) -> dict[str, str]:
    return {str(key): str(val) for key, val in value.items()}


_DEFAULT_GOVERNOR_WORKSPACE_DIR = Path("/governor-workspace")
_DEFAULT_GOVERNOR_CLAUDE_ROOT = Path("/claude-roots/governor")
LOCAL_DEBUG_RUNTIME_VOLUME_ROOT = Path("/tmp/local-debug-volume-agent-gov")
CONTAINER_RUNTIME_VOLUME_ROOT = Path.home() / "volume-agent-gov"
_SETTINGS_ENV_FILES = {
    "container": Path("docker/.env"),
    "local-debug": Path("docker/.env.local-debug"),
}
_CONTAINER_MARKER_ENV = "RUNTIME_CONTAINER"
_TRUTHY_CONTAINER_MARKERS = {"1", "true", "yes", "on", "container"}
_API_WORKER_ENV_KEYS = ("WEB_CONCURRENCY", "API_WORKERS", "UVICORN_WORKERS")


@dataclass(frozen=True)
class SettingsEnvSelection:
    runtime_volume_mode: RuntimeVolumeMode
    env_file: Path


class RuntimeSettingsLogFields(TypedDict):
    log_level: str
    runtime_volume_mode: RuntimeVolumeMode
    settings_env_file: str | None
    settings_env_file_exists: bool | None
    model_provider_backend: ModelProviderBackend
    model_provider_vllm_sidecar_threshold: str
    model_provider_vllm_allow_direct: bool
    provider_api_key_configured: bool
    provider_api_url_configured: bool
    governance_agent_timeout_seconds: int
    dspy_output_formatter_timeout_seconds: int
    agent_test_run_timeout_seconds: int
    prompt_suggestion_source: Literal["backend", "claude_native"]
    claude_web_hitl_enabled: bool
    hitl_timeout_seconds: int
    api_host: str
    api_port: int
    workspace_dir: str
    data_dir: str
    claude_root: str
    langfuse_base_url: str


def _derive_profile_dirs(settings: Any, explicit_env: Mapping[str, str] = os.environ) -> None:
    """governor 顶层目录随运行卷根（data_dir 的父目录）派生，使本机调试/容器两模式一致；
    默认业务 Agent 的 workspace/claude-root 由 business_agent_layout 在 /data 下派生。"""
    volume_root = settings.data_dir.parent
    if "GOVERNOR_WORKSPACE_DIR" not in explicit_env and settings.governor_workspace_dir == _DEFAULT_GOVERNOR_WORKSPACE_DIR:
        settings.governor_workspace_dir = volume_root / "governor-workspace"
    if "GOVERNOR_CLAUDE_ROOT" not in explicit_env and settings.governor_claude_root == _DEFAULT_GOVERNOR_CLAUDE_ROOT:
        settings.governor_claude_root = volume_root / "claude-roots" / "governor"


def running_in_container(environ: Mapping[str, str] = os.environ, *, dockerenv_path: Path = Path("/.dockerenv")) -> bool:
    marker = environ.get(_CONTAINER_MARKER_ENV)
    if marker is not None:
        return marker.strip().lower() in _TRUTHY_CONTAINER_MARKERS
    return dockerenv_path.exists()


def settings_env_selection(environ: Mapping[str, str] = os.environ) -> SettingsEnvSelection:
    mode: RuntimeVolumeMode = "container" if running_in_container(environ) else "local-debug"
    return SettingsEnvSelection(runtime_volume_mode=mode, env_file=_SETTINGS_ENV_FILES[mode])


def settings_env_file_for_mode(mode: str | None = None) -> Path:
    if mode is None:
        return settings_env_selection().env_file
    normalized = mode.strip()
    env_file = _SETTINGS_ENV_FILES.get(normalized)
    if env_file is None:
        raise ValueError(f"Unsupported RUNTIME_VOLUME_MODE={normalized!r}; expected container or local-debug")
    return env_file


def _runtime_volume_mode_for_env_file(value: object) -> RuntimeVolumeMode | None:
    if value is None:
        return None
    candidates: tuple[object, ...]
    if isinstance(value, (tuple, list)):
        candidates = tuple(value)
    else:
        candidates = (value,)
    for candidate in reversed(candidates):
        path = Path(candidate) if isinstance(candidate, (str, Path)) else None
        if path is None:
            continue
        for mode, env_file in _SETTINGS_ENV_FILES.items():
            if path.name in {env_file.name, f"{env_file.name}.example"}:
                return cast(RuntimeVolumeMode, mode)
    return None


class AppSettings(BaseSettings):
    """Runtime settings loaded from environment variables."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore")
    _settings_env_file: Path | None = PrivateAttr(default=None)

    def __init__(self, **values: Any) -> None:
        env_file_was_explicit = "_env_file" in values
        env_file = values.get("_env_file")
        if env_file_was_explicit:
            mode = _runtime_volume_mode_for_env_file(env_file)
            if mode is not None:
                values.setdefault("RUNTIME_VOLUME_MODE", mode)
        else:
            selection = settings_env_selection()
            env_file = selection.env_file
            values["_env_file"] = selection.env_file
            values.setdefault("RUNTIME_VOLUME_MODE", selection.runtime_volume_mode)
        super().__init__(**values)
        self._settings_env_file = Path(env_file) if isinstance(env_file, (str, Path)) else None

    api_host: str = Field(default="0.0.0.0", alias="API_HOST")
    api_port: int = Field(default=8080, alias="API_PORT")
    host_port: int = Field(default=58080, alias="HOST_PORT")
    log_level: str = Field(default="info", alias="LOG_LEVEL")
    runtime_volume_mode: Literal["container", "local-debug"] = Field(default="container", alias="RUNTIME_VOLUME_MODE")
    host_runtime_volume_root: str = Field(default=str(CONTAINER_RUNTIME_VOLUME_ROOT), alias="HOST_RUNTIME_VOLUME_ROOT")
    host_data_mount: str = Field(default=str(CONTAINER_RUNTIME_VOLUME_ROOT / "data"), alias="HOST_DATA_MOUNT")
    host_governor_workspace_mount: str = Field(default=str(CONTAINER_RUNTIME_VOLUME_ROOT / "governor-workspace"), alias="HOST_GOVERNOR_WORKSPACE_MOUNT")
    host_governor_claude_root_mount: str = Field(
        default=str(CONTAINER_RUNTIME_VOLUME_ROOT / "claude-roots" / "governor"), alias="HOST_GOVERNOR_CLAUDE_ROOT_MOUNT"
    )

    governor_workspace_dir: Path = Field(default=Path("/governor-workspace"), alias="GOVERNOR_WORKSPACE_DIR")
    data_dir: Path = Field(default=Path("/data"), alias="DATA_DIR")
    governor_claude_root: Path = Field(default=Path("/claude-roots/governor"), alias="GOVERNOR_CLAUDE_ROOT")
    claude_config_dir: Optional[Path] = Field(default=None, alias="CLAUDE_CONFIG_DIR")

    anthropic_api_key: Optional[str] = Field(default=None, alias="ANTHROPIC_API_KEY")
    anthropic_base_url: Optional[str] = Field(default=None, alias="ANTHROPIC_BASE_URL")
    model_provider_api_key: Optional[str] = Field(default=None, alias="MODEL_PROVIDER_API_KEY")
    model_provider_api_url: Optional[str] = Field(default=None, alias="MODEL_PROVIDER_API_URL")
    model_provider_backend: ModelProviderBackend = Field(default="anthropic_compatible", alias="MODEL_PROVIDER_BACKEND")
    model_provider_vllm_sidecar_threshold: str = Field(default="0.23.0", alias="MODEL_PROVIDER_VLLM_SIDECAR_THRESHOLD")
    model_provider_vllm_allow_direct: bool = Field(default=False, alias="MODEL_PROVIDER_VLLM_ALLOW_DIRECT")
    model_provider_probe_timeout_seconds: float = Field(default=30.0, alias="MODEL_PROVIDER_PROBE_TIMEOUT_SECONDS")
    model_provider_warning_ttl_seconds: int = Field(default=300, alias="MODEL_PROVIDER_WARNING_TTL_SECONDS")
    api_key: Optional[str] = Field(default=None, alias="API_KEY")

    # 会话历史读取端点 GET /api/sessions/{id}/messages 默认返回完整对话正文（会话所有者回放自己的历史）；
    # 仅当该开关打开时对返回的 text/thinking/tool 输入与结果做脱敏。
    session_history_scrub: bool = Field(default=False, alias="SESSION_HISTORY_SCRUB")

    agent_model: Optional[str] = Field(default="claude-sonnet-4-5", alias="AGENT_MODEL")
    fallback_model: Optional[str] = Field(default=None, alias="FALLBACK_MODEL")
    claude_system_append: Optional[str] = Field(default=None, alias="CLAUDE_SYSTEM_APPEND")

    enable_sdk_session_resume: bool = Field(default=True, alias="ENABLE_SDK_SESSION_RESUME")
    enable_claude_web_hitl: bool = Field(default=False, alias="ENABLE_CLAUDE_WEB_HITL")
    governance_agent_timeout_seconds: int = Field(default=300, alias="GOVERNANCE_AGENT_TIMEOUT_SECONDS")
    agent_test_run_timeout_seconds: int = Field(default=1800, ge=1, le=86400, alias="AGENT_TEST_RUN_TIMEOUT_SECONDS")
    hitl_timeout_seconds: int = Field(default=300, alias="HITL_TIMEOUT_SECONDS")
    enable_feedback_debug_evidence: bool = Field(default=True, alias="ENABLE_FEEDBACK_DEBUG_EVIDENCE")
    enable_dspy_output_formatter: bool = Field(default=True, alias="ENABLE_DSPY_OUTPUT_FORMATTER")
    dspy_output_formatter_model: Optional[str] = Field(default=None, alias="DSPY_OUTPUT_FORMATTER_MODEL")
    dspy_output_formatter_timeout_seconds_override: Optional[int] = Field(default=None, alias="DSPY_OUTPUT_FORMATTER_TIMEOUT_SECONDS")
    dspy_output_formatter_max_retries: int = Field(default=1, alias="DSPY_OUTPUT_FORMATTER_MAX_RETRIES")
    dspy_output_formatter_max_tokens: int = Field(default=8192, alias="DSPY_OUTPUT_FORMATTER_MAX_TOKENS")
    # 后端直接生成 Prompt Suggestion —— **受控特例**(见 prompt_suggestion_generator.py):
    # Claude Code 原生 SUGGESTION MODE 在本部署(SOC Agent + deepseek)事实上失效,故后端
    # 对本轮对话做一次 LLM 派生。**默认关**(守常规原则),仅本部署经 docker/.env 显式开启。
    enable_backend_prompt_suggestion: bool = Field(default=False, alias="ENABLE_BACKEND_PROMPT_SUGGESTION")
    backend_prompt_suggestion_model: Optional[str] = Field(default=None, alias="BACKEND_PROMPT_SUGGESTION_MODEL")
    # 每轮至多给几条候选(「最多 N 条」语义:模型给不满就少给,绝不凑数)。
    # 刻意不用 ge/le:本模块信条是「任何失败都不得影响主 Run」,配置写错值不该崩启动;
    # 数量在 prompt_suggestion_generator._count() 使用点 clamp 到 1..5。
    backend_prompt_suggestion_count: int = Field(default=3, alias="BACKEND_PROMPT_SUGGESTION_COUNT")
    # 推理模型(如 deepseek-v4-flash)会先吐 reasoning_content 再吐正文,max_tokens 必须留够
    # 思考预算,否则思考吃光配额、正文为空(finish_reason=length)。
    backend_prompt_suggestion_max_tokens: int = Field(default=1024, alias="BACKEND_PROMPT_SUGGESTION_MAX_TOKENS")
    include_hook_events: bool = Field(default=True, alias="INCLUDE_HOOK_EVENTS")
    include_partial_messages: bool = Field(default=False, alias="INCLUDE_PARTIAL_MESSAGES")
    max_turns: int = Field(default=16, alias="MAX_TURNS")
    max_budget_usd: Optional[float] = Field(default=None, alias="MAX_BUDGET_USD")
    max_buffer_size: Optional[int] = Field(default=None, alias="MAX_BUFFER_SIZE")

    claude_cli_path: Optional[Path] = Field(default=None, alias="CLAUDE_CLI_PATH")
    claude_betas_raw: Optional[str] = Field(default=None, alias="CLAUDE_BETAS")
    claude_user: Optional[str] = Field(default=None, alias="CLAUDE_USER")
    max_thinking_tokens: Optional[int] = Field(default=None, alias="MAX_THINKING_TOKENS")
    effort: Optional[Literal["low", "medium", "high", "xhigh", "max"]] = Field(default=None, alias="EFFORT")
    enable_file_checkpointing: bool = Field(default=False, alias="ENABLE_FILE_CHECKPOINTING")
    session_store_flush: Literal["batched", "eager"] = Field(default="batched", alias="SESSION_STORE_FLUSH")
    load_timeout_ms: int = Field(default=60000, alias="LOAD_TIMEOUT_MS")

    claude_env_json: Optional[str] = Field(default=None, alias="CLAUDE_ENV_JSON")

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
    langfuse_otel_endpoint: Optional[str] = Field(default=None, alias="LANGFUSE_OTEL_ENDPOINT")
    langfuse_otel_signals_raw: str = Field(default="traces,metrics", alias="LANGFUSE_OTEL_SIGNALS")
    langfuse_service_name: str = Field(default="agent-gov-api", alias="LANGFUSE_SERVICE_NAME")
    langfuse_deployment_environment: str = Field(default="local", alias="LANGFUSE_DEPLOYMENT_ENVIRONMENT")
    langfuse_resource_attributes_raw: Optional[str] = Field(default=None, alias="LANGFUSE_RESOURCE_ATTRIBUTES")
    langfuse_export_interval_ms: int = Field(default=1000, alias="LANGFUSE_EXPORT_INTERVAL_MS")

    @field_validator(
        "claude_config_dir",
        "claude_cli_path",
        "agent_git_repository_dir_override",
        "agent_git_worktrees_dir_override",
        "agent_release_archives_dir_override",
        mode="before",
    )
    @classmethod
    def _blank_optional_path(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("api_key", mode="before")
    @classmethod
    def _blank_optional_api_key(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return value

    def model_post_init(self, __context: Any) -> None:
        _derive_profile_dirs(self)

    @property
    def settings_env_file(self) -> Path | None:
        return self._settings_env_file

    @property
    def default_workspace_dir(self) -> Path:
        return business_agent_layout(self.data_dir, DEFAULT_BUSINESS_AGENT_ID).workspace

    @property
    def default_claude_root(self) -> Path:
        return business_agent_layout(self.data_dir, DEFAULT_BUSINESS_AGENT_ID).claude_root

    @property
    def workspace_dir(self) -> Path:
        return self.default_workspace_dir

    @property
    def claude_root(self) -> Path:
        return self.default_claude_root

    @property
    def claude_home(self) -> Path:
        return self.default_claude_root / ".claude"

    @property
    def provider_api_key(self) -> Optional[str]:
        return self.model_provider_api_key or self.anthropic_api_key

    @property
    def provider_api_url(self) -> Optional[str]:
        return self.model_provider_api_url or self.anthropic_base_url

    @property
    def dspy_output_formatter_timeout_seconds(self) -> int:
        return self.dspy_output_formatter_timeout_seconds_override or self.governance_agent_timeout_seconds

    @property
    def resolved_claude_config_dir(self) -> Optional[Path]:
        return self.claude_config_dir

    @property
    def claude_config_mode(self) -> str:
        return "redirected" if self.claude_config_dir else "native"

    @property
    def claude_global_config_file(self) -> Path:
        if self.claude_config_dir:
            return self.claude_config_dir / ".claude.json"
        return self.claude_root / ".claude.json"

    @property
    def claude_projects_dir(self) -> Path:
        return (self.claude_config_dir or self.claude_home) / "projects"

    @property
    def claude_betas(self) -> list[str]:
        return _csv(self.claude_betas_raw)

    @property
    def setting_sources(self) -> list[str]:
        return ["project"]

    @property
    def claude_env(self) -> dict[str, str]:
        return _string_dict(_json_object(self.claude_env_json))

    @property
    def langfuse_otel_signals(self) -> list[str]:
        allowed = {"traces", "metrics", "logs"}
        return [signal for signal in _csv(self.langfuse_otel_signals_raw) if signal in allowed]

    @property
    def langfuse_effective_otel_endpoint(self) -> str:
        if self.langfuse_otel_endpoint:
            return self.langfuse_otel_endpoint
        return f"{self.langfuse_base_url.rstrip('/')}/api/public/otel"

    @property
    def langfuse_otel_headers(self) -> str:
        auth = base64.b64encode(f"{self.langfuse_public_key}:{self.langfuse_secret_key}".encode()).decode()
        return f"Authorization=Basic {auth},x-langfuse-ingestion-version=4"

    @property
    def langfuse_resource_attributes(self) -> str:
        parts = [f"deployment.environment={self.langfuse_deployment_environment}"]
        if self.langfuse_resource_attributes_raw:
            parts.append(self.langfuse_resource_attributes_raw.strip())
        return ",".join(part for part in parts if part)

    @property
    def session_dir(self) -> Path:
        return self.data_dir / "sessions"

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
    settings = AppSettings()
    _derive_profile_dirs(settings)
    return settings


def runtime_settings_log_fields(settings: AppSettings) -> RuntimeSettingsLogFields:
    env_file = settings.settings_env_file
    return {
        "log_level": settings.log_level,
        "runtime_volume_mode": settings.runtime_volume_mode,
        "settings_env_file": env_file.as_posix() if env_file else None,
        "settings_env_file_exists": env_file.exists() if env_file else None,
        "model_provider_backend": settings.model_provider_backend,
        "model_provider_vllm_sidecar_threshold": settings.model_provider_vllm_sidecar_threshold,
        "model_provider_vllm_allow_direct": settings.model_provider_vllm_allow_direct,
        "provider_api_key_configured": provider_api_key_configured(settings.provider_api_key),
        "provider_api_url_configured": bool(settings.provider_api_url),
        "governance_agent_timeout_seconds": settings.governance_agent_timeout_seconds,
        "dspy_output_formatter_timeout_seconds": settings.dspy_output_formatter_timeout_seconds,
        "agent_test_run_timeout_seconds": settings.agent_test_run_timeout_seconds,
        "prompt_suggestion_source": ("backend" if settings.enable_backend_prompt_suggestion else "claude_native"),
        "claude_web_hitl_enabled": settings.enable_claude_web_hitl,
        "hitl_timeout_seconds": settings.hitl_timeout_seconds,
        "api_host": settings.api_host,
        "api_port": settings.api_port,
        "workspace_dir": settings.workspace_dir.as_posix(),
        "data_dir": settings.data_dir.as_posix(),
        "claude_root": settings.claude_root.as_posix(),
        "langfuse_base_url": settings.langfuse_base_url,
    }


def runtime_settings_log_message(settings: AppSettings) -> str:
    fields = runtime_settings_log_fields(settings)
    return "runtime settings configured " + " ".join(f"{key}={value}" for key, value in fields.items())


def validate_hitl_single_api_process(settings: AppSettings, env: Mapping[str, str] | None = None) -> None:
    if not settings.enable_claude_web_hitl:
        return
    values = env or os.environ
    for key in _API_WORKER_ENV_KEYS:
        raw = values.get(key)
        if raw is None or not raw.strip():
            continue
        try:
            count = int(raw)
        except ValueError as exc:
            raise RuntimeError(f"{key} must be an integer when ENABLE_CLAUDE_WEB_HITL=true") from exc
        if count > 1:
            raise RuntimeError(f"ENABLE_CLAUDE_WEB_HITL=true requires a single API process; {key}={count} would break pending HITL decisions")
