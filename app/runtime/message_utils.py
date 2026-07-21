from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

STREAM_TEXT_DIVERGED = "STREAM_TEXT_DIVERGED"


class StreamTextDivergedError(RuntimeError):
    """流式 delta 与 SDK 最终 AssistantMessage 快照不一致。"""

    error_code = STREAM_TEXT_DIVERGED

    def __init__(self, *, partial: str, snapshot: str | None) -> None:
        self.error_details = {
            "reason": "missing_final_snapshot" if snapshot is None else "final_snapshot_mismatch",
            "partial_length": len(partial),
            "snapshot_length": len(snapshot) if snapshot is not None else None,
        }
        super().__init__("SDK text deltas diverged from the final AssistantMessage snapshot")


def to_plain(obj: Any) -> Any:
    """Best-effort conversion of SDK message dataclasses to JSON-safe dicts."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [to_plain(v) for v in obj]
    if is_dataclass(obj):
        return to_plain(asdict(obj))
    if hasattr(obj, "__dict__"):
        return {k: to_plain(v) for k, v in vars(obj).items() if not k.startswith("_")}
    return str(obj)


def extract_text(message: Any) -> str:
    """Extract assistant text/result text from Claude Agent SDK messages."""
    pieces: list[str] = []

    # ResultMessage.result
    result = message.get("result") if isinstance(message, dict) else getattr(message, "result", None)
    if isinstance(result, str) and result.strip():
        pieces.append(result)

    # AssistantMessage.content -> TextBlock.text
    content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
    if isinstance(content, list):
        for block in content:
            text = getattr(block, "text", None)
            if isinstance(text, str) and text.strip():
                pieces.append(text)
            elif isinstance(block, dict):
                value = block.get("text")
                if isinstance(value, str) and value.strip():
                    pieces.append(value)

    return "\n".join(pieces).strip()


def extract_stream_text_delta(message: Any) -> str | None:
    """只读取 SDK ``StreamEvent.content_block_delta.text_delta``，保留原始空白。"""
    if message.__class__.__name__ != "StreamEvent":
        return None
    event = getattr(message, "event", None)
    if not isinstance(event, dict) or event.get("type") != "content_block_delta":
        return None
    delta = event.get("delta")
    if not isinstance(delta, dict) or delta.get("type") != "text_delta":
        return None
    text = delta.get("text")
    return text if isinstance(text, str) and text else None


def extract_assistant_text_snapshot(message: Any) -> str | None:
    """读取最终 AssistantMessage 的原始文本快照，不裁剪空白或注入分隔符。"""
    if not message.__class__.__name__.startswith("AssistantMessage"):
        return None
    content = getattr(message, "content", None)
    if not isinstance(content, list):
        return None
    pieces: list[str] = []
    for block in content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            pieces.append(text)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            pieces.append(block["text"])
    return "".join(pieces) if pieces else None


def reconcile_stream_snapshot(partial: str, snapshot: str | None) -> str:
    """返回最终快照尚未发送的后缀；不一致时 fail-closed。"""
    if not partial:
        return snapshot or ""
    if snapshot is None or not snapshot.startswith(partial):
        raise StreamTextDivergedError(partial=partial, snapshot=snapshot)
    return snapshot[len(partial) :]


def message_event_name(message: Any) -> str:
    cls = message.__class__.__name__
    subtype = getattr(message, "subtype", None)
    if subtype:
        return f"{cls}:{subtype}"
    return cls


def extract_answer_from_messages(messages: list[Any]) -> str | None:
    """Reconstruct the assistant answer from a persisted SDK message timeline.

    Prefers AssistantMessage text; falls back to ResultMessage / other text. Shared by
    the feedback-workbench agent-run view and the OpenAI Responses projection so both
    rebuild answers from the same single source of truth (the full ``messages`` list),
    not the truncated ``answer_summary``.
    """
    assistant_parts: list[str] = []
    fallback_parts: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        text = extract_text(message)
        if not text:
            continue
        event = str(message.get("event") or message.get("type") or message.get("role") or "")
        if event.startswith("AssistantMessage") or event == "assistant":
            assistant_parts.append(text)
        else:
            fallback_parts.append(text)
    answer = "\n\n".join(assistant_parts or fallback_parts).strip()
    return answer or None
