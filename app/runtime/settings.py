import base64
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Mapping, Optional, cast

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .json_types import JsonObject


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


def _optional_string_dict(value: JsonObject) -> dict[str, str | None]:
    return {str(key): None if val is None else str(val) for key, val in value.items()}


_DEFAULT_MAIN_WORKSPACE_DIR = Path("/main-workspace")
_DEFAULT_MAIN_CLAUDE_ROOT = Path("/claude-roots/main")
_DEFAULT_ATTRIBUTION_WORKSPACE_DIR = Path("/attribution-analyzer-workspace")
_DEFAULT_PROPOSAL_WORKSPACE_DIR = Path("/proposal-generator-workspace")
_DEFAULT_EXECUTION_WORKSPACE_DIR = Path("/execution-optimizer-workspace")
_DEFAULT_EVAL_CASE_GOVERNOR_WORKSPACE_DIR = Path("/eval-case-governor-workspace")
_DEFAULT_REGRESSION_IMPACT_WORKSPACE_DIR = Path("/regression-impact-analyzer-workspace")
_DEFAULT_ATTRIBUTION_CLAUDE_ROOT = Path("/claude-roots/attribution-analyzer")
_DEFAULT_PROPOSAL_CLAUDE_ROOT = Path("/claude-roots/proposal-generator")
_DEFAULT_EXECUTION_CLAUDE_ROOT = Path("/claude-roots/execution-optimizer")
_DEFAULT_EVAL_CASE_GOVERNOR_CLAUDE_ROOT = Path("/claude-roots/eval-case-governor")
_DEFAULT_REGRESSION_IMPACT_CLAUDE_ROOT = Path("/claude-roots/regression-impact-analyzer")
_DEFAULT_CLAUDE_HOME = Path("/claude-roots/main/.claude")

_WORKSPACE_PROFILE_DIR_DEFAULTS = (
    ("ATTRIBUTION_ANALYZER_WORKSPACE_DIR", "attribution_analyzer_workspace_dir", _DEFAULT_ATTRIBUTION_WORKSPACE_DIR, "attribution-analyzer-workspace"),
    ("PROPOSAL_GENERATOR_WORKSPACE_DIR", "proposal_generator_workspace_dir", _DEFAULT_PROPOSAL_WORKSPACE_DIR, "proposal-generator-workspace"),
    ("EXECUTION_OPTIMIZER_WORKSPACE_DIR", "execution_optimizer_workspace_dir", _DEFAULT_EXECUTION_WORKSPACE_DIR, "execution-optimizer-workspace"),
    ("EVAL_CASE_GOVERNOR_WORKSPACE_DIR", "eval_case_governor_workspace_dir", _DEFAULT_EVAL_CASE_GOVERNOR_WORKSPACE_DIR, "eval-case-governor-workspace"),
    ("REGRESSION_IMPACT_ANALYZER_WORKSPACE_DIR", "regression_impact_analyzer_workspace_dir", _DEFAULT_REGRESSION_IMPACT_WORKSPACE_DIR, "regression-impact-analyzer-workspace"),
)
_CLAUDE_ROOT_PROFILE_DIR_DEFAULTS = (
    ("ATTRIBUTION_ANALYZER_CLAUDE_ROOT", "attribution_analyzer_claude_root", _DEFAULT_ATTRIBUTION_CLAUDE_ROOT, "attribution-analyzer"),
    ("PROPOSAL_GENERATOR_CLAUDE_ROOT", "proposal_generator_claude_root", _DEFAULT_PROPOSAL_CLAUDE_ROOT, "proposal-generator"),
    ("EXECUTION_OPTIMIZER_CLAUDE_ROOT", "execution_optimizer_claude_root", _DEFAULT_EXECUTION_CLAUDE_ROOT, "execution-optimizer"),
    ("EVAL_CASE_GOVERNOR_CLAUDE_ROOT", "eval_case_governor_claude_root", _DEFAULT_EVAL_CASE_GOVERNOR_CLAUDE_ROOT, "eval-case-governor"),
    ("REGRESSION_IMPACT_ANALYZER_CLAUDE_ROOT", "regression_impact_analyzer_claude_root", _DEFAULT_REGRESSION_IMPACT_CLAUDE_ROOT, "regression-impact-analyzer"),
)


