"""Pure parsers for model-provider probe responses (OpenAI chat + Anthropic Messages).

Extracted from model_provider.py to keep that module under the architecture size
threshold. These helpers depend only on JsonObject and have no provider state.
"""

from __future__ import annotations

import json

from .json_types import JsonObject


def anthropic_tool_probe_body(model: str, *, system_in_messages: bool) -> JsonObject:
    """Anthropic Messages request that forces a tool_use. When `system_in_messages` is set it
    injects a `system`-role entry inside `messages` (the shape Claude Code emits) so strict vLLM
    schemas that reject it fail-close instead of passing a false positive."""
    messages: list[dict[str, object]] = []
    if system_in_messages:
        messages.append({"role": "system", "content": "You are a probe."})
    messages.append({"role": "user", "content": [{"type": "text", "text": "Call the agent_gov_probe tool with value ok."}]})
    return {
        "model": model,
        "max_tokens": 128,
        "messages": messages,
        "tools": [
            {
                "name": "agent_gov_probe",
                "description": "Return a probe value.",
                "input_schema": {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
            }
        ],
        "tool_choice": {"type": "tool", "name": "agent_gov_probe"},
    }


def first_choice_message(body: JsonObject | None) -> JsonObject:
    if not isinstance(body, dict):
        return {}
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return {}
    first = choices[0]
    if not isinstance(first, dict):
        return {}
    message = first.get("message")
    return message if isinstance(message, dict) else {}


def message_content_text(message: JsonObject) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                chunks.append(item["text"])
        return "\n".join(chunks).strip()
    return ""


def first_tool_call(message: JsonObject) -> JsonObject:
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list) or not tool_calls:
        return {}
    first = tool_calls[0]
    return first if isinstance(first, dict) else {}


def parse_json_object(value: str) -> JsonObject | None:
    try:
        loaded = json.loads(value)
    except Exception:
        return None
    return loaded if isinstance(loaded, dict) else None


def anthropic_content_has_tool_use(body: JsonObject | None) -> bool:
    if not isinstance(body, dict):
        return False
    content = body.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(item, dict) and item.get("type") == "tool_use" for item in content)
