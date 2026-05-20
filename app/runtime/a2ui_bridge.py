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
AI_SOC_A2UI_CATALOG = "ai-soc"
AI_SOC_A2UI_CATALOG_COMPONENTS = {
    "RiskMetricGroup",
    "RiskAssetTable",
    "AlertTriageCard",
}


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
        return normalize_a2ui_catalog_payload(payload)
    return None, [f"Invalid render_a2ui payload: unsupported mode '{mode}'."]


def normalize_a2ui_catalog_payload(payload: Any) -> tuple[dict[str, Any] | None, list[str]]:
    """Normalize AI-SOC catalog component payloads into A2UI v0.8 messages."""
    payload, parse_error = _parse_json_string_payload(payload)
    if parse_error:
        return None, [parse_error]
    if not isinstance(payload, dict):
        return None, ["Invalid A2UI catalog payload: expected an object."]

    catalog = str(payload.get("catalog") or AI_SOC_A2UI_CATALOG).strip()
    if catalog != AI_SOC_A2UI_CATALOG:
        return None, [f"Invalid A2UI catalog payload: unsupported catalog '{catalog}'."]

    surface_id = _catalog_surface_id(payload)
    components = _catalog_component_specs(payload)
    if components is None:
        return None, ["Invalid A2UI catalog payload: expected component or components."]

    cards: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, component in enumerate(components, start=1):
        card, component_errors = _catalog_component_to_card(component, index)
        if card is not None:
            cards.append(card)
        errors.extend(component_errors)

    if errors:
        return None, errors
    if not cards:
        return None, ["Invalid A2UI catalog payload: no renderable catalog components."]

    return _protocol_payload(_card_specs_to_a2ui_messages(cards, surface_id))


