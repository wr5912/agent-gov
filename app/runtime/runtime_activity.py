from __future__ import annotations

import json
from typing import Any, Optional

from .collection_utils import unique_strings
from .message_utils import to_plain
from .json_types import JsonObject
from .schemas import ChatRequest
from .settings import AppSettings

# 开发调试观测面保留完整 I/O（.claude/rules/agentgov-project.md），此上限只作 Langfuse 摄取安全阀，从宽。
_LANGFUSE_IO_MAX_CHARS = 200_000


def _io_payload(value: Any) -> Any:
    """把 SDK 值转成 JSON-safe 的 observation input/output，超大 payload 加截断标记（防摄取上限）。"""
    plain = to_plain(value)
    try:
        serialized = json.dumps(plain, ensure_ascii=False, default=str)
    except Exception:
        return str(plain)[:_LANGFUSE_IO_MAX_CHARS]
    if len(serialized) <= _LANGFUSE_IO_MAX_CHARS:
        return plain
    return {"_truncated": True, "original_chars": len(serialized), "preview": serialized[:_LANGFUSE_IO_MAX_CHARS]}


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

    def sdk_child_observations(self, messages: list[JsonObject]) -> list[dict[str, Any]]:
        """把 SDK message 流投影成 Langfuse 子观测描述符：逐工具 span（入参/结果）+ 逐轮 generation（报文/token）。

        数据源是 claude-agent-sdk 原生 message（ToolUseBlock.input / ToolResultBlock.content /
        AssistantMessage 每轮 content+usage），复用既有 walk/tool 抽取。纯数据、无 Langfuse 依赖。
        """
        if not messages:
            return []

        results_by_id: dict[str, JsonObject] = {}
        error_ids: set[str] = set()
        for message in messages:
            for record in self._walk_records(message):
                result = self._tool_result_from_record(record)
                if not result:
                    continue
                tid = self._string_value(result.get("tool_use_id"))
                if tid and tid not in results_by_id:
                    results_by_id[tid] = result
                    if record.get("is_error") is True:
                        error_ids.add(tid)

        children: list[dict[str, Any]] = []
        consumed: set[str] = set()
        pending_input: list[JsonObject] = []
        turn = 0
        for message in messages:
            if self._is_assistant_turn(message):
                turn += 1
                generation: dict[str, Any] = {
                    "kind": "generation",
                    "name": f"sdk.llm.{turn}",
                    "input": _io_payload(list(pending_input)) if pending_input else None,
                    "output": _io_payload(message.get("content")),
                }
                model = self._string_value(message.get("model"))
                if model:
                    generation["model"] = model
                usage = self.usage_details(message.get("usage"))
                if usage:
                    generation["usage_details"] = usage
                children.append(generation)
                pending_input = []
                for record in self._walk_records(message):
                    call = self._tool_call_from_record(record)
                    if not call:
                        continue
                    tid = self._string_value(call.get("tool_use_id"))
                    name = self._string_value(call.get("name")) or "tool"
                    result = results_by_id.get(tid) if tid else None
                    children.append(
                        {
                            "kind": "tool",
                            "name": f"sdk.tool.{name}",
                            "input": _io_payload(call.get("input")),
                            "output": _io_payload(result.get("content")) if result else None,
                            "level": "ERROR" if tid in error_ids else "DEFAULT",
                            "metadata": self._child_metadata(tid, call.get("agent_id")),
                        }
                    )
                    if tid:
                        consumed.add(tid)
            elif isinstance(message, dict):
                pending_input.append(message)

        for tid, result in results_by_id.items():
            if tid in consumed:
                continue
            name = self._string_value(result.get("name")) or "result"
            children.append(
                {
                    "kind": "tool",
                    "name": f"sdk.tool.{name}",
                    "input": None,
                    "output": _io_payload(result.get("content")),
                    "level": "ERROR" if tid in error_ids else "DEFAULT",
                    "metadata": self._child_metadata(tid, result.get("agent_id")),
                }
            )
        return children

    @staticmethod
    def _is_assistant_turn(message: Any) -> bool:
        return isinstance(message, dict) and isinstance(message.get("content"), list) and "model" in message

    @staticmethod
    def _child_metadata(tool_use_id: str | None, agent_id: Any) -> dict[str, str]:
        meta: dict[str, str] = {}
        if tool_use_id:
            meta["tool_use_id"] = tool_use_id
        if isinstance(agent_id, str) and agent_id:
            meta["agent_id"] = agent_id
        return meta

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
