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
    """Control-mode debug switches. These fields are never accepted in strict mode."""

    model_config = ConfigDict(extra="forbid")

    sdk_raw: bool = Field(
        default=False,
        description=(
            "Control streaming only. When true, emit AgentGov-wrapped SDK raw facts for debugging; "
            "the value does not change the model request or the standard response.* projection."
        ),
        examples=[True],
    )


class AgentGovRequestExtension(BaseModel):
    """control 模式的 AgentGov 控制面扩展。存在即选中 control 模式。"""

    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(
        ...,
        min_length=1,
        pattern=NON_BLANK_TEXT_PATTERN,
        description="Business agent to run in control mode. Must contain at least one non-whitespace character.",
        examples=["security-operations-expert"],
    )
    alert_id: Optional[str] = Field(
        default=None,
        description="Optional SOC alert id used as backend-owned feedback-loop routing input.",
        examples=["alert-20260729-001"],
    )
    case_id: Optional[str] = Field(
        default=None,
        description="Optional SOC case id used as backend-owned feedback-loop routing input.",
        examples=["case-20260729-001"],
    )
    max_turns: Optional[int] = Field(
        default=None,
        ge=1,
        le=50,
        description="Claude Code turn cap for this request; omit to use the operator-configured default.",
        examples=[8],
    )
    include_trace: bool = Field(
        default=False,
        description="Emit complete semantic SDK facts as agentgov.trace_event envelopes.",
        examples=[True],
    )
    with_speech_summary: bool = Field(
        default=False,
        description=(
            "Control streaming only; defaults to false. true requires the top-level stream=true or the API "
            "returns 422. When enabled, eligible top-level thinking/assistant boundaries may emit best-effort "
            "agentgov.speech_summary SSE events; generation failure is silent and no event is guaranteed."
        ),
        examples=[True],
    )
    debug: Optional[AgentGovDebug] = Field(
        default=None,
        description="Optional control-stream debugging switches; omit for normal business traffic.",
        examples=[{"sdk_raw": True}],
    )


class ResponsesInputText(BaseModel):
    """One typed text content block inside a Responses input message."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["input_text"] = Field(
        default="input_text",
        description="Discriminator for a text input content block.",
        examples=["input_text"],
    )
    text: str = Field(
        min_length=1,
        pattern=NON_BLANK_TEXT_PATTERN,
        description="Non-blank text carried by this input content block.",
        examples=["请复核该告警的处置结论"],
    )

    @field_validator("text")
    @classmethod
    def _non_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("input_text must contain non-whitespace text")
        return value


class ResponsesInputMessage(BaseModel):
    """Typed message item accepted by the transitional Responses input array."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["message"] = Field(
        default="message",
        description="Discriminator for an input message item.",
        examples=["message"],
    )
    role: Literal["developer", "system", "user", "assistant"] = Field(
        description=(
            "Message role. At least one user message with non-blank text is required in the complete input array; "
            "only user-message text is mapped to the current Agent prompt."
        ),
        examples=["user"],
    )
    content: Annotated[str, Field(min_length=1, pattern=NON_BLANK_TEXT_PATTERN)] | Annotated[list[ResponsesInputText], Field(min_length=1)] = Field(
        description="Non-blank message text or a non-empty array of typed input_text blocks.",
        examples=[
            "请复核该告警的处置结论",
            [{"type": "input_text", "text": "请复核该告警的处置结论"}],
        ],
    )

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

    model: Optional[str] = Field(
        default=None,
        description="Per-request LLM override only; never a business Agent handle. Omit to use the Agent profile.",
        examples=["claude-sonnet-4-5"],
    )
    input: Annotated[str, Field(min_length=1, pattern=NON_BLANK_TEXT_PATTERN)] | Annotated[list[ResponsesInputMessage], Field(min_length=1)] = Field(
        ...,
        description="Non-empty prompt string, or typed text message items containing a current user message.",
        examples=[
            "请核查当前告警并给出处置建议",
            [{"type": "message", "role": "user", "content": "请核查当前告警并给出处置建议"}],
        ],
    )
    instructions: Optional[str] = Field(
        default=None,
        description=(
            "OpenAI standard field NAME. In AgentGov this is APPEND-ONLY (mapped to system_append, appended to the "
            "Claude Code preset + workspace CLAUDE.md), which differs from OpenAI replace/swap semantics. "
            "Rejected (422) on the strict surface."
        ),
        examples=["补充说明证据不足的判断，不替换业务 Agent 的受治理指令。"],
    )
    stream: bool = Field(
        default=False,
        description=(
            "false returns one JSON ResponseObject; true returns Responses-style SSE. agentgov.with_speech_summary=true is valid only when this field is true."
        ),
        examples=[True],
    )
    store: bool = Field(
        default=True,
        description=(
            "Whether the response remains retrievable through GET /v1/responses/{response_id}. "
            "false disables public retrieval but does not remove internal audit evidence."
        ),
        examples=[False],
    )
    conversation: Optional[str] = Field(
        default=None,
        description=(
            "AgentGov conversation projection (normally conv_<session_id>) used to continue that server session. "
            "Prefer this or previous_response_id alone. If both are supplied, AgentGov currently accepts them only "
            "when they resolve to the same conversation; this is a documented OpenAI compatibility deviation."
        ),
        examples=["conv_sess-20260729"],
    )
    previous_response_id: Optional[str] = Field(
        default=None,
        description=(
            "Previous AgentGov response id (resp_<run_id>) whose owning conversation should be continued. "
            "Returns 404 when the response is unknown and 409 when its conversation is unavailable or conflicts "
            "with an explicit conversation. Prefer this or conversation alone."
        ),
        examples=["resp_run-20260729-001"],
    )
    metadata: JsonObject = Field(
        default_factory=dict,
        description=(
            "AgentGov transitional metadata object. Values may be nested JSON; backend-reserved keys are removed "
            "before public echo and the backend does not route on remaining entries."
        ),
        examples=[{"source": "soc-console", "tenant": "north-region"}],
    )
    agentgov: Optional[AgentGovRequestExtension] = Field(
        default=None,
        description=(
            "AgentGov control-plane extension. Omit it for strict mode; when present, agent_id is required and "
            "control-only trace, debug, feedback routing, and speech-summary switches become available."
        ),
        examples=[
            {
                "agent_id": "security-operations-expert",
                "include_trace": True,
                "with_speech_summary": True,
                "debug": {"sdk_raw": True},
            }
        ],
    )

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
