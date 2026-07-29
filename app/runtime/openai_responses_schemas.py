"""OpenAI Responses-first 过渡契约模型。

严格按 ``docs/engineering/OpenAI兼容接口能否替代原生Chat端点评估.md``：

- ``POST /v1/responses`` 采用 OpenAI Responses 外形（``input`` / ``instructions`` / ``store`` /
  ``conversation`` / ``previous_response_id`` / ``metadata``）+ 顶层 ``agentgov`` 强类型扩展。
- ``agentgov`` 是唯一承载「OpenAI 标准字段无法表达的控制面」的地方（业务 Agent 选择、
  turn cap、raw 调试）；``extra="forbid"`` 堵未知字段。
- ``metadata`` 接受后端不路由的任意 JSON 对象；公开投影移除 backend-reserved keys。
  ``alert_id``/``case_id`` 是 backend-owned 路由输入，走 ``agentgov``。

本模块只放 pydantic 契约；请求→ChatRequest 映射与响应投影在 ``openai_responses_adapter.py``。
当前 stream/non-stream/retrieve 的已知投影偏差由 OpenAPI operation 扩展声明。
"""

from __future__ import annotations

from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.runtime.json_types import JsonObject
from app.runtime.schemas import NON_BLANK_TEXT_PATTERN


class AgentGovDebug(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sdk_raw: bool = False


class AgentGovRequestExtension(BaseModel):
    """control 模式的 AgentGov 控制面扩展。存在即选中 control 模式。"""

    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(
        ...,
        min_length=1,
        pattern=NON_BLANK_TEXT_PATTERN,
        description="Business agent to run in control mode. Must contain at least one non-whitespace character.",
    )
    alert_id: Optional[str] = Field(default=None, description="Feedback-loop routing input (backend-owned).")
    case_id: Optional[str] = Field(default=None, description="Feedback-loop routing input (backend-owned).")
    max_turns: Optional[int] = Field(default=None, ge=1, le=50, description="Claude Code turn cap.")
    include_trace: bool = Field(
        default=False,
        description="Emit complete semantic SDK facts as agentgov.trace_event envelopes.",
    )
    with_speech_summary: bool = Field(
        default=False,
        description="Control streaming only: emit best-effort agentgov.speech_summary events.",
    )
    debug: Optional[AgentGovDebug] = None


class ResponsesInputText(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["input_text"] = "input_text"
    text: str = Field(min_length=1, pattern=NON_BLANK_TEXT_PATTERN)

    @field_validator("text")
    @classmethod
    def _non_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("input_text must contain non-whitespace text")
        return value


class ResponsesInputMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["message"] = "message"
    role: Literal["developer", "system", "user", "assistant"]
    content: Annotated[str, Field(min_length=1, pattern=NON_BLANK_TEXT_PATTERN)] | Annotated[list[ResponsesInputText], Field(min_length=1)]

    @field_validator("content")
    @classmethod
    def _valid_content(cls, value: str | list[ResponsesInputText]) -> str | list[ResponsesInputText]:
        if isinstance(value, str) and not value.strip():
            raise ValueError("message content must contain non-whitespace text")
        return value

    def text_content(self) -> str:
        if isinstance(self.content, str):
            return self.content
        return "\n".join(block.text for block in self.content)


class ResponsesRequest(BaseModel):
    """``POST /v1/responses`` 请求。无 ``agentgov`` = strict 模式；有 = control 模式。"""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "oneOf": [
                {
                    "title": "Strict Responses request",
                    "properties": {
                        "agentgov": {"type": "null"},
                        "instructions": {"type": "null"},
                    },
                },
                {
                    "title": "AgentGov control Responses request",
                    "required": ["agentgov"],
                    "properties": {
                        "agentgov": {
                            "type": "object",
                            "required": ["agent_id"],
                        },
                    },
                },
            ],
            "allOf": [
                {
                    "if": {
                        "required": ["agentgov"],
                        "properties": {
                            "agentgov": {
                                "type": "object",
                                "required": ["with_speech_summary"],
                                "properties": {"with_speech_summary": {"const": True}},
                            }
                        },
                    },
                    "then": {"properties": {"stream": {"const": True}}},
                }
            ],
        },
    )

    model: Optional[str] = Field(default=None, description="Per-request LLM override only; never an agent handle.")
    input: Annotated[str, Field(min_length=1, pattern=NON_BLANK_TEXT_PATTERN)] | Annotated[list[ResponsesInputMessage], Field(min_length=1)] = Field(
        ...,
        description="Non-empty prompt string, or typed text message items containing a current user message.",
    )
    instructions: Optional[str] = Field(
        default=None,
        description=(
            "OpenAI standard field NAME. In AgentGov this is APPEND-ONLY (mapped to system_append, appended to the "
            "Claude Code preset + workspace CLAUDE.md), which differs from OpenAI replace/swap semantics. "
            "Rejected (422) on the strict surface."
        ),
    )
    stream: bool = False
    store: bool = Field(default=True, description="Default true; false only closes public GET /v1/responses/{id}, internal audit stays.")
    conversation: Optional[str] = Field(default=None, description="conv_<session_id>; maps to the server session.")
    previous_response_id: Optional[str] = Field(
        default=None,
        description="Derives owning conversation; 409 if inconsistent with an explicit conversation, 404 if not found.",
    )
    metadata: JsonObject = Field(
        default_factory=dict,
        description=(
            "AgentGov transitional metadata object. Values may be nested JSON; backend-reserved keys are removed "
            "before public echo and the backend does not route on remaining entries."
        ),
    )
    agentgov: Optional[AgentGovRequestExtension] = Field(default=None, description="Presence selects control mode; carries the non-standard control plane.")

    @model_validator(mode="after")
    def _valid_input(self) -> ResponsesRequest:
        if isinstance(self.input, str):
            if not self.input.strip():
                raise ValueError("input must contain non-whitespace text")
            return self
        if not any(item.role == "user" and item.text_content().strip() for item in self.input):
            raise ValueError("input items must contain at least one non-empty user message")
        return self