def _derive_profile_dirs(settings: Any, explicit_env: Mapping[str, str] = os.environ) -> None:
    if (
        "MAIN_WORKSPACE_DIR" not in explicit_env
        and settings.main_workspace_dir == _DEFAULT_MAIN_WORKSPACE_DIR
        and settings.workspace_dir != _DEFAULT_MAIN_WORKSPACE_DIR
    ):
        settings.main_workspace_dir = settings.workspace_dir
    if (
        "MAIN_CLAUDE_ROOT" not in explicit_env
        and settings.main_claude_root == _DEFAULT_MAIN_CLAUDE_ROOT
        and settings.claude_root != _DEFAULT_MAIN_CLAUDE_ROOT
    ):
        settings.main_claude_root = settings.claude_root
    _derive_child_dirs(settings, explicit_env, settings.main_workspace_dir.parent, _WORKSPACE_PROFILE_DIR_DEFAULTS)
    _derive_child_dirs(settings, explicit_env, settings.main_claude_root.parent, _CLAUDE_ROOT_PROFILE_DIR_DEFAULTS)
    if "CLAUDE_HOME" not in explicit_env and settings.claude_home == _DEFAULT_CLAUDE_HOME:
        settings.claude_home = settings.main_claude_root / ".claude"


def _derive_child_dirs(settings: Any, explicit_env: Mapping[str, str], parent: Path, defaults: tuple[tuple[str, str, Path, str], ...]) -> None:
    for env_name, attr_name, default_path, child_name in defaults:
        if env_name not in explicit_env and getattr(settings, attr_name) == default_path:
            setattr(settings, attr_name, parent / child_name)


