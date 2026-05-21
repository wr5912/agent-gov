from app.runtime.a2ui_v09_bridge import (
    A2UI_V09_MESSAGE_TOOL_NAME,
    extract_a2ui_v09_tool_messages,
    normalize_a2ui_v09_tool_input,
)


class _ObjectMessage:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def test_normalize_a2ui_v09_tool_input_accepts_one_message():
    message = {
        "version": "v0.9",
        "createSurface": {
            "surfaceId": "risk-surface",
            "catalogId": "https://a2ui.org/specification/v0_9/basic_catalog.json",
        },
    }

    payload, errors = normalize_a2ui_v09_tool_input({"message": message})

    assert errors == []
    assert payload == message


def test_normalize_a2ui_v09_tool_input_rejects_arrays():
    payload, errors = normalize_a2ui_v09_tool_input(
        {
            "message": [
                {
                    "version": "v0.9",
                    "deleteSurface": {"surfaceId": "risk-surface"},
                }
            ]
        }
    )

    assert payload is None
    assert "not an array" in errors[0]


def test_normalize_a2ui_v09_tool_input_accepts_recoverable_quoted_json():
    payload, errors = normalize_a2ui_v09_tool_input(
        {
            "message": '{"version":"v0.9","deleteSurface":{"surfaceId":"risk-surface"}}',
        }
    )

    assert errors == []
    assert payload == {
        "version": "v0.9",
        "deleteSurface": {"surfaceId": "risk-surface"},
    }


def test_normalize_a2ui_v09_tool_input_rejects_v08_messages():
    payload, errors = normalize_a2ui_v09_tool_input(
        {
            "message": {
                "beginRendering": {"surfaceId": "legacy", "root": "root"},
            }
        }
    )

    assert payload is None
    assert "version must be 'v0.9'" in errors[0]


def test_extract_a2ui_v09_tool_messages_finds_tool_call():
    raw_message = {
        "type": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": "toolu-v09",
                "name": A2UI_V09_MESSAGE_TOOL_NAME,
                "input": {
                    "message": {
                        "version": "v0.9",
                        "updateComponents": {
                            "surfaceId": "risk-surface",
                            "components": [
                                {"id": "root", "component": "Card", "child": "content"},
                            ],
                        },
                    }
                },
            }
        ],
    }

    result = extract_a2ui_v09_tool_messages(raw_message)

    assert result.errors == []
    assert result.warnings == []
    assert result.messages == [
        {
            "version": "v0.9",
            "updateComponents": {
                "surfaceId": "risk-surface",
                "components": [
                    {"id": "root", "component": "Card", "child": "content"},
                ],
            },
        }
    ]


def test_extract_a2ui_v09_tool_messages_finds_sdk_object_tool_call():
    raw_message = _ObjectMessage(
        type="assistant",
        content=[
            _ObjectMessage(
                type="tool_use",
                id="toolu-v09-object",
                name=A2UI_V09_MESSAGE_TOOL_NAME,
                input={
                    "message": {
                        "version": "v0.9",
                        "createSurface": {
                            "surfaceId": "risk-surface",
                            "catalogId": "https://a2ui.org/specification/v0_9/basic_catalog.json",
                        },
                    }
                },
            )
        ],
    )

    result = extract_a2ui_v09_tool_messages(raw_message)

    assert result.errors == []
    assert result.warnings == []
    assert result.messages == [
        {
            "version": "v0.9",
            "createSurface": {
                "surfaceId": "risk-surface",
                "catalogId": "https://a2ui.org/specification/v0_9/basic_catalog.json",
            },
        }
    ]