# ---- Response ----


class ResponseOutputText(BaseModel):
    type: Literal["output_text"] = "output_text"
    text: str


class ResponseOutputMessage(BaseModel):
    type: Literal["message"] = "message"
    id: str
    status: Literal["completed", "in_progress"] = "completed"
    role: Literal["assistant"] = "assistant"
    content: list[ResponseOutputText] = Field(default_factory=list)


class ResponseReasoningText(BaseModel):
    type: Literal["reasoning_text"] = "reasoning_text"
    text: str


class ResponseReasoningItem(BaseModel):
    type: Literal["reasoning"] = "reasoning"
    id: str
    status: Literal["completed", "in_progress"] = "completed"
    summary: list[JsonObject] = Field(default_factory=list)
    content: list[ResponseReasoningText] = Field(default_factory=list)


class AgentGovResponseExtension(BaseModel):
    """响应侧 AgentGov 扩展（对称于请求侧顶层 agentgov）。"""

    model_config = ConfigDict(extra="allow")

    run_id: Optional[str] = None
    conversation_id: Optional[str] = None
    session_id: Optional[str] = None
    sdk_session_id: Optional[str] = None
    agent_id: Optional[str] = None
    agent_version_id: Optional[str] = None
    case_id: Optional[str] = None
    trace_id: Optional[str] = None
    output_text: Optional[str] = Field(
        default=None,
        description="Convenience aggregate of output[] text; AgentGov projection, not an OpenAI wire-standard field.",
    )
    agent_activity: JsonObject = Field(default_factory=dict)
    usage: Optional[JsonObject] = None
    total_cost_usd: Optional[float] = None
    stop_reason: Optional[str] = None
    errors: list[str] = Field(default_factory=list)


ResponseStatus = Literal["completed", "failed", "incomplete"]


class ResponseObject(BaseModel):
    """OpenAI Responses ``response`` 对象 + 顶层 ``agentgov`` 扩展。

    权威输出在 ``output[]``（``message`` -> ``content[].output_text.text``）；便利聚合在
    ``agentgov.output_text``（不在顶层放 output_text 冒充 OpenAI 标准字段）。
    """

    id: str
    object: Literal["response"] = "response"
    created_at: Optional[int] = None
    status: ResponseStatus
    model: Optional[str] = None
    output: list[ResponseReasoningItem | ResponseOutputMessage] = Field(default_factory=list)
    usage: Optional[JsonObject] = None
    metadata: JsonObject = Field(default_factory=dict)
    agentgov: Optional[AgentGovResponseExtension] = None


# ---- Conversations（会话对象，投影自 SDK session / transcript，不另建副本）----


class ConversationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metadata: JsonObject = Field(
        default_factory=dict,
        description="Observability metadata; backend-reserved keys are removed and the backend does not route on remaining entries.",
    )


class AgentGovConversationExtension(BaseModel):
    """会话对象上的 AgentGov 扩展（session 专属、非 OpenAI 标准字段；OpenAI 客户端忽略）。"""

    model_config = ConfigDict(extra="allow")

    agent_id: Optional[str] = None
    sdk_session_id: Optional[str] = None
    updated_at: Optional[int] = None
    turns: Optional[int] = None
    active_run_id: Optional[str] = None
    active_run_expires_at: Optional[str] = None


class Conversation(BaseModel):
    id: str
    object: Literal["conversation"] = "conversation"
    created_at: Optional[int] = None
    title: Optional[str] = None
    metadata: JsonObject = Field(default_factory=dict)
    agentgov: AgentGovConversationExtension = Field(default_factory=AgentGovConversationExtension)


class ConversationList(BaseModel):
    object: Literal["list"] = "list"
    data: list[Conversation] = Field(default_factory=list)


class ConversationDeleted(BaseModel):
    id: str
    object: Literal["conversation.deleted"] = "conversation.deleted"
    deleted: bool


class AgentGovConversationItemExtension(BaseModel):
    """AgentGov-owned run context associated with one SDK transcript message."""

    run_id: str
    sdk_session_id: Optional[str] = None
    agent_version_id: Optional[str] = None
    langfuse_trace_id: Optional[str] = None
    langfuse_trace_url: Optional[str] = None


class ConversationItem(BaseModel):
    """会话 item：投影自 SDK transcript 的一条 message（blocks 原样透传：thinking/text/tool_use/tool_result）。"""

    id: str
    object: Literal["conversation.item"] = "conversation.item"
    type: Literal["message"] = "message"
    role: Optional[str] = None
    content: list[JsonObject] = Field(default_factory=list)
    parent_tool_use_id: Optional[str] = None
    agentgov: Optional[AgentGovConversationItemExtension] = None


class ConversationItemList(BaseModel):
    object: Literal["list"] = "list"
    data: list[ConversationItem] = Field(default_factory=list)
    first_id: Optional[str] = None
    last_id: Optional[str] = None
    has_more: bool = False
