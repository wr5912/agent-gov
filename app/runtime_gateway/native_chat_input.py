"""复用 AgentScope 公开输入模型，并保留 SDK 填默认值前的请求身份。"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any, TypeAlias

from agentscope.event import ExternalExecutionResultEvent, UserConfirmResultEvent
from agentscope.message import Msg
from pydantic import BaseModel, ConfigDict, Field, ModelWrapValidatorHandler, PrivateAttr, model_validator

from app.runtime.json_types import JsonValue

from .operation_identity import RuntimeChatOperationKind, canonical_request_fingerprint

NativeChatInput: TypeAlias = Msg | list[Msg] | UserConfirmResultEvent | ExternalExecutionResultEvent | None
GOVERNED_EVIDENCE_ROOT_METADATA_KEY = "agentgov_governed_evidence_root"


class RuntimeChatRequest(BaseModel):
    """公开请求仅保留原生三字段；生成默认 id 不得变成重试身份。"""

    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1, max_length=128, description="AgentScope runtime_agent_id pinned by the target Session")
    session_id: str = Field(min_length=1, max_length=128)
    input: NativeChatInput
    _raw_input: JsonValue = PrivateAttr(default=None)

    @model_validator(mode="wrap")
    @classmethod
    def preserve_native_input(cls, value: Any, handler: ModelWrapValidatorHandler[RuntimeChatRequest]) -> RuntimeChatRequest:
        raw_input = deepcopy(value.get("input")) if isinstance(value, Mapping) else None
        if _contains_governed_evidence_root(raw_input):
            raise ValueError(f"{GOVERNED_EVIDENCE_ROOT_METADATA_KEY} is reserved for trusted backend injection")
        result = handler(value)
        if isinstance(value, Mapping):
            # Event.type 是 SDK 的确定性默认值；仅补它，绝不补随机 id/时间戳。
            # 否则合法的省略 type 的 HITL 输入会被误认为新一轮聊天。
            if isinstance(raw_input, dict) and isinstance(result.input, (UserConfirmResultEvent, ExternalExecutionResultEvent)):
                raw_input.setdefault("type", result.input.type)
            result._raw_input = raw_input
        return result

    @property
    def raw_input(self) -> JsonValue:
        return deepcopy(self._raw_input)


def _contains_governed_evidence_root(value: object) -> bool:
    if isinstance(value, Mapping):
        return GOVERNED_EVIDENCE_ROOT_METADATA_KEY in value or any(_contains_governed_evidence_root(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_governed_evidence_root(item) for item in value)
    return False


def native_operation_kind(input_value: object) -> RuntimeChatOperationKind:
    event_type = input_value.get("type") if isinstance(input_value, dict) else None
    if event_type == "USER_CONFIRM_RESULT":
        return RuntimeChatOperationKind.USER_CONFIRMATION
    if event_type == "EXTERNAL_EXECUTION_RESULT":
        return RuntimeChatOperationKind.EXTERNAL_EXECUTION
    return RuntimeChatOperationKind.INITIAL


def explicit_native_input_ids(input_value: object) -> tuple[str, ...] | None:
    """每个输入都显式携带 id 才支持查重；None/缺失 id 是单次提交。"""
    items = input_value if isinstance(input_value, list) else [input_value]
    ids = tuple(item.get("id") if isinstance(item, dict) else None for item in items)
    if not ids or any(not isinstance(value, str) or not value for value in ids):
        return None
    return tuple(str(value) for value in ids)


def native_operation_key(
    *,
    runtime_agent_id: str,
    session_id: str,
    operation_kind: RuntimeChatOperationKind,
    input_ids: tuple[str, ...],
) -> str:
    """身份取决于已授权 Session 绑定与有序原生 ID，不按正文去重。"""
    if not input_ids or any(not value for value in input_ids):
        raise ValueError("Input identity requires explicit native IDs")
    digest = canonical_request_fingerprint(
        {
            "runtime_agent_id": runtime_agent_id,
            "session_id": session_id,
            "operation_kind": operation_kind.value,
            "input_ids": input_ids,
        }
    )
    return f"native:{digest}"
