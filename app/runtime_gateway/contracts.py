from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Literal, Protocol, TypeAlias

from agentgov_run_permission import is_bounded_run_path_rule
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.runtime.json_types import JsonObject

RuntimeReceiptPayload: TypeAlias = JsonObject
RuntimeToolResultState: TypeAlias = Literal["success", "error", "interrupted", "denied", "running"]
RuntimeToolCallState: TypeAlias = Literal["pending", "asking", "allowed", "submitted", "finished"]
RuntimeTraceActionStatus: TypeAlias = Literal["pending", "resolved", "expired"]
GOVERNED_EVIDENCE_ROOT_METADATA_KEY = "agentgov_governed_evidence_root"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    WAITING_EXTERNAL = "waiting_external"
    FINALIZING = "finalizing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class ConfirmationScope(StrEnum):
    ONCE = "once"
    RUN = "run"


class _SuggestedPermissionRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(min_length=1, max_length=256)
    rule_content: str | None = Field(max_length=1024)
    behavior: Literal["allow"]
    source: str = Field(min_length=1, max_length=128)


ACTIVE_RUN_STATUSES = frozenset(
    {
        RunStatus.QUEUED,
        RunStatus.RUNNING,
        RunStatus.WAITING_HUMAN,
        RunStatus.WAITING_EXTERNAL,
        RunStatus.FINALIZING,
    }
)
TERMINAL_RUN_STATUSES = frozenset({RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.INTERRUPTED})

ALLOWED_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset({RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.INTERRUPTED}),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.WAITING_HUMAN,
            RunStatus.WAITING_EXTERNAL,
            RunStatus.FINALIZING,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.INTERRUPTED,
        }
    ),
    RunStatus.WAITING_HUMAN: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED, RunStatus.INTERRUPTED, RunStatus.FAILED}),
    RunStatus.WAITING_EXTERNAL: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED, RunStatus.INTERRUPTED, RunStatus.FAILED}),
    # AgentScope 的一个 ChatService run 可以在同一 Session lock 中产生多条
    # reply；首条 REPLY_END 后可能出现下一条 REPLY_START。
    RunStatus.FINALIZING: frozenset(
        {
            RunStatus.RUNNING,
            RunStatus.WAITING_HUMAN,
            RunStatus.WAITING_EXTERNAL,
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.INTERRUPTED,
        },
    ),
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
    RunStatus.INTERRUPTED: frozenset(),
}


class RuntimeSessionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(
        min_length=1,
        max_length=128,
        description="AgentScope runtime_agent_id of the exact current published version",
    )
    name: str | None = Field(default=None, max_length=512)


class RuntimeCurrentVersionResponse(BaseModel):
    governance_agent_id: str
    agent_version_id: str
    harness_digest: str
    runtime_agent_id: str | None
    provisioned: bool


class RuntimeChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(
        min_length=1,
        max_length=128,
        description="AgentScope runtime_agent_id pinned by the target Session",
    )
    session_id: str = Field(min_length=1, max_length=128)
    client_operation_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
        description="Stable client-side idempotency identity for this logical turn",
    )
    input: Any
    confirmation_scope: ConfirmationScope = ConfirmationScope.ONCE
    expected_run_id: str | None = Field(default=None, min_length=1, max_length=128)
    alert_id: str | None = Field(default=None, max_length=256)
    case_id: str | None = Field(default=None, max_length=256)
    metadata: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def _scope_only_applies_to_user_confirmation(self) -> RuntimeChatRequest:
        if _contains_governed_evidence_root(self.input) or _contains_governed_evidence_root(self.metadata):
            raise ValueError(
                f"{GOVERNED_EVIDENCE_ROOT_METADATA_KEY} is reserved for trusted backend injection",
            )
        confirmation = is_confirmation_input(self.input)
        if confirmation and self.expected_run_id is None:
            raise ValueError("expected_run_id is required for a HITL continuation")
        if not confirmation and self.expected_run_id is not None:
            raise ValueError("expected_run_id only applies to a HITL continuation")
        if self.confirmation_scope is ConfirmationScope.RUN and (not isinstance(self.input, dict) or self.input.get("type") != "USER_CONFIRM_RESULT"):
            raise ValueError("confirmation_scope=run only applies to USER_CONFIRM_RESULT")
        return self