def a2ui_payload_surface_ids(payload: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return ids

    for message in messages:
        if not isinstance(message, dict):
            continue
        surface_id = (
            read_surface_id(message.get("beginRendering"))
            or read_surface_id(message.get("surfaceUpdate"))
            or read_surface_id(message.get("dataModelUpdate"))
            or read_surface_id(message.get("deleteSurface"))
        )
        if surface_id and surface_id not in ids:
            ids.append(surface_id)
    return ids


def retarget_a2ui_payload(payload: dict[str, Any], target_surface_id: str) -> dict[str, Any]:
    source_ids = [surface_id for surface_id in a2ui_payload_surface_ids(payload) if surface_id != target_surface_id]
    if not source_ids:
        return payload

    # Current generated payloads are single-surface. If a future payload contains
    # multiple surfaces, only the first one is retargeted by this compatibility path.
    source_surface_id = source_ids[0]
    return _replace_surface_references(payload, source_surface_id, target_surface_id)


def read_surface_id(value: Any) -> str | None:
    return str(value["surfaceId"]) if isinstance(value, dict) and _non_empty_string(value.get("surfaceId")) else None


def asset_risk_surface_id(run_id: str) -> str:
    return _component_id("ai-soc-asset-risk", run_id or "run")


def asset_risk_skeleton_payload(surface_id: str) -> dict[str, Any]:
    title_id = _component_id(surface_id, "title")
    status_id = _component_id(surface_id, "status")
    content_id = _component_id(surface_id, "content")
    card_id = _component_id(surface_id, "card")
    root_id = _component_id(surface_id, "root")
    return _required_protocol_payload(
        [
            {"beginRendering": {"surfaceId": surface_id, "root": root_id}},
            {
                "surfaceUpdate": {
                    "surfaceId": surface_id,
                    "components": [
                        _column_component(root_id, [card_id]),
                        {
                            "id": card_id,
                            "component": {"Card": {"child": content_id}},
                        },
                        _column_component(content_id, [title_id, status_id]),
                        _text_component(title_id, "资产风险概览", "h3"),
                        _text_component(status_id, "正在准备资产风险视图", "caption"),
                    ],
                }
            },
        ]
    )


def asset_risk_status_payload(surface_id: str, status: str) -> dict[str, Any]:
    return _required_protocol_payload(
        [
            {
                "surfaceUpdate": {
                    "surfaceId": surface_id,
                    "components": [
                        _text_component(_component_id(surface_id, "status"), status, "caption"),
                    ],
                }
            }
        ]
    )


def asset_risk_result_payload(
    surface_id: str,
    assets: list[dict[str, Any]],
    *,
    completed: bool = False,
) -> dict[str, Any]:
    content_id = _component_id(surface_id, "content")
    title_id = _component_id(surface_id, "title")
    status_id = _component_id(surface_id, "status")
    metrics_title_id = _component_id(surface_id, "metrics-title")
    high_title_id = _component_id(surface_id, "high-risk-title")
    recommendation_title_id = _component_id(surface_id, "recommendation-title")

    normalized_assets = [_normalize_asset_for_card(asset) for asset in assets]
    normalized_assets = [asset for asset in normalized_assets if asset.get("assetId")]
    high_risk = [asset for asset in normalized_assets if _asset_score(asset) >= 80]
    medium_risk = [asset for asset in normalized_assets if 50 <= _asset_score(asset) < 80]
    low_risk = [asset for asset in normalized_assets if _asset_score(asset) < 50]
    top_assets = sorted(normalized_assets, key=_asset_score, reverse=True)[:5]

    child_ids = [
        title_id,
        status_id,
        metrics_title_id,
        _component_id(surface_id, "metric-total"),
        _component_id(surface_id, "metric-high"),
        _component_id(surface_id, "metric-medium"),
        _component_id(surface_id, "metric-low"),
        high_title_id,
    ]
    components = [
        _text_component(title_id, "资产风险概览", "h3"),
        _text_component(
            status_id,
            "资产风险视图已生成" if completed else "已获取资产数据，正在生成风险视图",
            "caption",
        ),
        _text_component(metrics_title_id, "风险分布", "body"),
        _text_component(_component_id(surface_id, "metric-total"), f"资产总数: {len(normalized_assets)}", "body"),
        _text_component(_component_id(surface_id, "metric-high"), f"高风险(>=80): {len(high_risk)}", "body"),
        _text_component(_component_id(surface_id, "metric-medium"), f"中风险(50-79): {len(medium_risk)}", "body"),
        _text_component(_component_id(surface_id, "metric-low"), f"低风险(<50): {len(low_risk)}", "body"),
        _text_component(high_title_id, "高风险资产 TOP 5", "body"),
    ]

    for index, asset in enumerate(top_assets, start=1):
        item_id = _component_id(surface_id, f"high-risk-asset-{index}")
        child_ids.append(item_id)
        components.append(_text_component(item_id, _asset_summary_text(asset), "caption"))

    child_ids.append(recommendation_title_id)
    components.append(_text_component(recommendation_title_id, "建议动作", "body"))
    recommendations = _asset_recommendations(top_assets, high_risk)
    for index, recommendation in enumerate(recommendations, start=1):
        item_id = _component_id(surface_id, f"recommendation-{index}")
        child_ids.append(item_id)
        components.append(_text_component(item_id, recommendation, "body"))

    action_specs = [
        {
            "label": f"查看 {asset.get('assetName') or asset.get('assetId')}",
            "name": A2UI_ASSET_SELECT_ACTION,
            "primary": index == 1,
            "context": {
                "assetId": asset.get("assetId"),
                "assetName": asset.get("assetName") or asset.get("assetId"),
                "riskScore": _asset_score(asset),
            },
        }
        for index, asset in enumerate(top_assets[:3], start=1)
    ]
    action_components = _card_action_components(surface_id, 1, action_specs)
    child_ids.extend(action_components["root_ids"])
    components.extend(action_components["components"])
    components.append(_column_component(content_id, child_ids))

    return _required_protocol_payload(
        [
            {
                "surfaceUpdate": {
                    "surfaceId": surface_id,
                    "components": components,
                }
            }
        ]
    )


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


def _catalog_surface_id(payload: dict[str, Any]) -> str:
    return (
        _string_or_empty(payload.get("surfaceId"))
        or _string_or_empty(payload.get("surface_id"))
        or "ai-soc-catalog-surface"
    )


def _catalog_component_specs(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    component = payload.get("component")
    components = payload.get("components")
    if isinstance(component, dict):
        return [component]
    if isinstance(components, list) and all(isinstance(item, dict) for item in components):
        return components
    return None


def _catalog_component_to_card(component: dict[str, Any], index: int) -> tuple[dict[str, Any] | None, list[str]]:
    component_type = str(component.get("type") or "").strip()
    props = component.get("props")
    if component_type not in AI_SOC_A2UI_CATALOG_COMPONENTS:
        return None, [f"Invalid A2UI catalog component: unsupported type '{component_type}'."]
    if not isinstance(props, dict):
        return None, [f"Invalid A2UI catalog component '{component_type}': props must be an object."]

    if component_type == "RiskMetricGroup":
        return _risk_metric_group_card(props, index)
    if component_type == "RiskAssetTable":
        return _risk_asset_table_card(props, index)
    if component_type == "AlertTriageCard":
        return _alert_triage_card(props, index)
    return None, [f"Invalid A2UI catalog component: unsupported type '{component_type}'."]


def _risk_metric_group_card(props: dict[str, Any], index: int) -> tuple[dict[str, Any] | None, list[str]]:
    metrics = _list_of_records(props.get("metrics"))
    if not metrics:
        return None, ["Invalid RiskMetricGroup: metrics must be a non-empty array."]

    items: list[dict[str, str]] = []
    for item in metrics[:8]:
        label = _string_or_empty(item.get("label"))
        raw_value = item.get("value")
        value = _string_or_empty(raw_value) or (str(raw_value) if raw_value is not None else "")
        if label and value:
            items.append({"label": label, "value": value})
    if not items:
        return None, ["Invalid RiskMetricGroup: each metric needs label and value."]

    return (
        {
            "type": "card",
            "title": _string_or_empty(props.get("title")) or "风险指标",
            "subtitle": _string_or_empty(props.get("subtitle")),
            "sections": [
                {
                    "type": "metric_group",
                    "items": items,
                }
            ],
            "footer": _catalog_footer(index, "RiskMetricGroup"),
        },
        [],
    )


def _risk_asset_table_card(props: dict[str, Any], index: int) -> tuple[dict[str, Any] | None, list[str]]:
    assets = [_normalize_asset_for_card(asset) for asset in _list_of_records(props.get("assets"))[:10]]
    assets = [asset for asset in assets if asset.get("assetId")]
    if not assets:
        return None, ["Invalid RiskAssetTable: assets must include at least one asset with assetId or hostname."]

    rows = [
        [
            asset.get("assetName") or asset.get("assetId"),
            asset.get("ip") or "-",
            str(int(_asset_score(asset))),
            asset.get("type") or "-",
            asset.get("zone") or "-",
        ]
        for asset in assets
    ]
    actions = [
        {
            "label": f"查看 {asset.get('assetName') or asset.get('assetId')}",
            "name": A2UI_ASSET_SELECT_ACTION,
            "primary": action_index == 1,
            "context": {
                "assetId": asset.get("assetId"),
                "assetName": asset.get("assetName") or asset.get("assetId"),
                "riskScore": _asset_score(asset),
            },
        }
        for action_index, asset in enumerate(assets[:3], start=1)
    ]

    return (
        {
            "type": "card",
            "title": _string_or_empty(props.get("title")) or "风险资产列表",
            "subtitle": _string_or_empty(props.get("subtitle")),
            "sections": [
                {
                    "title": "资产",
                    "type": "table",
                    "columns": ["资产", "IP", "评分", "类型", "区域"],
                    "rows": rows,
                }
            ],
            "actions": actions,
            "footer": _catalog_footer(index, "RiskAssetTable"),
        },
        [],
    )


def _alert_triage_card(props: dict[str, Any], index: int) -> tuple[dict[str, Any] | None, list[str]]:
    title = _string_or_empty(props.get("title")) or _string_or_empty(props.get("alertTitle")) or "告警研判摘要"
    entity = _string_or_empty(props.get("entity")) or _string_or_empty(props.get("host")) or _string_or_empty(props.get("target"))
    key_values = {
        "严重度": _string_or_empty(props.get("severity")) or _string_or_empty(props.get("riskLevel")),
        "置信度": _string_or_empty(props.get("confidence")) or _string_or_empty(props.get("confidenceLabel")),
        "状态": _string_or_empty(props.get("status")),
        "对象": entity,
    }
    key_values = {key: value for key, value in key_values.items() if value}

    sections: list[dict[str, Any]] = []
    if key_values:
        sections.append({"title": "研判状态", "type": "key_value", "items": key_values})

    summary = _string_or_empty(props.get("summary")) or _string_or_empty(props.get("description"))
    if summary:
        sections.append({"title": "摘要", "items": [summary]})

    evidence = _string_items(props.get("evidence"))
    if evidence:
        sections.append({"title": "关键证据", "items": evidence[:6]})

    recommendations = _string_items(props.get("recommendations")) or _string_items(props.get("suggestions"))
    if recommendations:
        sections.append({"title": "建议动作", "items": recommendations[:6]})

    if not sections:
        return None, ["Invalid AlertTriageCard: provide at least status, summary, evidence, or recommendations."]

    return (
        {
            "type": "card",
            "title": title,
            "subtitle": _string_or_empty(props.get("subtitle")),
            "sections": sections,
            "footer": _catalog_footer(index, "AlertTriageCard"),
        },
        [],
    )


def _catalog_footer(index: int, component_type: str) -> str:
    return f"AI-SOC catalog: {component_type} #{index}"


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


def _normalize_asset_for_card(value: dict[str, Any]) -> dict[str, Any]:
    host = value.get("host") if isinstance(value.get("host"), dict) else {}
    asset_id = (
        _string_or_empty(value.get("assetId"))
        or _string_or_empty(value.get("asset_id"))
        or _string_or_empty(value.get("id"))
        or _string_or_empty(value.get("hostname"))
        or _string_or_empty(value.get("name"))
        or _string_or_empty(value.get("asset"))
        or _string_or_empty(host.get("hostname"))
    )
    asset_name = (
        _string_or_empty(value.get("assetName"))
        or _string_or_empty(value.get("asset_name"))
        or _string_or_empty(value.get("hostname"))
        or _string_or_empty(value.get("name"))
        or _string_or_empty(value.get("asset"))
        or _string_or_empty(host.get("hostname"))
        or asset_id
    )
    return {
        "assetId": asset_id,
        "assetName": asset_name,
        "riskScore": _number_or_zero(
            value.get("riskScore")
            or value.get("risk_score")
            or value.get("risk")
            or value.get("score")
            or value.get("risk_level")
        ),
        "ip": _string_or_empty(value.get("ip")) or _string_or_empty(host.get("ip")),
        "zone": (
            _string_or_empty(value.get("zone"))
            or _string_or_empty(value.get("area"))
            or _string_or_empty(value.get("network_zone"))
            or _string_or_empty(value.get("businessZone"))
        ),
        "type": _string_or_empty(value.get("type")) or _string_or_empty(value.get("asset_type")),
    }


def _asset_score(asset: dict[str, Any]) -> float:
    score = asset.get("riskScore")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        return float(score)
    return 0.0


def _asset_summary_text(asset: dict[str, Any]) -> str:
    parts = [
        str(asset.get("assetName") or asset.get("assetId")),
        str(asset.get("ip") or "").strip(),
        str(int(_asset_score(asset))),
        str(asset.get("type") or "").strip(),
        str(asset.get("zone") or "").strip(),
    ]
    return " | ".join(part for part in parts if part)


def _asset_recommendations(top_assets: list[dict[str, Any]], high_risk: list[dict[str, Any]]) -> list[str]:
    if not top_assets:
        return ["未识别到可排序资产，建议补充资产清单或风险评分字段。"]
    first = str(top_assets[0].get("assetName") or top_assets[0].get("assetId"))
    return [
        f"优先确认 {first} 是否存在异常访问、漏洞暴露或横向移动迹象。",
        f"对 {len(high_risk)} 台高风险资产补充告警、进程和网络证据链分析。",
    ]


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


def _replace_surface_references(value: Any, source_surface_id: str, target_surface_id: str) -> Any:
    if isinstance(value, str):
        return f"{target_surface_id}{value[len(source_surface_id):]}" if value.startswith(source_surface_id) else value
    if isinstance(value, list):
        return [_replace_surface_references(item, source_surface_id, target_surface_id) for item in value]
    if isinstance(value, dict):
        return {
            key: target_surface_id
            if key == "surfaceId" and item == source_surface_id
            else _replace_surface_references(item, source_surface_id, target_surface_id)
            for key, item in value.items()
        }
    return value


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
