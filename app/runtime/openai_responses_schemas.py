"""OpenAI Responses-first 契约模型（AgentGov canonical Chat 接口）。

严格按 ``docs/engineering/OpenAI兼容接口能否替代原生Chat端点评估.md``：

- ``POST /v1/responses`` 采用 OpenAI Responses 外形（``input`` / ``instructions`` / ``store`` /
  ``conversation`` / ``previous_response_id`` / ``metadata``）+ 顶层 ``agentgov`` 强类型扩展。
- ``agentgov`` 是唯一承载「OpenAI 标准字段无法表达的控制面」的地方（业务 Agent 选择、HITL、
  turn cap、raw 调试）；``extra="forbid"`` 堵未知字段。
- ``metadata`` 只放后端不解释、原样回显的观测标签；``alert_id``/``case_id`` 是 backend-owned
  路由输入，走 ``agentgov``（不寄生 opaque metadata）。

本模块只放 pydantic 契约；请求→ChatRequest 映射与响应投影在 ``openai_responses_adapter.py``。
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.runtime.json_types import JsonObject

# 顶层请求默认 extra="ignore"（pydantic 默认）：不 reject 真实 OpenAI 客户端发来的、
# 本项目暂不建模的标准字段（temperature/top_p/tools/reasoning 等）。只有 agentgov 子模型 forbid。


class AgentGovHitl(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    allow_for_run: bool = False


class AgentGovDebug(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sdk_raw: bool = False


class AgentGovRequestExtension(BaseModel):
    """control 模式的 AgentGov 控制面扩展。存在即选中 control 模式。"""

    model_config = ConfigDict(extra="forbid")

    agent_id: Optional[str] = Field(
        default=None,
        description="Business agent to run. Required in control mode; missing -> 422 (no silent main).",
    )
    alert_id: Optional[str] = Field(default=None, description="Feedback-loop routing input (backend-owned).")
    case_id: Optional[str] = Field(default=None, description="Feedback-loop routing input (backend-owned).")
    max_turns: Optional[int] = Field(default=None, ge=1, le=50, description="Claude Code turn cap.")
    hitl: Optional[AgentGovHitl] = None
    debug: Optional[AgentGovDebug] = None


class ResponsesRequest(BaseModel):
    """``POST /v1/responses`` 请求。无 ``agentgov`` = strict 模式；有 = control 模式。"""

    model: Optional[str] = Field(default=None, description="Per-request LLM override only; never an agent handle.")
    input: str | list[JsonObject] = Field(..., description="Prompt string, or Responses input items.")
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
        description="Flat observability tags only (source/client_run_label); backend does not route on these.",
    )
    agentgov: Optional[AgentGovRequestExtension] = Field(default=None, description="Presence selects control mode; carries the non-standard control plane.")


# ---- Response ----


class ResponseOutputText(BaseModel):
    type: Literal["output_text"] = "output_text"
    text: str


class ResponseOutputMessage(BaseModel):
    type: Literal["message"] = "message"
    role: Literal["assistant"] = "assistant"
    content: list[ResponseOutputText] = Field(default_factory=list)


class AgentGovResponseExtension(BaseModel):
    """响应侧 AgentGov 扩展（对称于请求侧顶层 agentgov）。"""

    model_config = ConfigDict(extra="allow")

    run_id: Optional[str] = None
    conversation_id: Optional[str] = None
    session_id: Optional[str] = None
    sdk_session_id: Optional[str] = None
    agent_id: Optional[str] = None
    agent_version_id: Optional[str] = None
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
    output: list[ResponseOutputMessage] = Field(default_factory=list)
    usage: Optional[JsonObject] = None
    metadata: JsonObject = Field(default_factory=dict)
    agentgov: AgentGovResponseExtension


# ---- Conversations（会话对象，投影自 SDK session / transcript，不另建副本）----


class ConversationCreateRequest(BaseModel):
    metadata: JsonObject = Field(default_factory=dict, description="Flat observability tags (backend does not route on these).")


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


class ConversationItem(BaseModel):
    """会话 item：投影自 SDK transcript 的一条 message（blocks 原样透传：thinking/text/tool_use/tool_result）。"""

    id: str
    object: Literal["conversation.item"] = "conversation.item"
    type: Literal["message"] = "message"
    role: Optional[str] = None
    content: list[JsonObject] = Field(default_factory=list)
    parent_tool_use_id: Optional[str] = None


class ConversationItemList(BaseModel):
    object: Literal["list"] = "list"
    data: list[ConversationItem] = Field(default_factory=list)
    first_id: Optional[str] = None
    last_id: Optional[str] = None
    has_more: bool = False
