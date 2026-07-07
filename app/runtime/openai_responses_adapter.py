"""OpenAI Responses 接口的映射/投影层（薄投影，单一真相源在 SDK/agent）。

集中放：``ResponsesRequest`` -> ``ChatRequest`` 映射、``ChatResponse`` / 持久化 run -> ``ResponseObject``
投影、status 派生、usage 映射、conversation<->session id 转换。路由只做鉴权/模式判定/编排，
投影逻辑在这里保持单一来源（避免状态字面量与投影重复分散）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from app.runtime.json_types import JsonObject
from app.runtime.message_utils import extract_answer_from_messages
from app.runtime.openai_responses_schemas import (
    AgentGovResponseExtension,
    ResponseObject,
    ResponseOutputMessage,
    ResponseOutputText,
    ResponsesRequest,
    ResponseStatus,
)
from app.runtime.schemas import ChatRequest, ChatResponse

_CONVERSATION_PREFIX = "conv_"
_RESPONSE_PREFIX = "resp_"

# 持久化到 run metadata 的保留 backend 标记，仅内部用（永不回显给客户端）。
# store=false 时打上，retrieve 据此对公开 GET 返回 404（内部审计仍保留 run）。
_RESERVED_PREFIX = "__agentgov"
STORE_MARKER_KEY = "__agentgov_store__"


def public_metadata(metadata: object) -> JsonObject:
    """回显给客户端的 metadata，剥掉所有保留 backend 标记。"""
    if not isinstance(metadata, dict):
        return {}
    return {k: v for k, v in metadata.items() if not str(k).startswith(_RESERVED_PREFIX)}


def store_disabled(run: JsonObject) -> bool:
    meta = run.get("metadata")
    return isinstance(meta, dict) and meta.get(STORE_MARKER_KEY) is False


def conversation_id_from_session(session_id: Optional[str]) -> Optional[str]:
    return f"{_CONVERSATION_PREFIX}{session_id}" if session_id else None


def session_id_from_conversation(conversation: Optional[str]) -> Optional[str]:
    """``conv_<session_id>`` -> ``<session_id>``；无前缀时按原值当 session id 容错。"""
    if not conversation:
        return None
    if conversation.startswith(_CONVERSATION_PREFIX):
        return conversation[len(_CONVERSATION_PREFIX) :] or None
    return conversation


def response_id_from_run(run_id: Optional[str]) -> Optional[str]:
    return f"{_RESPONSE_PREFIX}{run_id}" if run_id else None


def run_id_from_response(response_id: Optional[str]) -> Optional[str]:
    if not response_id:
        return None
    if response_id.startswith(_RESPONSE_PREFIX):
        return response_id[len(_RESPONSE_PREFIX) :] or None
    return response_id


def extract_input_text(value: object) -> str:
    """把 Responses ``input``（字符串或 items 数组）取成本轮 Claude Code prompt 文本。"""
    if isinstance(value, str):
        return value
    parts: list[str] = []
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and isinstance(block.get("text"), str):
                        parts.append(block["text"])
            elif isinstance(item.get("text"), str):
                parts.append(item["text"])
    return "\n".join(part for part in parts if part)


def build_chat_request(
    req: ResponsesRequest,
    *,
    agent_id: Optional[str],
    system_append: Optional[str],
    session_id: Optional[str],
) -> ChatRequest:
    """映射到内部 ``ChatRequest``。alert_id/case_id 来自 agentgov（backend-owned），
    metadata 原样透传（观测标签）。runtime.run/stream 以 profile 为准，agent_id 仅作记录一致。
    ``store=false`` 时给持久化 metadata 打保留标记（不影响回显）。"""
    ext = req.agentgov
    # 先剥掉客户端可能塞进来的保留 backend key（防伪造 store 标记），再由服务端按 store 打标。
    metadata = public_metadata(req.metadata)
    if not req.store:
        metadata[STORE_MARKER_KEY] = False
    return ChatRequest(
        message=extract_input_text(req.input),
        session_id=session_id,
        agent_id=agent_id,
        alert_id=ext.alert_id if ext else None,
        case_id=ext.case_id if ext else None,
        max_turns=ext.max_turns if ext else None,
        model=req.model,
        system_append=system_append,
        metadata=metadata,
    )


def derive_status(errors: object, stop_reason: object = None) -> ResponseStatus:
    """单一 status 派生（agent_runs 无 status 列、run 仅完成时落库 -> 只有终态）。

    有 errors -> ``failed``；否则 ``completed``。``incomplete`` 保留给未来（如 max_turns 截断，
    当前不承诺）。stop_reason 作为将来细分的入参占位。
    """
    if isinstance(errors, list) and len(errors) > 0:
        return "failed"
    return "completed"


def map_usage(raw: object) -> Optional[JsonObject]:
    """把 Claude SDK usage 形状映射成 OpenAI-ish ``{input_tokens, output_tokens, total_tokens}``；
    无法识别时原样返回，raw 另存 ``agentgov.usage``。"""
    if not isinstance(raw, dict):
        return None
    inp = raw.get("input_tokens")
    out = raw.get("output_tokens")
    total = raw.get("total_tokens")
    if total is None and (isinstance(inp, int) or isinstance(out, int)):
        total = (inp or 0) + (out or 0)
    mapped: JsonObject = {}
    if inp is not None:
        mapped["input_tokens"] = inp
    if out is not None:
        mapped["output_tokens"] = out
    if total is not None:
        mapped["total_tokens"] = total
    return mapped or dict(raw)


def _iso_to_epoch(value: object) -> Optional[int]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _output_from_text(text: Optional[str]) -> list[ResponseOutputMessage]:
    if not text:
        return []
    return [ResponseOutputMessage(content=[ResponseOutputText(text=text)])]


def response_from_chat_response(
    chat: ChatResponse,
    *,
    model: Optional[str],
    agent_id: Optional[str],
    metadata: JsonObject,
    created_at: Optional[int] = None,
) -> ResponseObject:
    """live 非流式：``runtime.run`` 的 ``ChatResponse`` -> ``ResponseObject``。"""
    answer = chat.answer or ""
    return ResponseObject(
        id=response_id_from_run(chat.run_id) or chat.run_id,
        created_at=created_at,
        status=derive_status(chat.errors, chat.stop_reason),
        model=model,
        output=_output_from_text(answer),
        usage=map_usage(chat.usage),
        metadata=dict(metadata or {}),
        agentgov=AgentGovResponseExtension(
            run_id=chat.run_id,
            conversation_id=conversation_id_from_session(chat.session_id),
            session_id=chat.session_id,
            sdk_session_id=chat.sdk_session_id,
            agent_id=agent_id,
            agent_version_id=chat.agent_version_id,
            output_text=answer or None,
            agent_activity=chat.agent_activity or {},
            usage=chat.usage,
            total_cost_usd=chat.total_cost_usd,
            stop_reason=chat.stop_reason,
            errors=list(chat.errors or []),
        ),
    )


def _str_or_none(value: object) -> Optional[str]:
    return value if isinstance(value, str) else None


def response_from_run_payload(run: JsonObject) -> ResponseObject:
    """retrieve：持久化 run（``feedback_store.find_run``）-> ``ResponseObject``。

    权威 output_text 从 ``messages`` 重建（非截断 ``answer_summary``）；status 由 errors/stop_reason 派生。
    """
    run_id = str(run.get("run_id") or "")
    raw_messages = run.get("messages")
    messages = raw_messages if isinstance(raw_messages, list) else []
    answer = run.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        answer = extract_answer_from_messages(messages) or ""
    errors = run.get("errors")
    usage = run.get("usage")
    cost = run.get("total_cost_usd")
    return ResponseObject(
        id=response_id_from_run(run_id) or run_id,
        created_at=_iso_to_epoch(run.get("created_at")),
        status=derive_status(errors, run.get("stop_reason")),
        model=_str_or_none(run.get("model")),
        output=_output_from_text(answer or None),
        usage=map_usage(usage),
        metadata=public_metadata(run.get("metadata")),
        agentgov=AgentGovResponseExtension(
            run_id=run_id or None,
            conversation_id=conversation_id_from_session(_str_or_none(run.get("session_id"))),
            session_id=_str_or_none(run.get("session_id")),
            sdk_session_id=_str_or_none(run.get("sdk_session_id")),
            agent_id=_str_or_none(run.get("agent_id")),
            agent_version_id=_str_or_none(run.get("agent_version_id")),
            trace_id=_str_or_none(run.get("langfuse_trace_id")),
            output_text=answer or None,
            agent_activity=run.get("agent_activity") if isinstance(run.get("agent_activity"), dict) else {},
            usage=usage if isinstance(usage, dict) else None,
            total_cost_usd=cost if isinstance(cost, (int, float)) else None,
            stop_reason=_str_or_none(run.get("stop_reason")),
            errors=list(errors) if isinstance(errors, list) else [],
        ),
    )
