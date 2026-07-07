from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any


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