def test_extract_a2ui_v09_tool_messages_finds_pre_tool_use_hook_input():
    raw_message = {
        "hook_event_name": "PreToolUse",
        "tool_name": A2UI_V09_MESSAGE_TOOL_NAME,
        "tool_input": {
            "message": {
                "version": "v0.9",
                "updateDataModel": {
                    "surfaceId": "risk-surface",
                    "path": "$.riskAssets",
                    "data": [{"asset": "db-01", "score": 91}],
                },
            }
        },
    }

    result = extract_a2ui_v09_tool_messages(raw_message)

    assert result.errors == []
    assert result.warnings == []
    assert result.messages == [
        {
            "version": "v0.9",
            "updateDataModel": {
                "surfaceId": "risk-surface",
                "path": "$.riskAssets",
                "data": [{"asset": "db-01", "score": 91}],
            },
        }
    ]


def test_extract_a2ui_v09_tool_messages_ignores_pre_tool_use_hook_without_input():
    raw_message = {
        "hook_event_name": "PreToolUse",
        "tool_name": A2UI_V09_MESSAGE_TOOL_NAME,
        "session_id": "sdk-session",
    }

    result = extract_a2ui_v09_tool_messages(raw_message)

    assert result.errors == []
    assert result.warnings == []
    assert result.messages == []


def test_normalize_a2ui_v09_tool_input_repairs_type_component_alias():
    payload, errors = normalize_a2ui_v09_tool_input(
        {
            "message": {
                "version": "v0.9",
                "updateComponents": {
                    "surfaceId": "risk-surface",
                    "components": [
                        {"id": "root", "type": "card", "child": "content"},
                    ],
                },
            }
        }
    )

    assert errors == []
    assert payload == {
        "version": "v0.9",
        "updateComponents": {
            "surfaceId": "risk-surface",
            "components": [
                {"id": "root", "component": "Card", "child": "content"},
            ],
        },
    }


def test_normalize_a2ui_v09_tool_input_converts_legacy_card_components():
    payload, errors = normalize_a2ui_v09_tool_input(
        {
            "message": {
                "version": "v0.9",
                "updateComponents": {
                    "surfaceId": "risk-surface",
                    "components": [
                        {
                            "type": "card",
                            "id": "top-risk-assets",
                            "title": "TOP 高风险资产",
                            "sections": [
                                {
                                    "type": "table",
                                    "columns": [
                                        {"key": "hostname", "label": "主机名"},
                                        {"key": "risk_score", "label": "风险分"},
                                    ],
                                    "rows": [
                                        {"hostname": "vpn-07", "risk_score": 98},
                                    ],
                                }
                            ],
                        }
                    ],
                },
            }
        }
    )

    assert errors == []
    assert payload is not None
    components = payload["updateComponents"]["components"]
    assert {"id": "root", "component": "Card", "child": "top-risk-assets-body"} in components
    assert {
        "id": "top-risk-assets-title",
        "component": "Text",
        "text": "TOP 高风险资产",
        "variant": "h3",
    } in components
    assert any(
        component.get("component") == "Text"
        and "主机名: vpn-07" in str(component.get("text"))
        and "风险分: 98" in str(component.get("text"))
        for component in components
    )


def test_normalize_a2ui_v09_tool_input_converts_standalone_table_components():
    payload, errors = normalize_a2ui_v09_tool_input(
        {
            "message": {
                "version": "v0.9",
                "updateComponents": {
                    "surfaceId": "risk-surface",
                    "components": [
                        {
                            "id": "critical-table",
                            "component": "table",
                            "title": "关键风险资产",
                            "columns": [
                                {"key": "hostname", "label": "主机名"},
                                {"key": "score", "label": "风险分"},
                            ],
                            "rows": [
                                {"hostname": "vpn-07", "score": 99},
                            ],
                        }
                    ],
                },
            }
        }
    )

    assert errors == []
    assert payload is not None
    components = payload["updateComponents"]["components"]
    assert {"id": "root", "component": "Card", "child": "critical-table-body"} in components
    assert all(component.get("component") != "table" for component in components)
    assert any(
        component.get("component") == "Text"
        and "主机名: vpn-07" in str(component.get("text"))
        and "风险分: 99" in str(component.get("text"))
        for component in components
    )


