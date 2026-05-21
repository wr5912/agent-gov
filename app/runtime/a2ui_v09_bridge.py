from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from typing import Any


A2UI_V09_PROTOCOL_VERSION = "v0.9"
A2UI_V09_CUSTOM_EVENT_NAME = "a2ui.message"
A2UI_V09_DIAGNOSTIC_EVENT_NAME = "a2ui.diagnostic"
A2UI_V09_MESSAGE_TOOL_NAME = "mcp__ai-soc-ui__emit_a2ui_message"


@dataclass(frozen=True)
class A2uiV09ExtractionResult:
    messages: list[dict[str, Any]]
    errors: list[str]


def extract_a2ui_v09_tool_messages(raw_message: Any) -> A2uiV09ExtractionResult:
    messages: list[dict[str, Any]] = []
    errors: list[str] = []

    for record in _walk_records(raw_message):
        if _a2ui_v09_tool_name(record) is None:
            continue
        message, message_errors = normalize_a2ui_v09_tool_input(_tool_input(record))
        if message is not None:
            messages.append(message)
        errors.extend(message_errors)

    return A2uiV09ExtractionResult(messages=messages, errors=errors)


def normalize_a2ui_v09_tool_input(value: Any) -> tuple[dict[str, Any] | None, list[str]]:
    if isinstance(value, str):
        return None, ["Invalid A2UI v0.9 tool input: quoted JSON strings are not accepted."]
    if isinstance(value, list):
        return None, ["Invalid A2UI v0.9 tool input: expected exactly one message, not an array."]
    if not isinstance(value, dict):
        return None, ["Invalid A2UI v0.9 tool input: expected an object with a message field."]

    message = value.get("message", value)
    if isinstance(message, str):
        return None, ["Invalid A2UI v0.9 message: quoted JSON strings are not accepted."]
    if isinstance(message, list):
        return None, ["Invalid A2UI v0.9 message: expected exactly one message, not an array."]
    if not isinstance(message, dict):
        return None, ["Invalid A2UI v0.9 message: expected an object."]

    error = validate_a2ui_v09_message(message)
    if error:
        return None, [error]
    return message, []


def validate_a2ui_v09_message(message: dict[str, Any]) -> str | None:
    if message.get("version") != A2UI_V09_PROTOCOL_VERSION:
        return "Invalid A2UI v0.9 message: version must be 'v0.9'."

    message_keys = ["createSurface", "updateComponents", "updateDataModel", "deleteSurface"]
    present_keys = [key for key in message_keys if key in message]
    if len(present_keys) != 1:
        return (
            "Invalid A2UI v0.9 message: expected exactly one of createSurface, "
            "updateComponents, updateDataModel, or deleteSurface."
        )

    message_type = present_keys[0]
    payload = message.get(message_type)
    if not isinstance(payload, dict):
        return f"Invalid A2UI v0.9 {message_type} message: payload must be an object."

    if message_type == "createSurface":
        return _validate_create_surface(payload)
    if message_type == "updateComponents":
        return _validate_update_components(payload)
    if message_type == "updateDataModel":
        return _validate_update_data_model(payload)
    if message_type == "deleteSurface":
        return _validate_delete_surface(payload)
    return None


def _validate_create_surface(payload: dict[str, Any]) -> str | None:
    if not _non_empty_string(payload.get("surfaceId")):
        return "Invalid A2UI v0.9 createSurface message: surfaceId is required."
    if not _non_empty_string(payload.get("catalogId")):
        return "Invalid A2UI v0.9 createSurface message: catalogId is required."
    send_data_model = payload.get("sendDataModel")
    if send_data_model is not None and not isinstance(send_data_model, bool):
        return "Invalid A2UI v0.9 createSurface message: sendDataModel must be a boolean."
    return None


def _validate_update_components(payload: dict[str, Any]) -> str | None:
    if not _non_empty_string(payload.get("surfaceId")):
        return "Invalid A2UI v0.9 updateComponents message: surfaceId is required."
    components = payload.get("components")
    if not isinstance(components, list) or not components:
        return "Invalid A2UI v0.9 updateComponents message: components must be a non-empty array."

    for index, component in enumerate(components, start=1):
        if not isinstance(component, dict):
            return f"Invalid A2UI v0.9 updateComponents message: component {index} must be an object."
        if "id" in component and not _non_empty_string(component.get("id")):
            return f"Invalid A2UI v0.9 updateComponents message: component {index} id must be a string."
        if not _non_empty_string(component.get("component")):
            return f"Invalid A2UI v0.9 updateComponents message: component {index} component is required."
        weight = component.get("weight")
        if weight is not None and (isinstance(weight, bool) or not isinstance(weight, (int, float))):
            return f"Invalid A2UI v0.9 updateComponents message: component {index} weight must be a number."
    return None


def _validate_update_data_model(payload: dict[str, Any]) -> str | None:
    if not _non_empty_string(payload.get("surfaceId")):
        return "Invalid A2UI v0.9 updateDataModel message: surfaceId is required."
    path = payload.get("path")
    if path is not None and not isinstance(path, str):
        return "Invalid A2UI v0.9 updateDataModel message: path must be a string."
    return None


def _validate_delete_surface(payload: dict[str, Any]) -> str | None:
    if not _non_empty_string(payload.get("surfaceId")):
        return "Invalid A2UI v0.9 deleteSurface message: surfaceId is required."
    return None


def _walk_records(value: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if isinstance(value, dict):
        records.append(value)
        for item in value.values():
            records.extend(_walk_records(item))
    elif isinstance(value, list):
        for item in value:
            records.extend(_walk_records(item))
    elif is_dataclass(value):
        records.extend(_walk_records(asdict(value)))
    elif hasattr(value, "__dict__"):
        records.extend(
            _walk_records({key: item for key, item in vars(value).items() if not key.startswith("_")})
        )
    return records


def _a2ui_v09_tool_name(record: dict[str, Any]) -> str | None:
    tool_name = record.get("name") or record.get("tool_name") or record.get("toolName")
    if tool_name != A2UI_V09_MESSAGE_TOOL_NAME:
        return None

    record_type = str(record.get("type") or "").lower()
    hook_event = str(
        record.get("hook_event_name")
        or record.get("hookEventName")
        or record.get("hook_event")
        or record.get("hookEvent")
        or ""
    )
    has_input = any(key in record for key in ("input", "tool_input", "toolInput"))
    has_tool_use_shape = has_input and any(
        key in record for key in ("id", "tool_use_id", "toolUseID", "toolUseId")
    )
    if "tool_use" in record_type or has_tool_use_shape or (hook_event == "PreToolUse" and has_input):
        return str(tool_name)
    return None


def _tool_input(record: dict[str, Any]) -> Any:
    for key in ("input", "tool_input", "toolInput"):
        if key in record:
            return record.get(key)
    return None


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())
