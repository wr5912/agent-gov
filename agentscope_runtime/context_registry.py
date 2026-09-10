"""进程内保存 reply 与 AgentGov run 的稳定关联。

AgentScope 在完成一批 Message 持久化后才异步发送回执。回执重试期间，
同一 Session 可能已经开始下一次 run，因此不能再次按 Session 查询“当前”
run。这里在原生事件产生时固定 reply -> RuntimeContext，供 Storage 生成不可
重绑定的持久化回执。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RuntimeContext(BaseModel):
    """AgentGov 为当前 AgentScope Session 持有的权威运行上下文。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    root_session_id: str = Field(min_length=1)
    role: Literal["root", "worker"]
    agent_id: str = Field(min_length=1)
    agent_version_id: str = Field(min_length=1)
    runtime_agent_id: str = Field(min_length=1)
    harness_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    team_generation: int = Field(ge=0)

    @field_validator("trace_id")
    @classmethod
    def _trace_id_must_be_valid(cls, value: str) -> str:
        if int(value, 16) == 0:
            raise ValueError("trace_id must be a non-zero 128-bit hexadecimal id")
        return value


_REPLY_CONTEXTS: dict[tuple[str, str], RuntimeContext] = {}


def bind_reply_context(context: RuntimeContext, reply_id: str) -> None:
    """首次观察 reply 时固定上下文；禁止相同身份静默换绑。"""

    key = (context.session_id, reply_id)
    existing = _REPLY_CONTEXTS.get(key)
    if existing is not None and existing != context:
        raise RuntimeError("AgentScope reply is already bound to another AgentGov run")
    _REPLY_CONTEXTS[key] = context


def take_reply_context(session_id: str, reply_id: str) -> RuntimeContext | None:
    """由持久化层一次性取走绑定；后续重试持有同一不可变对象。"""

    return _REPLY_CONTEXTS.pop((session_id, reply_id), None)


def discard_reply_contexts(context: RuntimeContext, reply_ids: list[str]) -> None:
    """批次回执已捕获上下文后清理 registry，且不删除后来者的绑定。"""

    for reply_id in reply_ids:
        key = (context.session_id, reply_id)
        if _REPLY_CONTEXTS.get(key) == context:
            _REPLY_CONTEXTS.pop(key, None)