def test_normalize_a2ui_v09_tool_input_rejects_unregistered_components():
    payload, errors = normalize_a2ui_v09_tool_input(
        {
            "message": {
                "version": "v0.9",
                "updateComponents": {
                    "surfaceId": "risk-surface",
                    "components": [
                        {"id": "chart", "component": "Chart", "data": []},
                    ],
                },
            }
        }
    )

    assert payload is None
    assert "unregistered component 'Chart'" in errors[0]


def test_normalize_a2ui_v09_tool_input_repairs_official_basic_catalog_child_fields():
    payload, errors = normalize_a2ui_v09_tool_input(
        {
            "message": {
                "version": "v0.9",
                "updateComponents": {
                    "surfaceId": "risk-surface",
                    "components": [
                        {
                            "id": "summary-card",
                            "component": "Card",
                            "children": ["summary-title", "summary-list"],
                        },
                        {"id": "summary-title", "component": "Text", "text": "高风险资产概览"},
                        {
                            "id": "summary-list",
                            "component": "List",
                            "items": ["asset-1", "asset-2"],
                        },
                        {"id": "asset-1", "component": "Text", "text": "vpn-07 | 风险 99"},
                        {"id": "asset-2", "component": "Text", "text": "db-core-21 | 风险 94"},
                    ],
                },
            }
        }
    )

    assert errors == []
    assert payload is not None
    components = payload["updateComponents"]["components"]
    assert {
        "id": "summary-card-body",
        "component": "Column",
        "children": ["summary-title", "summary-list"],
        "align": "stretch",
    } in components
    assert {"id": "summary-card", "component": "Card", "child": "summary-card-body"} in components
    assert {"id": "summary-list", "component": "List", "children": ["asset-1", "asset-2"]} in components
    assert all("children" not in component for component in components if component.get("component") == "Card")
    assert all("items" not in component for component in components if component.get("component") == "List")


def test_extract_a2ui_v09_tool_messages_reports_repair_warnings():
    raw_message = {
        "type": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": "toolu-v09-repaired",
                "name": A2UI_V09_MESSAGE_TOOL_NAME,
                "input": {
                    "message": '{"version":"v0.9","deleteSurface":{"surfaceId":"risk-surface"}}',
                },
            }
        ],
    }

    result = extract_a2ui_v09_tool_messages(raw_message)

    assert result.errors == []
    assert result.messages == [
        {"version": "v0.9", "deleteSurface": {"surfaceId": "risk-surface"}},
    ]
    assert result.warnings == [
        "Recovered A2UI v0.9 message from a quoted JSON string; pass a structured object instead.",
    ]


def test_normalize_a2ui_v09_tool_input_repairs_props_nested_children_and_missing_ids():
    payload, errors = normalize_a2ui_v09_tool_input(
        {
            "message": {
                "version": "v0.9",
                "updateComponents": {
                    "surfaceId": "risk-surface",
                    "components": [
                        {
                            "component": "Row",
                            "id": "metrics",
                            "props": {
                                "children": [
                                    {
                                        "component": "Card",
                                        "id": "metric-critical",
                                        "props": {"title": "Critical", "subtitle": "4"},
                                    },
                                    {"component": "Divider"},
                                ]
                            },
                        }
                    ],
                },
            }
        }
    )

    assert errors == []
    assert payload is not None
    components = payload["updateComponents"]["components"]
    assert {"id": "root", "component": "Column", "children": ["metrics"], "align": "stretch"} in components
    assert {"id": "metric-critical-title", "component": "Text", "text": "Critical", "variant": "h3"} in components
    assert {
        "id": "metric-critical-subtitle",
        "component": "Text",
        "text": "4",
        "variant": "caption",
    } in components
    assert {
        "id": "metric-critical-body",
        "component": "Column",
        "children": ["metric-critical-title", "metric-critical-subtitle"],
        "align": "stretch",
    } in components
    assert {
        "id": "metric-critical",
        "component": "Card",
        "child": "metric-critical-body",
    } in components
    assert any(component.get("component") == "Divider" and component.get("id") for component in components)
    assert {"component": "Row", "id": "metrics", "children": ["metric-critical", "metrics-children-2"]} in components
