from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from typing import Any, TypeAlias

from .json_types import JsonObject, JsonValue


@dataclass(frozen=True)
class ClaudeSdkMessageEvent:
    """One event for one value yielded by the official Claude Agent SDK."""

    message: Any


@dataclass(frozen=True)
class AgentGovControlEvent:
    """AgentGov-owned lifecycle/control data, kept separate from SDK messages."""

    name: str
    data: JsonObject


@dataclass(frozen=True)
class AgentGovHeartbeatEvent:
    run_id: str
    timestamp: str


ManagedClaudeEvent: TypeAlias = ClaudeSdkMessageEvent | AgentGovControlEvent | AgentGovHeartbeatEvent


def sdk_message_event_name(message: Any) -> str:
    """Return the SDK class identity without rewriting it into an API semantic name."""

    name = message.__class__.__name__
    if not name:
        raise TypeError("Claude SDK yielded a value without a class name")
    return f"claude.sdk.{name}"


def sdk_message_to_json(message: Any) -> JsonObject:
    """Mechanically serialize an SDK dataclass.

    This deliberately has no ``str(value)`` fallback: a newly introduced SDK value
    that is not JSON-compatible must fail visibly instead of silently changing the
    native contract.
    """

    if not is_dataclass(message) or isinstance(message, type):
        raise TypeError(f"Claude SDK native events require a dataclass message; got {message.__class__.__name__}")
    value = _sdk_value_to_json(message, path=message.__class__.__name__)
    if not isinstance(value, dict):
        raise TypeError(f"Claude SDK message {message.__class__.__name__} did not serialize to an object")
    return value


def _sdk_value_to_json(value: Any, *, path: str) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Enum):
        return _sdk_value_to_json(value.value, path=path)
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _sdk_value_to_json(getattr(value, field.name), path=f"{path}.{field.name}") for field in fields(value)}
    if isinstance(value, Mapping):
        output: JsonObject = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"Unsupported non-string mapping key at {path}: {key!r}")
            output[key] = _sdk_value_to_json(item, path=f"{path}.{key}")
        return output
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_sdk_value_to_json(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    raise TypeError(f"Unsupported Claude SDK value at {path}: {value.__class__.__name__}")


def stream_event_payload(message: Any) -> JsonObject | None:
    """Return the untouched Anthropic stream event carried by ``StreamEvent``."""

    if message.__class__.__name__ != "StreamEvent":
        return None
    event = getattr(message, "event", None)
    return event if isinstance(event, dict) else None


def stream_delta(message: Any) -> tuple[str, str] | None:
    """Extract only the SDK-native delta kind and text; keep policy in projectors."""

    event = stream_event_payload(message)
    if not event or event.get("type") != "content_block_delta":
        return None
    delta = event.get("delta")
    if not isinstance(delta, dict):
        return None
    delta_type = delta.get("type")
    if delta_type == "text_delta" and isinstance(delta.get("text"), str):
        return "text_delta", delta["text"]
    if delta_type == "thinking_delta" and isinstance(delta.get("thinking"), str):
        return "thinking_delta", delta["thinking"]
    if delta_type == "input_json_delta" and isinstance(delta.get("partial_json"), str):
        return "input_json_delta", delta["partial_json"]
    return None