def _contains_governed_evidence_root(value: object) -> bool:
    if isinstance(value, Mapping):
        return GOVERNED_EVIDENCE_ROOT_METADATA_KEY in value or any(_contains_governed_evidence_root(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_governed_evidence_root(item) for item in value)
    return False


class RuntimeReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    receipt_id: str = Field(min_length=1, max_length=128)
    event_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    reply_id: str | None = Field(default=None, max_length=128)
    type: str = Field(min_length=1, max_length=64)
    payload: RuntimeReceiptPayload = Field(default_factory=dict)
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    trace_url: str | None = Field(default=None, max_length=2048)


class RuntimeContextResponse(BaseModel):
    run_id: str
    session_id: str
    root_session_id: str
    role: Literal["root", "worker"]
    agent_id: str
    agent_version_id: str
    runtime_agent_id: str
    harness_digest: str
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    team_generation: int = Field(ge=0)


class RuntimeChildSessionRegistration(BaseModel):
    """Runtime 在唤醒 AgentScope Team worker 前建立的受控绑定。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, max_length=128)
    parent_session_id: str = Field(min_length=1, max_length=128)
    child_session_id: str = Field(min_length=1, max_length=128)
    child_runtime_agent_id: str = Field(min_length=1, max_length=128)
    team_id: str = Field(min_length=1, max_length=128)


class RuntimeTeamInboxDelivery(BaseModel):
    """一个将在持久化 fence 后进入 AgentScope inbox 的跨 Session 消息。"""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    source_session_id: str = Field(min_length=1, max_length=128)
    target_session_id: str = Field(min_length=1, max_length=128)


class RuntimeTeamInboxAck(BaseModel):
    run_id: str
    event_id: str
    generation: int = Field(ge=1)


class RuntimeBootAnnouncement(BaseModel):
    """独立 AgentScope Runtime 进程启动代际通知。"""

    model_config = ConfigDict(extra="forbid")

    boot_id: str = Field(min_length=1, max_length=128)
    runtime_version: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9.+_-]*$",
    )


class RuntimeBootAck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    boot_id: str
    runtime_version: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9.+_-]*$",
    )
    recovery_run_ids: list[str] = Field(default_factory=list)


class AgentRunResponse(BaseModel):
    run_id: str
    session_id: str
    client_operation_id: str | None = None
    agent_id: str
    agent_version_id: str
    runtime_agent_id: str
    harness_digest: str
    status: RunStatus
    reply_ids: list[str] = Field(default_factory=list)
    persisted_reply_ids: list[str] = Field(default_factory=list)
    persistence_batch_reply_ids: list[str] = Field(default_factory=list)
    team_generation: int = 0
    root_persisted_team_generation: int = 0
    pending_child_session_ids: list[str] = Field(default_factory=list)
    trace_id: str | None = None
    trace_url: str | None = None
    trace_status: Literal["pending", "complete", "incomplete"] = "pending"
    terminal_reason: str | None = None
    error: JsonObject | None = None
    alert_id: str | None = None
    case_id: str | None = None
    metadata: JsonObject = Field(default_factory=dict)
    created_at: str
    started_at: str | None = None
    updated_at: str
    completed_at: str | None = None


class RuntimeToolCallFingerprint(BaseModel):
    """完整 canonical AgentScope ToolCall 的无正文指纹。"""

    model_config = ConfigDict(extra="forbid")

    tool_call_id: str = Field(min_length=1, max_length=128)
    tool_call_name: str = Field(min_length=1, max_length=256)
    tool_call_state: RuntimeToolCallState
    tool_call_utf8_length: int = Field(gt=0)
    tool_call_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RuntimePendingActionResponse(RuntimeToolCallFingerprint):
    """未决 action 的无正文投影；原始 ToolCall 只从 AgentScope 获取。"""

    action_id: str
    session_id: str
    run_id: str
    reply_id: str
    kind: Literal["human", "external"]
    status: Literal["pending"]
    created_at: str


class RuntimeTraceTeamChildExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    runtime_agent_id: str | None


class RuntimeTraceToolExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    reply_id: str
    tool_call_id: str
    state: RuntimeToolResultState | None
    source: Literal["tool_result_receipt", "external_action"]


class RuntimeTraceActionExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    reply_id: str
    tool_call_id: str
    kind: Literal["human", "external"]
    status: RuntimeTraceActionStatus


class RuntimeTraceExpectations(BaseModel):
    """只能从 AgentGov durable ledger 构建的 Trace 完整性预期。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    root_session_id: str
    root_reply_ids: list[str] = Field(default_factory=list)
    team_children: list[RuntimeTraceTeamChildExpectation] = Field(default_factory=list)
    tool_results: list[RuntimeTraceToolExpectation] = Field(default_factory=list)
    actions: list[RuntimeTraceActionExpectation] = Field(default_factory=list)
    interrupted_before_reply: bool = False
    control_integrity_complete: bool = True


class AgentRunTraceResponse(BaseModel):
    """公开 Trace 状态只暴露受管身份和链接，不返回 Langfuse 原始 payload。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    trace_id: str | None
    trace_url: str | None
    trace_status: Literal["pending", "complete", "incomplete"]


class RuntimeInterruptResponse(BaseModel):
    run_id: str | None
    session_id: str
    status: RunStatus | None


class RuntimeGatewayResponse(Protocol):
    status_code: int
    headers: Mapping[str, str]
    body: object


class RuntimeGateway(Protocol):
    async def request_json(self, method: str, path: str, **kwargs: Any) -> RuntimeGatewayResponse: ...

    async def stream(self, path: str, **kwargs: Any) -> Any: ...


def is_confirmation_input(value: Any) -> bool:
    return isinstance(value, dict) and value.get("type") in {"USER_CONFIRM_RESULT", "EXTERNAL_EXECUTION_RESULT"}


def confirmation_reply_id(value: Any) -> str | None:
    if not is_confirmation_input(value):
        return None
    reply_id = value.get("reply_id")
    return reply_id if isinstance(reply_id, str) and reply_id else None


def validate_no_permission_rules(value: Any) -> None:
    """浏览器不能提交可持久化权限规则。"""

    if not is_confirmation_input(value):
        return
    results = value.get("confirm_results")
    if not isinstance(results, list):
        return
    for result in results:
        if isinstance(result, dict) and result.get("rules") not in (None, []):
            raise ValueError("permission rules are controlled by AgentGov and cannot be submitted by clients")


def governed_run_permission_rules(tool_call: JsonObject, run_id: str) -> list[JsonObject]:
    """只从 AgentScope 已持久化的建议生成当前 run 的 allow rules。"""

    suggested = tool_call.get("suggested_rules")
    tool_name = tool_call.get("name")
    if not isinstance(suggested, list) or not suggested or not isinstance(tool_name, str):
        raise ValueError("Run-scoped approval requires AgentScope suggested permission rules")
    governed: list[JsonObject] = []
    seen: set[tuple[str, str | None]] = set()
    for value in suggested:
        try:
            rule = _SuggestedPermissionRule.model_validate(value)
        except ValidationError as exc:
            raise ValueError("AgentScope suggested permission rule is invalid") from exc
        if rule.tool_name != tool_name:
            raise ValueError("AgentScope suggested permission rule does not match the pending tool")
        if rule.source != "workspace_policy.ask_tools":
            raise ValueError("Run-scoped approval requires an AgentGov governed suggestion source")
        if not is_bounded_run_path_rule(rule.tool_name, rule.rule_content):
            raise ValueError("Run-scoped approval requires a bounded AgentScope permission rule")
        identity = (rule.tool_name, rule.rule_content)
        if identity in seen:
            continue
        seen.add(identity)
        governed.append(
            rule.model_copy(update={"source": f"agentgov-run:{run_id}"}).model_dump(mode="json"),
        )
    return governed


def require_non_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("value must contain non-whitespace text")
    return value
