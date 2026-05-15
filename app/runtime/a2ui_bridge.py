from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


A2UI_PROTOCOL_VERSION = "v0_8"
A2UI_CUSTOM_EVENT_NAME = "a2ui.message"
A2UI_RAW_TOOL_NAME = "mcp__ai-soc-ui__emit_a2ui"
A2UI_CARD_TOOL_NAME = "mcp__ai-soc-ui__emit_cards"


@dataclass(frozen=True)
class A2uiExtractionResult:
    payloads: list[dict[str, Any]]
    errors: list[str]


def normalize_a2ui_payload(payload: Any) -> tuple[dict[str, Any] | None, list[str]]:
    """Normalize an A2UI message payload from a structured tool call."""
    payload, parse_error = _parse_json_string_payload(payload)
    if parse_error:
        return None, [parse_error]

    messages = _normalize_a2ui_messages(payload)
    if messages is None:
        messages = _normalize_card_specs(payload)

    if messages is None:
        return None, ["Invalid A2UI payload: expected a v0_8 message, message list, or {messages: [...]}."]

    return _protocol_payload(messages)


def normalize_a2ui_card_payload(payload: Any) -> tuple[dict[str, Any] | None, list[str]]:
    """Normalize simple AI-SOC card specs into A2UI messages."""
    payload, parse_error = _parse_json_string_payload(payload)
    if parse_error:
        return None, [parse_error]

    messages = _normalize_card_specs(payload)
    if messages is None:
        return None, ["Invalid A2UI card payload: expected {cards: [...]} or a card spec list."]

    return _protocol_payload(messages)


def _protocol_payload(messages: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[str]]:
    invalid = [_validate_a2ui_message(message) for message in messages]
    invalid_reasons = [reason for reason in invalid if reason]
    if invalid_reasons:
        return None, invalid_reasons

    return (
        {
            "protocol": "a2ui",
            "version": A2UI_PROTOCOL_VERSION,
            "messages": messages,
        },
        [],
    )


def extract_a2ui_tool_payloads(raw_message: Any) -> A2uiExtractionResult:
    payloads: list[dict[str, Any]] = []
    errors: list[str] = []

    for record in _walk_records(raw_message):
        tool_name = _a2ui_tool_name(record)
        if tool_name is None:
            continue

        tool_input = record.get("input")
        if tool_name == A2UI_CARD_TOOL_NAME:
            payload, payload_errors = normalize_a2ui_card_payload(tool_input)
        else:
            payload, payload_errors = normalize_a2ui_payload(tool_input)
        if payload is not None:
            payloads.append(payload)
        errors.extend(payload_errors)

    return A2uiExtractionResult(payloads=payloads, errors=errors)