class AppSettings(BaseSettings):
    """Runtime settings loaded from environment variables."""

    model_config = SettingsConfigDict(env_file=("docker/.env", "docker/.env.local"), extra="ignore")

    api_host: str = Field(default="0.0.0.0", alias="API_HOST")
    api_port: int = Field(default=8080, alias="API_PORT")
    host_port: int = Field(default=58080, alias="HOST_PORT")
    host_runtime_volume_root: str = Field(default=str(Path.home() / "volume-agent-runtime"), alias="HOST_RUNTIME_VOLUME_ROOT")
    host_workspace_mount: str = Field(default=str(Path.home() / "volume-agent-runtime" / "main-workspace"), alias="HOST_WORKSPACE_MOUNT")
    host_data_mount: str = Field(default=str(Path.home() / "volume-agent-runtime" / "data"), alias="HOST_DATA_MOUNT")
    host_claude_root_mount: str = Field(default=str(Path.home() / "volume-agent-runtime" / "claude-roots" / "main"), alias="HOST_CLAUDE_ROOT_MOUNT")
    host_eval_case_governor_workspace_mount: str = Field(default=str(Path.home() / "volume-agent-runtime" / "eval-case-governor-workspace"), alias="HOST_EVAL_CASE_GOVERNOR_WORKSPACE_MOUNT")
    host_regression_impact_analyzer_workspace_mount: str = Field(default=str(Path.home() / "volume-agent-runtime" / "regression-impact-analyzer-workspace"), alias="HOST_REGRESSION_IMPACT_ANALYZER_WORKSPACE_MOUNT")
    host_eval_case_governor_claude_root_mount: str = Field(default=str(Path.home() / "volume-agent-runtime" / "claude-roots" / "eval-case-governor"), alias="HOST_EVAL_CASE_GOVERNOR_CLAUDE_ROOT_MOUNT")
    host_regression_impact_analyzer_claude_root_mount: str = Field(default=str(Path.home() / "volume-agent-runtime" / "claude-roots" / "regression-impact-analyzer"), alias="HOST_REGRESSION_IMPACT_ANALYZER_CLAUDE_ROOT_MOUNT")

    workspace_dir: Path = Field(default=Path("/main-workspace"), alias="WORKSPACE_DIR")
    main_workspace_dir: Path = Field(default=Path("/main-workspace"), alias="MAIN_WORKSPACE_DIR")
    attribution_analyzer_workspace_dir: Path = Field(default=Path("/attribution-analyzer-workspace"), alias="ATTRIBUTION_ANALYZER_WORKSPACE_DIR")
    proposal_generator_workspace_dir: Path = Field(default=Path("/proposal-generator-workspace"), alias="PROPOSAL_GENERATOR_WORKSPACE_DIR")
    execution_optimizer_workspace_dir: Path = Field(default=Path("/execution-optimizer-workspace"), alias="EXECUTION_OPTIMIZER_WORKSPACE_DIR")
    eval_case_governor_workspace_dir: Path = Field(default=Path("/eval-case-governor-workspace"), alias="EVAL_CASE_GOVERNOR_WORKSPACE_DIR")
    regression_impact_analyzer_workspace_dir: Path = Field(default=Path("/regression-impact-analyzer-workspace"), alias="REGRESSION_IMPACT_ANALYZER_WORKSPACE_DIR")
    data_dir: Path = Field(default=Path("/data"), alias="DATA_DIR")
    claude_root: Path = Field(default=Path("/claude-roots/main"), alias="CLAUDE_ROOT")
    main_claude_root: Path = Field(default=Path("/claude-roots/main"), alias="MAIN_CLAUDE_ROOT")
    attribution_analyzer_claude_root: Path = Field(default=Path("/claude-roots/attribution-analyzer"), alias="ATTRIBUTION_ANALYZER_CLAUDE_ROOT")
    proposal_generator_claude_root: Path = Field(default=Path("/claude-roots/proposal-generator"), alias="PROPOSAL_GENERATOR_CLAUDE_ROOT")
    execution_optimizer_claude_root: Path = Field(default=Path("/claude-roots/execution-optimizer"), alias="EXECUTION_OPTIMIZER_CLAUDE_ROOT")
    eval_case_governor_claude_root: Path = Field(default=Path("/claude-roots/eval-case-governor"), alias="EVAL_CASE_GOVERNOR_CLAUDE_ROOT")
    regression_impact_analyzer_claude_root: Path = Field(default=Path("/claude-roots/regression-impact-analyzer"), alias="REGRESSION_IMPACT_ANALYZER_CLAUDE_ROOT")
    claude_home: Path = Field(default=Path("/claude-roots/main/.claude"), alias="CLAUDE_HOME")
    claude_config_dir: Optional[Path] = Field(default=None, alias="CLAUDE_CONFIG_DIR")

    anthropic_api_key: Optional[str] = Field(default=None, alias="ANTHROPIC_API_KEY")
    anthropic_base_url: Optional[str] = Field(default=None, alias="ANTHROPIC_BASE_URL")
    model_provider_api_key: Optional[str] = Field(default=None, alias="MODEL_PROVIDER_API_KEY")
    model_provider_api_url: Optional[str] = Field(default=None, alias="MODEL_PROVIDER_API_URL")
    api_key: Optional[str] = Field(default=None, alias="API_KEY")

    agent_model: Optional[str] = Field(default="claude-sonnet-4-5", alias="AGENT_MODEL")
    fallback_model: Optional[str] = Field(default=None, alias="FALLBACK_MODEL")
    permission_mode: Optional[str] = Field(default="dontAsk", alias="PERMISSION_MODE")
    default_agent: Optional[str] = Field(default=None, alias="DEFAULT_AGENT")

    claude_tools_raw: Optional[str] = Field(default=None, alias="CLAUDE_TOOLS")
    default_skills_raw: Optional[str] = Field(default=None, alias="DEFAULT_SKILLS")
    default_allowed_tools_raw: str = Field(default="Read,Grep,Glob,Skill,Write", alias="DEFAULT_ALLOWED_TOOLS")
    default_disallowed_tools_raw: str = Field(default="Bash,WebFetch,WebSearch", alias="DEFAULT_DISALLOWED_TOOLS")
    default_skills_mode: Literal["all", "default", "none"] = Field(default="default", alias="DEFAULT_SKILLS_MODE")
    claude_system_append: Optional[str] = Field(default=None, alias="CLAUDE_SYSTEM_APPEND")
    claude_settings_path: Optional[Path] = Field(default=None, alias="CLAUDE_SETTINGS_PATH")
    claude_mcp_config_path: Optional[Path] = Field(default=None, alias="CLAUDE_MCP_CONFIG_PATH")
    strict_mcp_config: bool = Field(default=False, alias="STRICT_MCP_CONFIG")

    enable_programmatic_agents: bool = Field(default=False, alias="ENABLE_PROGRAMMATIC_AGENTS")
    enable_sdk_session_resume: bool = Field(default=True, alias="ENABLE_SDK_SESSION_RESUME")
    enable_policy_hooks: bool = Field(default=True, alias="ENABLE_POLICY_HOOKS")
    enable_feedback_debug_evidence: bool = Field(default=True, alias="ENABLE_FEEDBACK_DEBUG_EVIDENCE")
    enable_dspy_output_formatter: bool = Field(default=True, alias="ENABLE_DSPY_OUTPUT_FORMATTER")
    dspy_output_formatter_model: Optional[str] = Field(default=None, alias="DSPY_OUTPUT_FORMATTER_MODEL")
    dspy_output_formatter_timeout_seconds: int = Field(default=120, alias="DSPY_OUTPUT_FORMATTER_TIMEOUT_SECONDS")
    dspy_output_formatter_max_retries: int = Field(default=1, alias="DSPY_OUTPUT_FORMATTER_MAX_RETRIES")
    include_hook_events: bool = Field(default=True, alias="INCLUDE_HOOK_EVENTS")
    include_partial_messages: bool = Field(default=False, alias="INCLUDE_PARTIAL_MESSAGES")
    max_turns: int = Field(default=16, alias="MAX_TURNS")
    max_budget_usd: Optional[float] = Field(default=None, alias="MAX_BUDGET_USD")
    max_buffer_size: Optional[int] = Field(default=None, alias="MAX_BUFFER_SIZE")

    claude_cli_path: Optional[Path] = Field(default=None, alias="CLAUDE_CLI_PATH")
    claude_add_dirs_raw: Optional[str] = Field(default=None, alias="CLAUDE_ADD_DIRS")
    claude_betas_raw: Optional[str] = Field(default=None, alias="CLAUDE_BETAS")
    permission_prompt_tool_name: Optional[str] = Field(default=None, alias="PERMISSION_PROMPT_TOOL_NAME")
    claude_user: Optional[str] = Field(default=None, alias="CLAUDE_USER")
    setting_sources_raw: Optional[str] = Field(default="user,project,local", alias="CLAUDE_SETTING_SOURCES")
    max_thinking_tokens: Optional[int] = Field(default=None, alias="MAX_THINKING_TOKENS")
    effort: Optional[Literal["low", "medium", "high", "xhigh", "max"]] = Field(default=None, alias="EFFORT")
    enable_file_checkpointing: bool = Field(default=False, alias="ENABLE_FILE_CHECKPOINTING")
    session_store_flush: Literal["batched", "eager"] = Field(default="batched", alias="SESSION_STORE_FLUSH")
    load_timeout_ms: int = Field(default=60000, alias="LOAD_TIMEOUT_MS")

    claude_env_json: Optional[str] = Field(default=None, alias="CLAUDE_ENV_JSON")
    claude_extra_args_json: Optional[str] = Field(default=None, alias="CLAUDE_EXTRA_ARGS_JSON")

    agent_git_service_provider: Literal["local", "gitea"] = Field(default="local", alias="AGENT_GIT_SERVICE_PROVIDER")
    agent_git_service_url: Optional[str] = Field(default=None, alias="AGENT_GIT_SERVICE_URL")
    agent_git_service_public_url: Optional[str] = Field(default=None, alias="AGENT_GIT_SERVICE_PUBLIC_URL")
    agent_git_repository_name: str = Field(default="main-agent-config", alias="AGENT_GIT_REPOSITORY_NAME")
    agent_git_repository_dir_override: Optional[Path] = Field(default=None, alias="AGENT_GIT_REPOSITORY_DIR")
    agent_git_worktrees_dir_override: Optional[Path] = Field(default=None, alias="AGENT_GIT_WORKTREES_DIR")
    agent_release_archives_dir_override: Optional[Path] = Field(default=None, alias="AGENT_RELEASE_ARCHIVES_DIR")
    agent_git_user_name: str = Field(default="Claude Agent Runtime", alias="AGENT_GIT_USER_NAME")
    agent_git_user_email: str = Field(default="agent-runtime@example.local", alias="AGENT_GIT_USER_EMAIL")

    langfuse_enabled: bool = Field(default=False, alias="LANGFUSE_ENABLED")
    langfuse_public_key: Optional[str] = Field(default=None, alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: Optional[str] = Field(default=None, alias="LANGFUSE_SECRET_KEY")
    langfuse_base_url: str = Field(default="https://cloud.langfuse.com", alias="LANGFUSE_BASE_URL")
    langfuse_otel_endpoint: Optional[str] = Field(default=None, alias="LANGFUSE_OTEL_ENDPOINT")
    langfuse_otel_signals_raw: str = Field(default="traces,metrics,logs", alias="LANGFUSE_OTEL_SIGNALS")
    langfuse_service_name: str = Field(default="claude-agent-runtime-api", alias="LANGFUSE_SERVICE_NAME")
    langfuse_deployment_environment: str = Field(default="local", alias="LANGFUSE_DEPLOYMENT_ENVIRONMENT")
    langfuse_resource_attributes_raw: Optional[str] = Field(default=None, alias="LANGFUSE_RESOURCE_ATTRIBUTES")
    langfuse_export_interval_ms: int = Field(default=1000, alias="LANGFUSE_EXPORT_INTERVAL_MS")

    def model_post_init(self, __context: Any) -> None:
        _derive_profile_dirs(self)

    @property
    def provider_api_key(self) -> Optional[str]:
        return self.model_provider_api_key or self.anthropic_api_key

    @property
    def provider_api_url(self) -> Optional[str]:
        return self.model_provider_api_url or self.anthropic_base_url

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
    def claude_tools(self) -> Optional[list[str]]:
        tools = _csv(self.claude_tools_raw)
        return tools or None

    @property
    def default_skills(self) -> Optional[list[str]]:
        skills = _csv(self.default_skills_raw)
        return skills or None

    @property
    def default_allowed_tools(self) -> list[str]:
        return _csv(self.default_allowed_tools_raw)

    @property
    def default_disallowed_tools(self) -> list[str]:
        return _csv(self.default_disallowed_tools_raw)

    @property
    def claude_add_dirs(self) -> list[str]:
        return _csv(self.claude_add_dirs_raw)

    @property
    def claude_betas(self) -> list[str]:
        return _csv(self.claude_betas_raw)

    @property
    def setting_sources(self) -> Optional[list[str]]:
        if self.setting_sources_raw is None:
            return None
        return _csv(self.setting_sources_raw)

    @property
    def claude_env(self) -> dict[str, str]:
        return _string_dict(_json_object(self.claude_env_json))

    @property
    def claude_extra_args(self) -> dict[str, str | None]:
        return _optional_string_dict(_json_object(self.claude_extra_args_json))

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
        return self.agent_git_repository_dir_override or self.main_workspace_dir

    @property
    def agent_git_worktrees_dir(self) -> Path:
        return self.agent_git_worktrees_dir_override or self.data_dir / "agent-governance" / "worktrees"

    @property
    def agent_release_archives_dir(self) -> Path:
        return self.agent_release_archives_dir_override or self.data_dir / "agent-governance" / "releases"

    @property
    def runtime_db_path(self) -> Path:
        return self.data_dir / "runtime.sqlite3"


@lru_cache
def get_settings() -> AppSettings:
    settings = AppSettings()
    _derive_profile_dirs(settings)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.main_workspace_dir.mkdir(parents=True, exist_ok=True)
    settings.attribution_analyzer_workspace_dir.mkdir(parents=True, exist_ok=True)
    settings.proposal_generator_workspace_dir.mkdir(parents=True, exist_ok=True)
    settings.execution_optimizer_workspace_dir.mkdir(parents=True, exist_ok=True)
    settings.eval_case_governor_workspace_dir.mkdir(parents=True, exist_ok=True)
    settings.regression_impact_analyzer_workspace_dir.mkdir(parents=True, exist_ok=True)
    settings.main_claude_root.mkdir(parents=True, exist_ok=True)
    settings.attribution_analyzer_claude_root.mkdir(parents=True, exist_ok=True)
    settings.proposal_generator_claude_root.mkdir(parents=True, exist_ok=True)
    settings.execution_optimizer_claude_root.mkdir(parents=True, exist_ok=True)
    settings.eval_case_governor_claude_root.mkdir(parents=True, exist_ok=True)
    settings.regression_impact_analyzer_claude_root.mkdir(parents=True, exist_ok=True)
    settings.claude_home.mkdir(parents=True, exist_ok=True)
    if settings.resolved_claude_config_dir:
        settings.resolved_claude_config_dir.mkdir(parents=True, exist_ok=True)
    settings.agent_git_repository_dir.mkdir(parents=True, exist_ok=True)
    settings.agent_git_worktrees_dir.mkdir(parents=True, exist_ok=True)
    settings.agent_release_archives_dir.mkdir(parents=True, exist_ok=True)
    return settings
