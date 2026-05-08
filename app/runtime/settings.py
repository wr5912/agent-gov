from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


class AppSettings(BaseSettings):
    """Runtime settings loaded from environment variables."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    workspace_dir: Path = Field(default=Path("/workspace"), alias="WORKSPACE_DIR")
    data_dir: Path = Field(default=Path("/data"), alias="DATA_DIR")
    claude_home: Path = Field(default=Path("/home/agentuser/.claude"), alias="CLAUDE_HOME")

    anthropic_api_key: Optional[str] = Field(default=None, alias="ANTHROPIC_API_KEY")
    api_key: Optional[str] = Field(default=None, alias="API_KEY")

    agent_model: Optional[str] = Field(default="claude-sonnet-4-5", alias="AGENT_MODEL")
    fallback_model: Optional[str] = Field(default=None, alias="FALLBACK_MODEL")
    permission_mode: Optional[str] = Field(default="dontAsk", alias="PERMISSION_MODE")

    default_allowed_tools_raw: str = Field(default="Read,Grep,Glob", alias="DEFAULT_ALLOWED_TOOLS")
    default_disallowed_tools_raw: str = Field(default="Bash,WebFetch,WebSearch", alias="DEFAULT_DISALLOWED_TOOLS")

    enable_programmatic_agents: bool = Field(default=True, alias="ENABLE_PROGRAMMATIC_AGENTS")
    enable_sdk_session_resume: bool = Field(default=True, alias="ENABLE_SDK_SESSION_RESUME")
    include_hook_events: bool = Field(default=True, alias="INCLUDE_HOOK_EVENTS")
    max_turns: int = Field(default=8, alias="MAX_TURNS")
    max_budget_usd: Optional[float] = Field(default=None, alias="MAX_BUDGET_USD")

    @property
    def default_allowed_tools(self) -> List[str]:
        return _csv(self.default_allowed_tools_raw)

    @property
    def default_disallowed_tools(self) -> List[str]:
        return _csv(self.default_disallowed_tools_raw)

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


@lru_cache
def get_settings() -> AppSettings:
    settings = AppSettings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.transcript_dir.mkdir(parents=True, exist_ok=True)
    settings.session_dir.mkdir(parents=True, exist_ok=True)
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    return settings
