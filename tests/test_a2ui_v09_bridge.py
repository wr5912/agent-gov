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


def test_normalize_a2ui_v09_tool_input_rejects_quoted_json():
    payload, errors = normalize_a2ui_v09_tool_input(
        {
            "message": '{"version":"v0.9","deleteSurface":{"surfaceId":"risk-surface"}}',
        }
    )

    assert payload is None
    assert "quoted JSON strings" in errors[0]


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
    assert result.messages == []
