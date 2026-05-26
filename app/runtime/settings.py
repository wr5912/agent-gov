import base64
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    loaded = json.loads(value)
    if not isinstance(loaded, dict):
        raise ValueError("Expected a JSON object")
    return loaded


def _string_dict(value: dict[str, Any]) -> dict[str, str]:
    return {str(key): str(val) for key, val in value.items()}


def _optional_string_dict(value: dict[str, Any]) -> dict[str, str | None]:
    return {str(key): None if val is None else str(val) for key, val in value.items()}


class AppSettings(BaseSettings):
    """Runtime settings loaded from environment variables."""

    model_config = SettingsConfigDict(env_file="docker/.env", extra="ignore")

    api_host: str = Field(default="0.0.0.0", alias="API_HOST")
    api_port: int = Field(default=8080, alias="API_PORT")
    host_port: int = Field(default=58080, alias="HOST_PORT")
    host_workspace_mount: str = Field(default="./docker/volume/main-workspace", alias="HOST_WORKSPACE_MOUNT")
    host_data_mount: str = Field(default="./docker/volume/data", alias="HOST_DATA_MOUNT")
    host_claude_root_mount: str = Field(default="./docker/volume/claude-roots/main", alias="HOST_CLAUDE_ROOT_MOUNT")

    workspace_dir: Path = Field(default=Path("/main-workspace"), alias="WORKSPACE_DIR")
    main_workspace_dir: Path = Field(default=Path("/main-workspace"), alias="MAIN_WORKSPACE_DIR")
    attribution_workspace_dir: Path = Field(default=Path("/attribution-workspace"), alias="ATTRIBUTION_WORKSPACE_DIR")
    proposal_workspace_dir: Path = Field(default=Path("/proposal-workspace"), alias="PROPOSAL_WORKSPACE_DIR")
    data_dir: Path = Field(default=Path("/data"), alias="DATA_DIR")
    claude_root: Path = Field(default=Path("/claude-roots/main"), alias="CLAUDE_ROOT")
    main_claude_root: Path = Field(default=Path("/claude-roots/main"), alias="MAIN_CLAUDE_ROOT")
    attribution_claude_root: Path = Field(default=Path("/claude-roots/attribution"), alias="ATTRIBUTION_CLAUDE_ROOT")
    proposal_claude_root: Path = Field(default=Path("/claude-roots/proposal"), alias="PROPOSAL_CLAUDE_ROOT")
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
    default_allowed_tools_raw: str = Field(default="Read,Grep,Glob,Skill", alias="DEFAULT_ALLOWED_TOOLS")
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
    max_turns: int = Field(default=8, alias="MAX_TURNS")
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
        if self.main_workspace_dir == Path("/main-workspace") and self.workspace_dir != Path("/main-workspace"):
            self.main_workspace_dir = self.workspace_dir
        if self.main_claude_root == Path("/claude-roots/main") and self.claude_root != Path("/claude-roots/main"):
            self.main_claude_root = self.claude_root
        if self.attribution_workspace_dir == Path("/attribution-workspace") and self.main_workspace_dir != Path("/main-workspace"):
            self.attribution_workspace_dir = self.main_workspace_dir.parent / "attribution-workspace"
        if self.proposal_workspace_dir == Path("/proposal-workspace") and self.main_workspace_dir != Path("/main-workspace"):
            self.proposal_workspace_dir = self.main_workspace_dir.parent / "proposal-workspace"
        if self.attribution_claude_root == Path("/claude-roots/attribution") and self.main_claude_root != Path("/claude-roots/main"):
            self.attribution_claude_root = self.main_claude_root.parent / "attribution"
        if self.proposal_claude_root == Path("/claude-roots/proposal") and self.main_claude_root != Path("/claude-roots/main"):
            self.proposal_claude_root = self.main_claude_root.parent / "proposal"
        if self.claude_home == Path("/claude-roots/main/.claude") and self.main_claude_root != Path("/claude-roots/main"):
            self.claude_home = self.main_claude_root / ".claude"

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
    def claude_settings_file(self) -> Optional[Path]:
        return self.claude_settings_path

    @property
    def claude_mcp_servers(self) -> str | dict[str, Any] | None:
        if self.claude_mcp_config_path:
            return str(self.claude_mcp_config_path)
        return None

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
    def transcript_dir(self) -> Path:
        return self.data_dir / "transcripts"

    @property
    def session_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def output_dir(self) -> Path:
        return self.data_dir / "outputs"

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def agent_runs_dir(self) -> Path:
        return self.data_dir / "agent-runs"

    @property
    def feedback_signals_dir(self) -> Path:
        return self.data_dir / "feedback-signals"

    @property
    def soc_events_dir(self) -> Path:
        return self.data_dir / "soc-events"

    @property
    def pending_correlations_dir(self) -> Path:
        return self.data_dir / "pending-correlations"

    @property
    def feedback_cases_dir(self) -> Path:
        return self.data_dir / "feedback-cases"

    @property
    def evidence_packages_dir(self) -> Path:
        return self.data_dir / "evidence-packages"

    @property
    def feedback_analysis_jobs_dir(self) -> Path:
        return self.data_dir / "feedback-analysis" / "jobs"

    @property
    def optimization_proposals_dir(self) -> Path:
        return self.data_dir / "optimization-proposals"

    @property
    def optimization_tasks_dir(self) -> Path:
        return self.data_dir / "optimization-tasks"

    @property
    def agent_versions_dir(self) -> Path:
        return self.data_dir / "agent-versions" / "main"

    @property
    def runtime_db_path(self) -> Path:
        return self.data_dir / "runtime.sqlite3"


@lru_cache
def get_settings() -> AppSettings:
    settings = AppSettings()
    if "MAIN_WORKSPACE_DIR" not in os.environ and "WORKSPACE_DIR" in os.environ:
        settings.main_workspace_dir = settings.workspace_dir
    if "MAIN_CLAUDE_ROOT" not in os.environ and "CLAUDE_ROOT" in os.environ:
        settings.main_claude_root = settings.claude_root
    if "ATTRIBUTION_WORKSPACE_DIR" not in os.environ:
        settings.attribution_workspace_dir = settings.main_workspace_dir.parent / "attribution-workspace"
    if "PROPOSAL_WORKSPACE_DIR" not in os.environ:
        settings.proposal_workspace_dir = settings.main_workspace_dir.parent / "proposal-workspace"
    if "ATTRIBUTION_CLAUDE_ROOT" not in os.environ:
        settings.attribution_claude_root = settings.main_claude_root.parent / "attribution"
    if "PROPOSAL_CLAUDE_ROOT" not in os.environ:
        settings.proposal_claude_root = settings.main_claude_root.parent / "proposal"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.main_workspace_dir.mkdir(parents=True, exist_ok=True)
    settings.attribution_workspace_dir.mkdir(parents=True, exist_ok=True)
    settings.proposal_workspace_dir.mkdir(parents=True, exist_ok=True)
    settings.main_claude_root.mkdir(parents=True, exist_ok=True)
    settings.attribution_claude_root.mkdir(parents=True, exist_ok=True)
    settings.proposal_claude_root.mkdir(parents=True, exist_ok=True)
    settings.claude_home.mkdir(parents=True, exist_ok=True)
    if settings.resolved_claude_config_dir:
        settings.resolved_claude_config_dir.mkdir(parents=True, exist_ok=True)
    settings.agent_versions_dir.mkdir(parents=True, exist_ok=True)
    return settings
