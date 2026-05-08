from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(..., description="User message or task prompt.")
    session_id: Optional[str] = Field(default=None, description="Client-visible session id. If omitted, the API creates one.")
    agent: Optional[str] = Field(default=None, description="Subagent name, for example security-triage. Omit to use DEFAULT_AGENT.")
    skills: Optional[list[str]] = Field(default=None, description="Skill names to enable. Omit to use DEFAULT_SKILLS.")
    skills_mode: Optional[Literal["all", "default", "none"]] = Field(
        default=None,
        description="Skill loading mode. Omit to use DEFAULT_SKILLS_MODE from .env.",
    )
    allowed_tools: Optional[list[str]] = Field(default=None, description="Per-request allow list. Defaults to DEFAULT_ALLOWED_TOOLS.")
    disallowed_tools: Optional[list[str]] = Field(default=None, description="Per-request deny list. Defaults to DEFAULT_DISALLOWED_TOOLS.")
    max_turns: Optional[int] = Field(default=None, ge=1, le=50, description="Per-request turn cap. Defaults to MAX_TURNS.")
    model: Optional[str] = Field(default=None, description="Per-request model override. Defaults to AGENT_MODEL.")
    permission_mode: Optional[str] = Field(default=None, description="Per-request permission mode override. Defaults to PERMISSION_MODE.")
    system_append: Optional[str] = Field(default=None, description="Extra instruction appended to the Claude Code preset prompt.")
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "message": "请说明当前 workspace 中有哪些 subagents 和 skills",
                    "skills_mode": "all",
                    "allowed_tools": ["Read", "Grep", "Glob"],
                }
            ]
        }
    }


class ChatResponse(BaseModel):
    session_id: str
    sdk_session_id: Optional[str] = None
    answer: str
    messages: list[dict[str, Any]] = Field(default_factory=list)
    usage: Optional[dict[str, Any]] = None
    total_cost_usd: Optional[float] = None
    stop_reason: Optional[str] = None
    errors: list[str] = Field(default_factory=list)


class SessionInfo(BaseModel):
    session_id: str
    sdk_session_id: Optional[str] = None
    created_at: str
    updated_at: str
    title: Optional[str] = None
    turns: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentInfo(BaseModel):
    name: str
    path: str
    description: Optional[str] = None
    model: Optional[str] = None
    tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)


class SkillInfo(BaseModel):
    name: str
    path: str
    description: Optional[str] = None


class ConfigMappingItem(BaseModel):
    scope: str
    kind: str
    container_path: str
    host_mount: Optional[str] = None
    exists: bool
    loaded_by_default: bool
    git_policy: str
    notes: Optional[str] = None


class ConfigMappingResponse(BaseModel):
    claude_config_mode: str
    claude_home: str
    claude_global_config_file: str
    claude_config_dir: Optional[str] = None
    setting_sources_effective: Optional[list[str]] = None
    mappings: list[ConfigMappingItem]


class OpenAIChatMessage(BaseModel):
    role: str
    content: str


class OpenAIChatCompletionRequest(BaseModel):
    model: Optional[str] = Field(default=None, description="Model override. Defaults to AGENT_MODEL.")
    messages: list[OpenAIChatMessage] = Field(..., description="OpenAI-compatible chat messages.")
    stream: bool = Field(default=False, description="Reserved for compatibility. This shim currently returns non-streaming responses.")
    max_turns: Optional[int] = Field(default=None, description="Claude Agent turn cap for this request.")
    metadata: dict[str, Any] = Field(default_factory=dict)


class OpenAIChatCompletionChoice(BaseModel):
    index: int = 0
    message: OpenAIChatMessage
    finish_reason: Optional[str] = "stop"


class OpenAIChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    model: Optional[str] = None
    choices: list[OpenAIChatCompletionChoice]
    usage: Optional[dict[str, Any]] = None
