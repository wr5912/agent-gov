from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


A2UI_BLOCK_PATTERN = re.compile(
    r"<a2ui-json>\s*(?P<payload>.*?)\s*</a2ui-json>",
    re.DOTALL | re.IGNORECASE,
)
A2UI_PROTOCOL_VERSION = "v0_8"
A2UI_CUSTOM_EVENT_NAME = "a2ui.message"
_A2UI_START_TAG = "<a2ui-json>"
_A2UI_END_TAG = "</a2ui-json>"


@dataclass(frozen=True)
class A2uiExtractionResult:
    text: str
    payloads: list[dict[str, Any]]
    errors: list[str]


class A2uiStreamExtractor:
    """Incrementally strips A2UI XML blocks and emits completed payloads."""

    def __init__(self) -> None:
        self._buffer = ""
        self._inside_block = False

    def feed(self, chunk: str) -> A2uiExtractionResult:
        self._buffer += chunk
        visible: list[str] = []
        payloads: list[dict[str, Any]] = []
        errors: list[str] = []

        while self._buffer:
            if self._inside_block:
                end_index = self._buffer.lower().find(_A2UI_END_TAG)
                if end_index < 0:
                    break

                raw_payload = self._buffer[:end_index]
                payload, error = _parse_a2ui_payload(raw_payload)
                if payload is not None:
                    payloads.append(payload)
                if error:
                    errors.extend(error)
                self._buffer = self._buffer[end_index + len(_A2UI_END_TAG) :]
                self._inside_block = False
                continue

            start_index = self._buffer.lower().find(_A2UI_START_TAG)
            if start_index >= 0:
                visible.append(self._buffer[:start_index])
                self._buffer = self._buffer[start_index + len(_A2UI_START_TAG) :]
                self._inside_block = True
                continue

            keep = _partial_tag_suffix_length(self._buffer, _A2UI_START_TAG)
            if keep:
                visible.append(self._buffer[:-keep])
                self._buffer = self._buffer[-keep:]
            else:
                visible.append(self._buffer)
                self._buffer = ""
            break

        return A2uiExtractionResult(
            text="".join(visible),
            payloads=payloads,
            errors=errors,
        )

    def finish(self) -> A2uiExtractionResult:
        if not self._buffer:
            return A2uiExtractionResult(text="", payloads=[], errors=[])

        if self._inside_block:
            self._buffer = ""
            self._inside_block = False
            return A2uiExtractionResult(
                text="",
                payloads=[],
                errors=["Invalid A2UI JSON: missing closing a2ui-json tag"],
            )

        text = self._buffer
        self._buffer = ""
        return A2uiExtractionResult(text=text, payloads=[], errors=[])


def extract_a2ui_payloads(text: str) -> A2uiExtractionResult:
    payloads: list[dict[str, Any]] = []
    errors: list[str] = []

    def replace_block(match: re.Match[str]) -> str:
        raw_payload = match.group("payload")
        payload, error = _parse_a2ui_payload(raw_payload)
        if payload is not None:
            payloads.append(payload)
        if error:
            errors.extend(error)
        return ""

    visible_text = A2UI_BLOCK_PATTERN.sub(replace_block, text).strip()
    return A2uiExtractionResult(
        text=visible_text,
        payloads=payloads,
        errors=errors,
    )


def _parse_a2ui_payload(raw_payload: str) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        return None, [f"Invalid A2UI JSON: {exc.msg}"]

    messages = _normalize_a2ui_messages(payload)
    if messages is None:
        return None, ["Invalid A2UI payload: expected a v0_8 message, message list, or {messages: [...]}."] 

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


def _partial_tag_suffix_length(value: str, tag: str) -> int:
    lowered = value.lower()
    tag = tag.lower()
    max_len = min(len(lowered), len(tag) - 1)
    for length in range(max_len, 0, -1):
        if lowered[-length:] == tag[:length]:
            return length
    return 0


def _normalize_a2ui_messages(payload: Any) -> list[dict[str, Any]] | None:
    if _is_a2ui_message(payload):
        return [payload]

    if isinstance(payload, list) and all(_is_a2ui_message(item) for item in payload):
        return payload

    if isinstance(payload, dict) and isinstance(payload.get("messages"), list):
        messages = payload["messages"]
        if all(_is_a2ui_message(item) for item in messages):
            return messages

    return None


def _is_a2ui_message(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    keys = {"beginRendering", "surfaceUpdate", "dataModelUpdate", "deleteSurface"}
    return sum(1 for key in keys if key in value) == 1 and "version" not in value


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
