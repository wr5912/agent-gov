from __future__ import annotations

import json
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any


A2UI_V09_PROTOCOL_VERSION = "v0.9"
A2UI_V09_CUSTOM_EVENT_NAME = "a2ui.message"
A2UI_V09_DIAGNOSTIC_EVENT_NAME = "a2ui.diagnostic"
A2UI_V09_MESSAGE_TOOL_NAME = "mcp__ai-soc-ui__emit_a2ui_message"
A2UI_V09_BASIC_CATALOG_ID = "https://a2ui.org/specification/v0_9/basic_catalog.json"
ALLOWED_A2UI_V09_COMPONENTS = {
    "Text",
    "Image",
    "Icon",
    "Video",
    "AudioPlayer",
    "Row",
    "Column",
    "List",
    "Card",
    "Tabs",
    "Divider",
    "Modal",
    "Button",
    "TextField",
    "CheckBox",
    "ChoicePicker",
    "Slider",
    "DateTimeInput",
}


@dataclass(frozen=True)
class A2uiV09ExtractionResult:
    messages: list[dict[str, Any]]
    errors: list[str]
    warnings: list[str]


@dataclass(frozen=True)
class A2uiV09NormalizationResult:
    message: dict[str, Any] | None
    errors: list[str]
    warnings: list[str]


def extract_a2ui_v09_tool_messages(raw_message: Any) -> A2uiV09ExtractionResult:
    messages: list[dict[str, Any]] = []
    errors: list[str] = []
    warnings: list[str] = []

    for record in _walk_records(raw_message):
        if _a2ui_v09_tool_name(record) is None:
            continue
        result = _normalize_a2ui_v09_tool_input(_tool_input(record))
        if result.message is not None:
            messages.append(result.message)
        errors.extend(result.errors)
        warnings.extend(result.warnings)

    return A2uiV09ExtractionResult(messages=messages, errors=errors, warnings=warnings)


def normalize_a2ui_v09_tool_input(value: Any) -> tuple[dict[str, Any] | None, list[str]]:
    result = _normalize_a2ui_v09_tool_input(value)
    return result.message, result.errors


def _normalize_a2ui_v09_tool_input(value: Any) -> A2uiV09NormalizationResult:
    warnings: list[str] = []
    if isinstance(value, str):
        value, errors = _parse_quoted_json(value, "tool input")
        if errors:
            return A2uiV09NormalizationResult(message=None, errors=errors, warnings=warnings)
        warnings.append("Recovered A2UI v0.9 tool input from a quoted JSON string; pass a structured object instead.")
    if isinstance(value, list):
        return A2uiV09NormalizationResult(
            message=None,
            errors=["Invalid A2UI v0.9 tool input: expected exactly one message, not an array."],
            warnings=warnings,
        )
    if not isinstance(value, dict):
        return A2uiV09NormalizationResult(
            message=None,
            errors=["Invalid A2UI v0.9 tool input: expected an object with a message field."],
            warnings=warnings,
        )

    message = value.get("message", value)
    if isinstance(message, str):
        message, errors = _parse_quoted_json(message, "message")
        if errors:
            return A2uiV09NormalizationResult(message=None, errors=errors, warnings=warnings)
        warnings.append("Recovered A2UI v0.9 message from a quoted JSON string; pass a structured object instead.")
    if isinstance(message, list):
        return A2uiV09NormalizationResult(
            message=None,
            errors=["Invalid A2UI v0.9 message: expected exactly one message, not an array."],
            warnings=warnings,
        )
    if not isinstance(message, dict):
        return A2uiV09NormalizationResult(
            message=None,
            errors=["Invalid A2UI v0.9 message: expected an object."],
            warnings=warnings,
        )

    message, normalization_warnings = _normalize_message(message)
    warnings.extend(normalization_warnings)
    error = validate_a2ui_v09_message(message)
    if error:
        return A2uiV09NormalizationResult(message=None, errors=[error], warnings=warnings)
    return A2uiV09NormalizationResult(message=message, errors=[], warnings=warnings)


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
        component_name = str(component.get("component"))
        if component_name not in ALLOWED_A2UI_V09_COMPONENTS:
            return (
                f"Invalid A2UI v0.9 updateComponents message: component {index} "
                f"uses unregistered component '{component_name}'."
            )
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


def _parse_quoted_json(value: str, label: str) -> tuple[Any, list[str]]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None, [f"Invalid A2UI v0.9 {label}: quoted JSON string could not be parsed."]
    if isinstance(parsed, str):
        return None, [f"Invalid A2UI v0.9 {label}: nested quoted JSON strings are not accepted."]
    return parsed, []


