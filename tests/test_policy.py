import asyncio

from app.runtime.policy import pre_tool_use_hook


def test_pre_tool_use_hook_blocks_legacy_a2ui_tool():
    result = asyncio.run(
        pre_tool_use_hook(
            {
                "tool_name": "mcp__ai-soc-ui__emit_a2ui",
                "tool_input": {"messages": []},
            },
            None,
            {},
        )
    )

    output = result["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert "Legacy A2UI tools are disabled" in output["permissionDecisionReason"]


def test_pre_tool_use_hook_blocks_v08_payload_on_v09_tool():
    result = asyncio.run(
        pre_tool_use_hook(
            {
                "tool_name": "mcp__ai-soc-ui__emit_a2ui_message",
                "tool_input": {
                    "message": {
                        "protocol": "a2ui",
                        "version": "v0_8",
                        "messages": [],
                    }
                },
            },
            None,
            {},
        )
    )

    output = result["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert "only accepts A2UI v0.9 messages" in output["permissionDecisionReason"]


def test_pre_tool_use_hook_allows_valid_v09_message():
    result = asyncio.run(
        pre_tool_use_hook(
            {
                "tool_name": "mcp__ai-soc-ui__emit_a2ui_message",
                "tool_input": {
                    "message": {
                        "version": "v0.9",
                        "createSurface": {
                            "surfaceId": "asset-risk-overview",
                            "catalogId": "https://a2ui.org/specification/v0_9/basic_catalog.json",
                        },
                    }
                },
            },
            None,
            {},
        )
    )

    assert result == {}


def test_pre_tool_use_hook_blocks_update_components_without_surface_id():
    result = asyncio.run(
        pre_tool_use_hook(
            {
                "tool_name": "mcp__ai-soc-ui__emit_a2ui_message",
                "tool_input": {
                    "message": {
                        "version": "v0.9",
                        "updateComponents": {
                            "components": [
                                {"id": "root", "component": "Card", "child": "content"},
                            ],
                        },
                    }
                },
            },
            None,
            {},
        )
    )

    output = result["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert "surfaceId is required" in output["permissionDecisionReason"]


def test_pre_tool_use_hook_blocks_update_components_object_map():
    result = asyncio.run(
        pre_tool_use_hook(
            {
                "tool_name": "mcp__ai-soc-ui__emit_a2ui_message",
                "tool_input": {
                    "message": {
                        "version": "v0.9",
                        "updateComponents": {
                            "surfaceId": "asset-risk-overview",
                            "components": {
                                "root": {"component": "Card", "child": "content"},
                            },
                        },
                    }
                },
            },
            None,
            {},
        )
    )

    output = result["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert "components must be a non-empty array" in output["permissionDecisionReason"]
