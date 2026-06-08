from __future__ import annotations

import json
from typing import Any, Optional

from .collection_utils import unique_strings
from .message_utils import to_plain
from .json_types import JsonObject
from .schemas import ChatRequest
from .settings import AppSettings


class RuntimeActivityExtractor:
    """Extracts tool and skill activity from Claude SDK message payloads."""

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings

    def agent_activity_payload(self, req: ChatRequest, messages: list[JsonObject]) -> JsonObject:
        requested_skills = req.skills if req.skills is not None else self.settings.default_skills
        tool_calls: list[JsonObject] = []
        tool_results: list[JsonObject] = []
        seen_calls: set[str] = set()
        seen_results: set[str] = set()

        for message in messages:
            for record in self._walk_records(message):
                tool_call = self._tool_call_from_record(record)
                if tool_call:
                    self._append_unique(tool_calls, tool_call, seen_calls)

                tool_result = self._tool_result_from_record(record)
                if tool_result:
                    self._append_unique(tool_results, tool_result, seen_results)

        tool_names = self._unique_strings(
            name for call in tool_calls for name in [self._string_value(call.get("name"))] if name
        )
        skill_calls = [
            skill_call
            for call in tool_calls
            for skill_call in [self._skill_call_from_tool_call(call)]
            if skill_call
        ]

        return {
            "requested_skills": list(requested_skills or []),
            "skills_mode": req.skills_mode or self.settings.default_skills_mode,
            "allowed_tools": None,
            "disallowed_tools": None,
            "claude_config_source": "official_files",
            "tool_names": tool_names,
            "tool_calls": tool_calls,
            "tool_results": tool_results,
            "skill_calls": skill_calls,
        }

    @staticmethod
    def usage_details(usage: Any) -> Optional[dict[str, int]]:
        plain = to_plain(usage)
        if not isinstance(plain, dict):
            return None
        details: dict[str, int] = {}
        for key, value in plain.items():
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                details[str(key)] = value
        return details or None

    @staticmethod
    def cost_details(total_cost_usd: Optional[float]) -> Optional[dict[str, float]]:
        if total_cost_usd is None:
            return None
        return {"total_cost_usd": float(total_cost_usd)}

    def _walk_records(self, value: Any) -> list[JsonObject]:
        records: list[JsonObject] = []
        if isinstance(value, dict):
            records.append(value)
            for item in value.values():
                records.extend(self._walk_records(item))
        elif isinstance(value, list):
            for item in value:
                records.extend(self._walk_records(item))
        return records

    def _tool_call_from_record(self, record: JsonObject) -> JsonObject | None:
        record_type = self._string_value(record.get("type")) or ""
        hook_event = self._hook_event(record) or ""
        name = self._tool_name(record)
        is_tool_use = "tool_use" in record_type.lower()
        has_tool_use_shape = bool(name and "input" in record and any(key in record for key in ("id", "tool_use_id", "toolUseID")))
        is_hook_tool_use = hook_event in {"PreToolUse", "PermissionRequest"}
        if not name or not (is_tool_use or has_tool_use_shape or is_hook_tool_use):
            return None

        entry: JsonObject = {"name": name}
        self._copy_first(record, entry, ("id", "tool_use_id", "toolUseID"), "tool_use_id")
        self._copy_first(record, entry, ("input", "tool_input", "toolInput"), "input")
        self._copy_first(record, entry, ("agent_id", "agentId"), "agent_id")
        self._copy_first(record, entry, ("agent_type", "agentType"), "agent_type")
        self._copy_first(record, entry, ("session_id", "sessionId"), "session_id")
        if hook_event:
            entry["hook_event_name"] = hook_event
        return entry

    def _tool_result_from_record(self, record: JsonObject) -> JsonObject | None:
        record_type = self._string_value(record.get("type")) or ""
        hook_event = self._hook_event(record) or ""
        is_tool_result = "tool_result" in record_type.lower()
        has_tool_result_shape = "tool_use_id" in record and "content" in record
        is_hook_result = hook_event in {"PostToolUse", "PostToolUseFailure"}
        if not (is_tool_result or has_tool_result_shape or is_hook_result):
            return None

        entry: JsonObject = {}
        self._copy_first(record, entry, ("tool_use_id", "toolUseID", "id"), "tool_use_id")
        self._copy_first(record, entry, ("tool_name", "toolName", "name"), "name")
        self._copy_first(record, entry, ("content", "tool_response", "toolResponse", "error"), "content")
        self._copy_first(record, entry, ("agent_id", "agentId"), "agent_id")
        self._copy_first(record, entry, ("agent_type", "agentType"), "agent_type")
        if "name" not in entry:
            name = self._tool_name(record)
            if name:
                entry["name"] = name
        if hook_event:
            entry["hook_event_name"] = hook_event
        return entry or None

    def _skill_call_from_tool_call(self, call: JsonObject) -> JsonObject | None:
        tool_name = self._string_value(call.get("name")) or ""
        if tool_name != "Skill" and not tool_name.startswith("Skill("):
            return None

        skill_name = None
        if tool_name.startswith("Skill(") and tool_name.endswith(")"):
            skill_name = tool_name.removeprefix("Skill(").removesuffix(")")

        input_value = call.get("input")
        if isinstance(input_value, dict):
            skill_name = (
                self._string_value(input_value.get("skill"))
                or self._string_value(input_value.get("name"))
                or self._string_value(input_value.get("skill_name"))
                or skill_name
            )

        entry = {"tool_name": tool_name}
        if skill_name:
            entry["name"] = skill_name
        if "tool_use_id" in call:
            entry["tool_use_id"] = call["tool_use_id"]
        if "input" in call:
            entry["input"] = call["input"]
        return entry

    def _tool_name(self, record: JsonObject) -> str | None:
        direct = (
            self._string_value(record.get("name"))
            or self._string_value(record.get("tool_name"))
            or self._string_value(record.get("toolName"))
        )
        if direct:
            return direct

        hook_name = self._string_value(record.get("hook_name"))
        if hook_name and ":" in hook_name:
            return hook_name.split(":", 1)[1] or None
        return None

    def _hook_event(self, record: JsonObject) -> str | None:
        direct = (
            self._string_value(record.get("hook_event_name"))
            or self._string_value(record.get("hook_event"))
        )
        if direct:
            return direct

        hook_name = self._string_value(record.get("hook_name"))
        if hook_name and ":" in hook_name:
            return hook_name.split(":", 1)[0] or None
        return None

    @staticmethod
    def _copy_first(
        source: JsonObject,
        target: JsonObject,
        candidates: tuple[str, ...],
        target_key: str,
    ) -> None:
        for key in candidates:
            if key in source:
                target[target_key] = source[key]
                return

    @staticmethod
    def _append_unique(items: list[JsonObject], entry: JsonObject, seen: set[str]) -> None:
        key = json.dumps(entry, sort_keys=True, ensure_ascii=False, default=str)
        if key in seen:
            return
        seen.add(key)
        items.append(entry)

    @staticmethod
    def _unique_strings(values: Any) -> list[str]:
        return unique_strings(values)

    @staticmethod
    def _string_value(value: Any) -> str | None:
        return value if isinstance(value, str) and value else None