def _normalize_message(message: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    normalized = dict(message)
    warnings: list[str] = []
    payload = normalized.get("updateComponents")
    if isinstance(payload, dict):
        normalized["updateComponents"], component_warnings = _normalize_update_components(payload)
        warnings.extend(component_warnings)
    return normalized, warnings


def _normalize_update_components(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    normalized = dict(payload)
    warnings: list[str] = []
    components = normalized.get("components")
    if not isinstance(components, list):
        return normalized, warnings

    cards = [item for item in components if _is_legacy_card_component(item)]
    if cards:
        normalized["components"] = _legacy_cards_to_v09_components(cards)
        warnings.append("Repaired legacy card DSL into basic A2UI v0.9 Card/Column/Text components.")
        return normalized, warnings

    tables = [item for item in components if _is_legacy_table_component(item)]
    if tables:
        normalized["components"] = _legacy_tables_to_v09_components(tables)
        warnings.append("Repaired unsupported table component into basic A2UI v0.9 Card/Column/Text components.")
        return normalized, warnings

    normalized_components: list[dict[str, Any]] = []
    top_level_ids: list[str] = []
    for index, component in enumerate(components, start=1):
        if not isinstance(component, dict):
            normalized_components.append(component)  # type: ignore[arg-type]
            continue
        root_id, component_warnings = _normalize_component_tree(
            component,
            fallback_id=f"component-{index}",
            output=normalized_components,
        )
        if root_id:
            top_level_ids.append(root_id)
        warnings.extend(component_warnings)
    if normalized_components and not any(component.get("id") == "root" for component in normalized_components):
        normalized_components.append(
            {
                "id": "root",
                "component": "Column",
                "children": top_level_ids,
                "align": "stretch",
            }
        )
        warnings.append("Repaired updateComponents without root by wrapping top-level components in a root Column.")
    normalized["components"] = normalized_components
    return normalized, warnings


def _normalize_component_tree(
    component: dict[str, Any],
    *,
    fallback_id: str,
    output: list[dict[str, Any]],
) -> tuple[str | None, list[str]]:
    normalized = dict(component)
    warnings: list[str] = []
    props = normalized.pop("props", None)
    if isinstance(props, dict):
        for key, value in props.items():
            normalized.setdefault(key, value)
        warnings.append("Repaired React-style props object into A2UI v0.9 component properties.")

    if "component" not in normalized and _non_empty_string(normalized.get("type")):
        normalized["component"] = _component_name(str(normalized["type"]))
        normalized.pop("type", None)
        warnings.append("Repaired legacy component type field to A2UI v0.9 component field.")

    component_name = normalized.get("component")
    if isinstance(component_name, str):
        normalized["component"] = _component_name(component_name)

    if not _non_empty_string(normalized.get("id")):
        normalized["id"] = fallback_id
        warnings.append("Repaired A2UI v0.9 component without id by assigning a generated id.")

    component_id = str(normalized["id"])
    for key in ("child",):
        child = normalized.get(key)
        if isinstance(child, dict):
            child_id, child_warnings = _normalize_component_tree(
                child,
                fallback_id=f"{component_id}-{key}",
                output=output,
            )
            warnings.extend(child_warnings)
            if child_id:
                normalized[key] = child_id

    for key in ("children", "items"):
        children = normalized.get(key)
        if not isinstance(children, list):
            continue
        child_ids: list[Any] = []
        for index, child in enumerate(children, start=1):
            if isinstance(child, dict):
                child_id, child_warnings = _normalize_component_tree(
                    child,
                    fallback_id=f"{component_id}-{key}-{index}",
                    output=output,
                )
                warnings.extend(child_warnings)
                if child_id:
                    child_ids.append(child_id)
            else:
                child_ids.append(child)
        normalized[key] = child_ids

    if normalized.get("component") == "Card" and not _non_empty_string(normalized.get("child")):
        card_children: list[str] = []
        title = _string_or_none(normalized.pop("title", None))
        subtitle = _string_or_none(normalized.pop("subtitle", None))
        if title:
            title_id = f"{component_id}-title"
            output.append({"id": title_id, "component": "Text", "text": title, "variant": "h3"})
            card_children.append(title_id)
        if subtitle:
            subtitle_id = f"{component_id}-subtitle"
            output.append({"id": subtitle_id, "component": "Text", "text": subtitle, "variant": "caption"})
            card_children.append(subtitle_id)
        if card_children:
            body_id = f"{component_id}-body"
            output.append({"id": body_id, "component": "Column", "children": card_children, "align": "stretch"})
            normalized["child"] = body_id
            warnings.append("Repaired title/subtitle Card shorthand into explicit Text children.")

    output.append(normalized)
    return component_id, warnings


def _is_legacy_card_component(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    component_type = str(value.get("type") or value.get("component") or "").lower()
    return component_type == "card" and ("sections" in value or "title" in value)


def _is_legacy_table_component(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    component_type = str(value.get("type") or value.get("component") or "").lower()
    return component_type == "table" and isinstance(value.get("rows"), list)


def _legacy_tables_to_v09_components(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cards = [
        {
            "id": _safe_id(table.get("id"), f"table-{index}"),
            "title": _string_or_none(table.get("title")) or "数据表",
            "sections": [
                {
                    "type": "table",
                    "columns": table.get("columns"),
                    "rows": table.get("rows"),
                }
            ],
        }
        for index, table in enumerate(tables, start=1)
    ]
    return _legacy_cards_to_v09_components(cards)


def _legacy_cards_to_v09_components(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    components: list[dict[str, Any]] = []
    root_children: list[str] = []

    for index, card in enumerate(cards, start=1):
        card_id = _safe_id(card.get("id"), f"card-{index}")
        body_id = f"{card_id}-body"
        child_ids: list[str] = []

        title = _string_or_none(card.get("title"))
        if title:
            title_id = f"{card_id}-title"
            child_ids.append(title_id)
            components.append({"id": title_id, "component": "Text", "text": title, "variant": "h3"})

        subtitle = _string_or_none(card.get("subtitle"))
        if subtitle:
            subtitle_id = f"{card_id}-subtitle"
            child_ids.append(subtitle_id)
            components.append({"id": subtitle_id, "component": "Text", "text": subtitle, "variant": "caption"})

        sections = card.get("sections")
        if isinstance(sections, list):
            for section_index, section in enumerate(sections, start=1):
                child_ids.extend(_legacy_section_components(card_id, section_index, section, components))

        if not child_ids:
            empty_id = f"{card_id}-empty"
            child_ids.append(empty_id)
            components.append({"id": empty_id, "component": "Text", "text": "暂无可展示内容。", "variant": "body"})

        components.append({"id": body_id, "component": "Column", "children": child_ids, "align": "stretch"})
        components.append({"id": card_id, "component": "Card", "child": body_id})
        root_children.append(card_id)

    if len(root_children) == 1:
        only_card = next((component for component in components if component.get("id") == root_children[0]), None)
        if isinstance(only_card, dict):
            only_card["id"] = "root"
            root_children[0] = "root"
    else:
        components.append({"id": "root", "component": "Column", "children": root_children, "align": "stretch"})

    return components


def _legacy_section_components(
    card_id: str,
    section_index: int,
    section: Any,
    components: list[dict[str, Any]],
) -> list[str]:
    if not isinstance(section, dict):
        return []

    section_type = str(section.get("type") or "").lower()
    root_ids: list[str] = []
    heading = _string_or_none(section.get("title") or section.get("label"))
    if heading:
        heading_id = f"{card_id}-section-{section_index}-title"
        root_ids.append(heading_id)
        components.append({"id": heading_id, "component": "Text", "text": heading, "variant": "h3"})

    if section_type == "metric_group":
        metrics = section.get("metrics")
        if isinstance(metrics, list):
            for metric_index, metric in enumerate(metrics, start=1):
                if not isinstance(metric, dict):
                    continue
                label = _string_or_none(metric.get("label")) or f"指标 {metric_index}"
                value = metric.get("value")
                metric_id = f"{card_id}-section-{section_index}-metric-{metric_index}"
                root_ids.append(metric_id)
                components.append(
                    {
                        "id": metric_id,
                        "component": "Text",
                        "text": f"{label}：{_display_value(value)}",
                        "variant": "body",
                    }
                )
        return root_ids

    if section_type == "table":
        columns = section.get("columns")
        rows = section.get("rows")
        if isinstance(rows, list):
            for row_index, row in enumerate(rows[:10], start=1):
                row_text = _legacy_table_row_text(columns, row)
                if not row_text:
                    continue
                row_id = f"{card_id}-section-{section_index}-row-{row_index}"
                root_ids.append(row_id)
                components.append({"id": row_id, "component": "Text", "text": row_text, "variant": "body"})
        return root_ids

    text = _string_or_none(section.get("text") or section.get("content") or section.get("summary"))
    if text:
        text_id = f"{card_id}-section-{section_index}-text"
        root_ids.append(text_id)
        components.append({"id": text_id, "component": "Text", "text": text, "variant": "body"})
    return root_ids


def _legacy_table_row_text(columns: Any, row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    if isinstance(columns, list) and columns:
        parts: list[str] = []
        for column in columns[:5]:
            if not isinstance(column, dict):
                continue
            key = column.get("key")
            if not isinstance(key, str) or key not in row:
                continue
            label = _string_or_none(column.get("label")) or key
            parts.append(f"{label}: {_display_value(row.get(key))}")
        return " | ".join(parts)
    return " | ".join(f"{key}: {_display_value(value)}" for key, value in list(row.items())[:5])


def _component_name(value: str) -> str:
    aliases = {
        "card": "Card",
        "column": "Column",
        "row": "Row",
        "text": "Text",
        "list": "List",
        "divider": "Divider",
        "button": "Button",
    }
    return aliases.get(value.lower(), value)


def _safe_id(value: Any, fallback: str) -> str:
    return str(value).strip() if _non_empty_string(value) else fallback


def _string_or_none(value: Any) -> str | None:
    return str(value).strip() if isinstance(value, (str, int, float)) and str(value).strip() else None


def _display_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, default=str)


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
