from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


A2UI_PROTOCOL_VERSION = "v0_8"
A2UI_CUSTOM_EVENT_NAME = "a2ui.message"
A2UI_DIAGNOSTIC_EVENT_NAME = "a2ui.diagnostic"
A2UI_RAW_TOOL_NAME = "mcp__ai-soc-ui__emit_a2ui"
A2UI_CARD_TOOL_NAME = "mcp__ai-soc-ui__emit_cards"
A2UI_RENDER_TOOL_NAME = "mcp__ai-soc-ui__render_a2ui"
A2UI_ASSET_SELECT_ACTION = "ai_soc.asset.select"
A2UI_ALERT_SELECT_ACTION = "ai_soc.alert.select"
A2UI_EVIDENCE_SELECT_ACTION = "ai_soc.evidence.select"
A2UI_JUDGEMENT_REQUEST_ACTION = "ai_soc.judgement.request"


@dataclass(frozen=True)
class A2uiExtractionResult:
    tool_payloads: list["A2uiToolPayload"]
    errors: list[str]

    @property
    def payloads(self) -> list[dict[str, Any]]:
        return [item.payload for item in self.tool_payloads]


@dataclass(frozen=True)
class A2uiToolPayload:
    tool_name: str
    payload: dict[str, Any]
    mode: str | None = None


def normalize_a2ui_payload(payload: Any) -> tuple[dict[str, Any] | None, list[str]]:
    """Normalize an A2UI message payload from a structured tool call."""
    payload, parse_error = _parse_json_string_payload(payload)
    if parse_error:
        return None, [parse_error]

    messages = _normalize_a2ui_messages(payload)
    if messages is None:
        return None, [
            "Invalid raw A2UI payload: expected a v0_8 message, message list, or {messages: [...]}. "
            "Use render_a2ui mode 'card' or emit_cards for card specs."
        ]

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


def normalize_render_a2ui_payload(payload: Any) -> tuple[dict[str, Any] | None, list[str]]:
    """Normalize the official-aligned render_a2ui tool payload."""
    payload, parse_error = _parse_json_string_payload(payload)
    if parse_error:
        return None, [parse_error]

    payload = _unwrap_render_payload(payload)
    payload, parse_error = _parse_json_string_payload(payload)
    if parse_error:
        return None, [parse_error]

    if not isinstance(payload, dict):
        return None, ["Invalid render_a2ui payload: expected an object with mode and payload fields."]

    mode = str(payload.get("mode") or "a2ui").strip().lower()
    if mode in {"a2ui", "raw"}:
        return normalize_a2ui_payload(payload.get("messages", payload.get("payload", payload)))
    if mode in {"card", "cards"}:
        return normalize_a2ui_card_payload(payload.get("payload", payload))
    if mode == "catalog":
        return None, [
            "render_a2ui catalog mode is disabled: backend must not synthesize business cards. "
            "Use mode 'card' and provide Agent-generated cards, sections, rows, and actions."
        ]
    return None, [f"Invalid render_a2ui payload: unsupported mode '{mode}'."]


def normalize_a2ui_catalog_payload(payload: Any) -> tuple[dict[str, Any] | None, list[str]]:
    """Reject backend catalog templates; Agents must provide generated cards."""
    return None, [
        "render_a2ui catalog mode is disabled: backend must not synthesize business cards. "
        "Use mode 'card' with Agent-generated card specs."
    ]


def a2ui_payload_stream_chunks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    messages = payload.get("messages")
    if not isinstance(messages, list) or len(messages) < 2:
        return [payload]

    chunks: list[dict[str, Any]] = []
    pending_begin: dict[str, Any] | None = None
    for message in messages:
        if not isinstance(message, dict):
            continue
        if "beginRendering" in message:
            pending_begin = message
            continue
        surface_update = message.get("surfaceUpdate")
        if isinstance(surface_update, dict):
            update_chunks = _surface_update_stream_chunks(surface_update)
            if not update_chunks:
                if pending_begin is not None:
                    chunks.append(_required_protocol_payload([pending_begin]))
                    pending_begin = None
                chunks.append(_required_protocol_payload([message]))
                continue
            for index, update_chunk in enumerate(update_chunks):
                chunk_messages = []
                if index == 0 and pending_begin is not None:
                    chunk_messages.append(pending_begin)
                    pending_begin = None
                chunk_messages.append({"surfaceUpdate": update_chunk})
                chunks.append(_required_protocol_payload(chunk_messages))
            continue
        if pending_begin is not None:
            chunks.append(_required_protocol_payload([pending_begin]))
            pending_begin = None
        chunks.append(_required_protocol_payload([message]))

    if pending_begin is not None:
        chunks.append(_required_protocol_payload([pending_begin]))

    return chunks or [payload]


def read_surface_id(value: Any) -> str | None:
    return str(value["surfaceId"]) if isinstance(value, dict) and _non_empty_string(value.get("surfaceId")) else None


def _required_protocol_payload(messages: list[dict[str, Any]]) -> dict[str, Any]:
    payload, errors = _protocol_payload(messages)
    if payload is None:
        raise ValueError("; ".join(errors) or "invalid A2UI payload")
    return payload


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


