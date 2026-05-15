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
    result = getattr(message, "result", None)
    if isinstance(result, str) and result.strip():
        pieces.append(result)

    # AssistantMessage.content -> TextBlock.text
    content = getattr(message, "content", None)
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


def extract_stream_event_text(message: Any) -> str:
    """Extract token-level text deltas from Claude Agent SDK StreamEvent."""
    event = getattr(message, "event", None)
    if not isinstance(event, dict):
        return ""

    if event.get("type") != "content_block_delta":
        return ""

    delta = event.get("delta")
    if not isinstance(delta, dict):
        return ""

    if delta.get("type") != "text_delta":
        return ""

    text = delta.get("text")
    return text if isinstance(text, str) else ""


def message_event_name(message: Any) -> str:
    cls = message.__class__.__name__
    subtype = getattr(message, "subtype", None)
    if subtype:
        return f"{cls}:{subtype}"
    return cls