def _walk_records(value: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if isinstance(value, dict):
        records.append(value)
        for item in value.values():
            records.extend(_walk_records(item))
    elif isinstance(value, list):
        for item in value:
            records.extend(_walk_records(item))
    return records


def _a2ui_tool_name(record: dict[str, Any]) -> str | None:
    tool_name = record.get("name") or record.get("tool_name") or record.get("toolName")
    if tool_name not in {A2UI_RAW_TOOL_NAME, A2UI_CARD_TOOL_NAME}:
        return None

    record_type = str(record.get("type") or "").lower()
    has_tool_use_shape = "input" in record and any(key in record for key in ("id", "tool_use_id"))
    if "tool_use" in record_type or has_tool_use_shape:
        return str(tool_name)
    return None


def _normalize_a2ui_messages(payload: Any) -> list[dict[str, Any]] | None:
    if _is_a2ui_message(payload):
        return [payload]

    if isinstance(payload, list) and all(_is_a2ui_message(item) for item in payload):
        return payload

    if isinstance(payload, dict) and isinstance(payload.get("messages"), list):
        messages = payload["messages"]
        if all(_is_a2ui_message(item) for item in messages):
            return messages

    if isinstance(payload, dict) and isinstance(payload.get("messages"), str):
        parsed_messages, parse_error = _parse_json_string_payload(payload["messages"])
        if parse_error:
            return None
        if isinstance(parsed_messages, list) and all(_is_a2ui_message(item) for item in parsed_messages):
            return parsed_messages

    return None


def _normalize_card_specs(payload: Any) -> list[dict[str, Any]] | None:
    surface_id = "ai-soc-generated-cards"
    specs: list[dict[str, Any]] | None = None

    if _is_card_spec(payload):
        specs = [payload]
    elif isinstance(payload, list) and all(_is_card_spec(item) for item in payload):
        specs = payload
    elif isinstance(payload, dict):
        if _non_empty_string(payload.get("surfaceId")):
            surface_id = str(payload["surfaceId"])
        elif _non_empty_string(payload.get("surface_id")):
            surface_id = str(payload["surface_id"])
        cards = payload.get("cards", payload.get("messages"))
        if isinstance(cards, str):
            cards, _ = _parse_json_string_payload(cards)
        if _is_card_spec(cards):
            specs = [cards]
        elif isinstance(cards, list) and all(_is_card_spec(item) for item in cards):
            specs = cards

    if not specs:
        return None

    return _card_specs_to_a2ui_messages(specs, surface_id)


def _is_card_spec(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("type", "card") == "card"
        and _non_empty_string(value.get("title"))
    )


def _card_specs_to_a2ui_messages(specs: list[dict[str, Any]], surface_id: str) -> list[dict[str, Any]]:
    components: list[dict[str, Any]] = []
    card_ids: list[str] = []
    root_id = _component_id(surface_id, "root")

    for index, spec in enumerate(specs, start=1):
        card_id = _component_id(surface_id, f"card-{index}")
        content_id = _component_id(surface_id, f"card-{index}-content")
        child_ids: list[str] = []
        card_ids.append(card_id)

        title = str(spec.get("title") or "").strip()
        if title:
            title_id = _component_id(surface_id, f"card-{index}-title")
            child_ids.append(title_id)
            components.append(_text_component(title_id, title, "h3"))

        subtitle = str(spec.get("subtitle") or "").strip()
        if subtitle:
            subtitle_id = _component_id(surface_id, f"card-{index}-subtitle")
            child_ids.append(subtitle_id)
            components.append(_text_component(subtitle_id, subtitle, "caption"))

        for section_index, section in enumerate(_list_of_records(spec.get("sections")), start=1):
            section_ids = _section_components(surface_id, index, section_index, section)
            child_ids.extend(section_ids["root_ids"])
            components.extend(section_ids["components"])

        footer = str(spec.get("footer") or "").strip()
        if footer:
            footer_id = _component_id(surface_id, f"card-{index}-footer")
            child_ids.append(footer_id)
            components.append(_text_component(footer_id, footer, "caption"))

        components.append(
            {
                "id": card_id,
                "component": {
                    "Card": {
                        "child": content_id,
                    }
                },
            }
        )
        components.append(_column_component(content_id, child_ids))

    components.insert(0, _column_component(root_id, card_ids))

    return [
        {"beginRendering": {"surfaceId": surface_id, "root": root_id}},
        {
            "surfaceUpdate": {
                "surfaceId": surface_id,
                "components": components,
            }
        },
    ]


def _section_components(surface_id: str, card_index: int, section_index: int, section: dict[str, Any]) -> dict[str, Any]:
    prefix = f"card-{card_index}-section-{section_index}"
    components: list[dict[str, Any]] = []
    root_ids: list[str] = []

    title = str(section.get("title") or "").strip()
    if title:
        title_id = _component_id(surface_id, f"{prefix}-title")
        root_ids.append(title_id)
        components.append(_text_component(title_id, title, "body"))

    section_type = str(section.get("type") or "").strip()
    if section_type == "metric_group":
        for item_index, item in enumerate(_list_of_records(section.get("items")), start=1):
            label = str(item.get("label") or "").strip()
            value = str(item.get("value") or "").strip()
            text = f"{label}: {value}" if label and value else label or value
            if text:
                item_id = _component_id(surface_id, f"{prefix}-metric-{item_index}")
                root_ids.append(item_id)
                components.append(_text_component(item_id, text, "body"))
    elif section_type == "table":
        rows = section.get("rows")
        for row_index, row in enumerate(rows if isinstance(rows, list) else [], start=1):
            if not isinstance(row, list):
                continue
            text = " | ".join(str(cell) for cell in row)
            if text:
                row_id = _component_id(surface_id, f"{prefix}-row-{row_index}")
                root_ids.append(row_id)
                components.append(_text_component(row_id, text, "caption"))
    elif section_type == "key_value":
        items = section.get("items")
        if isinstance(items, dict):
            for item_index, (key, value) in enumerate(items.items(), start=1):
                item_id = _component_id(surface_id, f"{prefix}-kv-{item_index}")
                root_ids.append(item_id)
                components.append(_text_component(item_id, f"{key}: {value}", "body"))
    elif section_type in {"tags", "action_list"}:
        for item_index, item in enumerate(_string_items(section.get("items")), start=1):
            item_id = _component_id(surface_id, f"{prefix}-item-{item_index}")
            root_ids.append(item_id)
            components.append(_text_component(item_id, item, "body"))
    else:
        for item_index, item in enumerate(_string_items(section.get("items")), start=1):
            item_id = _component_id(surface_id, f"{prefix}-item-{item_index}")
            root_ids.append(item_id)
            components.append(_text_component(item_id, item, "body"))

    return {"root_ids": root_ids, "components": components}


def _column_component(component_id: str, child_ids: list[str]) -> dict[str, Any]:
    return {
        "id": component_id,
        "component": {
            "Column": {
                "children": {
                    "explicitList": child_ids,
                },
                "distribution": "start",
                "alignment": "stretch",
            }
        },
    }


def _text_component(component_id: str, text: str, usage_hint: str) -> dict[str, Any]:
    return {
        "id": component_id,
        "component": {
            "Text": {
                "text": {
                    "literal": text,
                },
                "usageHint": usage_hint,
            }
        },
    }


def _component_id(surface_id: str, suffix: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9_-]+", "-", f"{surface_id}-{suffix}").strip("-")
    return value or suffix


def _list_of_records(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _string_items(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict):
            label = item.get("label")
            description = item.get("description")
            if isinstance(label, str) and isinstance(description, str):
                result.append(f"{label}: {description}")
            elif isinstance(label, str):
                result.append(label)
    return result


def _is_a2ui_message(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    keys = {"beginRendering", "surfaceUpdate", "dataModelUpdate", "deleteSurface"}
    return sum(1 for key in keys if key in value) == 1 and "version" not in value


def _parse_json_string_payload(value: Any) -> tuple[Any, str | None]:
    if not isinstance(value, str):
        return value, None
    try:
        return json.loads(value), None
    except json.JSONDecodeError as exc:
        return None, f"Invalid A2UI JSON string payload: {exc.msg}"


def _validate_a2ui_message(message: dict[str, Any]) -> str | None:
    if "beginRendering" in message:
        return _validate_begin_rendering(message["beginRendering"])
    if "surfaceUpdate" in message:
        return _validate_surface_update(message["surfaceUpdate"])
    if "dataModelUpdate" in message:
        return _validate_data_model_update(message["dataModelUpdate"])
    if "deleteSurface" in message:
        return _validate_delete_surface(message["deleteSurface"])
    return "Invalid A2UI message: unsupported message type."


def _validate_begin_rendering(value: Any) -> str | None:
    if not isinstance(value, dict):
        return "Invalid A2UI beginRendering: expected object."
    if not _non_empty_string(value.get("surfaceId")):
        return "Invalid A2UI beginRendering: missing surfaceId."
    if not _non_empty_string(value.get("root")):
        return "Invalid A2UI beginRendering: missing root."
    return None


def _validate_surface_update(value: Any) -> str | None:
    if not isinstance(value, dict):
        return "Invalid A2UI surfaceUpdate: expected object."
    if not _non_empty_string(value.get("surfaceId")):
        return "Invalid A2UI surfaceUpdate: missing surfaceId."
    components = value.get("components")
    if not isinstance(components, list) or not components:
        return "Invalid A2UI surfaceUpdate: components must be a non-empty list."
    for component in components:
        if not isinstance(component, dict):
            return "Invalid A2UI surfaceUpdate: component must be an object."
        if not _non_empty_string(component.get("id")):
            return "Invalid A2UI surfaceUpdate: component missing id."
        if not isinstance(component.get("component"), dict) or len(component["component"]) != 1:
            return "Invalid A2UI surfaceUpdate: component wrapper must contain exactly one component type."
    return None


def _validate_data_model_update(value: Any) -> str | None:
    if not isinstance(value, dict):
        return "Invalid A2UI dataModelUpdate: expected object."
    if not _non_empty_string(value.get("surfaceId")):
        return "Invalid A2UI dataModelUpdate: missing surfaceId."
    contents = value.get("contents")
    if not isinstance(contents, list):
        return "Invalid A2UI dataModelUpdate: contents must be a list."
    for item in contents:
        if not isinstance(item, dict) or not _non_empty_string(item.get("key")):
            return "Invalid A2UI dataModelUpdate: each entry must include a key."
    return None


def _validate_delete_surface(value: Any) -> str | None:
    if not isinstance(value, dict):
        return "Invalid A2UI deleteSurface: expected object."
    if not _non_empty_string(value.get("surfaceId")):
        return "Invalid A2UI deleteSurface: missing surfaceId."
    return None


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())