def _surface_update_stream_chunks(surface_update: dict[str, Any]) -> list[dict[str, Any]]:
    surface_id = read_surface_id(surface_update)
    components = surface_update.get("components")
    if not surface_id or not isinstance(components, list) or len(components) < 4:
        return []

    raw_components = [component for component in components if isinstance(component, dict)]
    structural = [component for component in raw_components if _is_structural_component(component)]
    leaves = [component for component in raw_components if not _is_structural_component(component)]
    if not structural or not leaves:
        return []

    chunks = [{"surfaceId": surface_id, "components": structural}]
    for component in leaves:
        chunks.append({"surfaceId": surface_id, "components": [component]})
    return chunks


def _is_structural_component(component: dict[str, Any]) -> bool:
    wrapped = component.get("component")
    if not isinstance(wrapped, dict) or len(wrapped) != 1:
        return False
    component_type = next(iter(wrapped.keys()))
    return component_type in {"Card", "Column", "Row", "List", "Divider"}


def extract_a2ui_tool_payloads(raw_message: Any) -> A2uiExtractionResult:
    tool_payloads: list[A2uiToolPayload] = []
    errors: list[str] = []

    for record in _walk_records(raw_message):
        tool_name = _a2ui_tool_name(record)
        if tool_name is None:
            continue

        tool_input = record.get("input")
        if tool_name == A2UI_CARD_TOOL_NAME:
            payload, payload_errors = normalize_a2ui_card_payload(tool_input)
            payload_mode = "card"
        elif tool_name == A2UI_RENDER_TOOL_NAME:
            payload, payload_errors = normalize_render_a2ui_payload(tool_input)
            payload_mode = _render_a2ui_mode(tool_input)
        else:
            payload, payload_errors = normalize_a2ui_payload(tool_input)
            payload_mode = "a2ui"
        if payload is not None:
            tool_payloads.append(A2uiToolPayload(tool_name=tool_name, payload=payload, mode=payload_mode))
        errors.extend(payload_errors)

    return A2uiExtractionResult(tool_payloads=tool_payloads, errors=errors)


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
    if tool_name not in {A2UI_RAW_TOOL_NAME, A2UI_CARD_TOOL_NAME, A2UI_RENDER_TOOL_NAME}:
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


def _unwrap_render_payload(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    if set(payload.keys()) == {"payload"}:
        return payload["payload"]
    if "payload" in payload and not any(key in payload for key in ("mode", "messages", "cards", "component")):
        return payload["payload"]
    return payload


def _render_a2ui_mode(payload: Any) -> str | None:
    payload, _ = _parse_json_string_payload(payload)
    payload = _unwrap_render_payload(payload)
    payload, _ = _parse_json_string_payload(payload)
    if isinstance(payload, dict):
        return str(payload.get("mode") or "a2ui").strip().lower()
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

        action_ids = _card_action_components(surface_id, index, _list_of_records(spec.get("actions")))
        if action_ids["root_ids"]:
            child_ids.extend(action_ids["root_ids"])
            components.extend(action_ids["components"])

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


def _card_action_components(surface_id: str, card_index: int, actions: list[dict[str, Any]]) -> dict[str, Any]:
    if not actions:
        return {"root_ids": [], "components": []}

    prefix = f"card-{card_index}-actions"
    row_id = _component_id(surface_id, prefix)
    button_ids: list[str] = []
    components: list[dict[str, Any]] = []

    for action_index, action in enumerate(actions, start=1):
        name = str(action.get("name") or "").strip()
        label = str(action.get("label") or action.get("title") or "").strip()
        if not name or not label:
            continue

        button_id = _component_id(surface_id, f"{prefix}-button-{action_index}")
        label_id = _component_id(surface_id, f"{prefix}-button-{action_index}-label")
        button_ids.append(button_id)
        components.append(_text_component(label_id, label, "body"))
        components.append(
            _button_component(
                button_id,
                label_id,
                name=name,
                context=_a2ui_action_context(action.get("context")),
                primary=bool(action.get("primary")),
            )
        )

    if not button_ids:
        return {"root_ids": [], "components": []}

    components.append(_row_component(row_id, button_ids))
    return {"root_ids": [row_id], "components": components}


def _button_component(
    component_id: str,
    child_id: str,
    *,
    name: str,
    context: list[dict[str, Any]],
    primary: bool,
) -> dict[str, Any]:
    return {
        "id": component_id,
        "component": {
            "Button": {
                "child": child_id,
                "primary": primary,
                "action": {
                    "name": name,
                    "context": context,
                },
            }
        },
    }


def _row_component(component_id: str, child_ids: list[str]) -> dict[str, Any]:
    return {
        "id": component_id,
        "component": {
            "Row": {
                "children": {
                    "explicitList": child_ids,
                },
                "distribution": "start",
                "alignment": "center",
            }
        },
    }


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


def _a2ui_action_context(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []

    result: list[dict[str, Any]] = []
    for key, raw_value in value.items():
        if not _non_empty_string(key):
            continue
        literal = _literal_value(raw_value)
        if literal is None:
            continue
        result.append({"key": str(key), "value": literal})
    return result


def _literal_value(value: Any) -> dict[str, Any] | None:
    if isinstance(value, bool):
        return {"literalBoolean": value}
    if isinstance(value, (int, float)):
        return {"literalNumber": value}
    if isinstance(value, str):
        return {"literalString": value}
    return None


def _string_or_empty(value: Any) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _number_or_zero(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


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
