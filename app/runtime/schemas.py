from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(..., description="User message or task prompt.")
    session_id: Optional[str] = Field(default=None, description="Client-visible session id. If omitted, the API creates one.")
    agent: Optional[str] = Field(default=None, description="Subagent name, for example security-triage.")
    skills: Optional[list[str]] = Field(default=None, description="Skill names to enable. Use ['all'] only via skills_mode.")
    skills_mode: Optional[Literal["all", "default", "none"]] = Field(default="default")
    allowed_tools: Optional[list[str]] = Field(default=None)
    disallowed_tools: Optional[list[str]] = Field(default=None)
    max_turns: Optional[int] = Field(default=None, ge=1, le=50)
    model: Optional[str] = None
    permission_mode: Optional[str] = None
    system_append: Optional[str] = Field(default=None, description="Extra instruction appended to the Claude Code preset prompt.")
    metadata: dict[str, Any] = Field(default_factory=dict)


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


class OpenAIChatMessage(BaseModel):
    role: str
    content: str


class OpenAIChatCompletionRequest(BaseModel):
    model: Optional[str] = None
    messages: list[OpenAIChatMessage]
    stream: bool = False
    max_turns: Optional[int] = None
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
