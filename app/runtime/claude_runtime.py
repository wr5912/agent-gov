from __future__ import annotations

import json
import os
import warnings
from contextlib import nullcontext
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Optional

from .agent_loader import load_programmatic_agents
from .ag_ui import (
    RunAgentInput,
    custom_event,
    run_error_event,
    run_finished_event,
    run_input_to_chat_request,
    run_started_event,
    text_message_content_event,
    text_message_end_event,
    text_message_start_event,
)
from .a2ui_bridge import A2UI_CUSTOM_EVENT_NAME, extract_a2ui_tool_payloads
from .message_utils import extract_stream_event_text, extract_text, message_event_name, to_plain
from .policy import build_default_hooks, guard_tool_use
from .schemas import ChatRequest
from .session_store import LocalSession, LocalSessionStore
from .settings import AppSettings


AGENT_ACTIVITY_EVENT_NAME = "ai_soc.agent.activity"


def _is_internal_skill_payload(text: str) -> bool:
    first_line = text.splitlines()[0] if text else ""
    return first_line.startswith("Base directory for this skill:") and "/.claude/skills/" in first_line


class ClaudeRuntime:
    """Thin runtime adapter around Claude Agent SDK.

    Design goals:
    - Keep Claude native config on disk: CLAUDE.md, .claude/settings.json,
      .claude/agents/*.md, .claude/skills/*/SKILL.md, .mcp.json.
    - Expose a stable HTTP API around it.
    - Persist a lightweight mapping from API session ids to Claude SDK session ids.
    """

    def __init__(self, settings: AppSettings, session_store: LocalSessionStore) -> None:
        self.settings = settings
        self.session_store = session_store
        self._langfuse_client: Any | None = None
        self._langfuse_unavailable = False

    def _build_prompt(self, req: ChatRequest) -> str:
        parts: list[str] = []
        agent = req.agent or self.settings.default_agent
        skills = req.skills if req.skills is not None else self.settings.default_skills
        if agent:
            parts.append(
                f"请优先委派或使用名为 `{agent}` 的 Claude Code subagent 处理本次任务；"
                "如果运行时无法直接切换到该 subagent，则按该 subagent 的职责边界执行。"
            )
        if skills:
            parts.append(f"本次任务优先使用这些 Skills：{', '.join(skills)}。")
        parts.append(req.message)
        return "\n\n".join(parts)

    async def _single_prompt_stream(self, prompt: str) -> AsyncIterator[dict[str, Any]]:
        yield {
            "type": "user",
            "message": {"role": "user", "content": prompt},
            "parent_tool_use_id": None,
            "session_id": "default",
        }

    def _skills_option(self, req: ChatRequest) -> Any:
        skills_mode = req.skills_mode or self.settings.default_skills_mode
        if skills_mode == "all":
            return "all"
        if skills_mode == "none":
            return []
        if req.skills:
            return req.skills
        if self.settings.default_skills:
            return self.settings.default_skills
        return None

    def _result_errors(self, msg: Any) -> list[str]:
        raw_errors = getattr(msg, "errors", None) or []
        if raw_errors:
            return [str(error) for error in raw_errors]
        if not getattr(msg, "is_error", False):
            return []

        result = getattr(msg, "result", None)
        if isinstance(result, str) and result.strip():
            status = getattr(msg, "api_error_status", None)
            status_part = f" ({status})" if status else ""
            return [f"Claude Code API error{status_part}: {result.strip()}"]

        subtype = getattr(msg, "subtype", None) or "unknown"
        return [f"Claude Code returned an error result: {subtype}"]

    def _should_suppress_exception(self, exc: Exception, errors: list[str]) -> bool:
        if not errors:
            return False
        text = str(exc)
        return text.startswith("Claude Code returned an error result:")

    def _dedupe_answer_parts(self, parts: list[str]) -> str:
        seen: set[str] = set()
        unique: list[str] = []
        for part in parts:
            text = part.strip()
            if not text or text in seen:
                continue
            seen.add(text)
            unique.append(text)
        return "\n".join(unique).strip()

    def _request_telemetry_input(self, req: ChatRequest, prompt: str, session: LocalSession) -> dict[str, Any]:
        allowed_tools = req.allowed_tools if req.allowed_tools is not None else self.settings.default_allowed_tools
        disallowed_tools = (
            req.disallowed_tools
            if req.disallowed_tools is not None
            else self.settings.default_disallowed_tools
        )
        return {
            "message": req.message,
            "prompt": prompt,
            "api_session_id": session.session_id,
            "sdk_session_id": session.sdk_session_id,
            "agent": req.agent or self.settings.default_agent,
            "skills": req.skills if req.skills is not None else self.settings.default_skills,
            "skills_mode": req.skills_mode or self.settings.default_skills_mode,
            "allowed_tools": allowed_tools,
            "disallowed_tools": disallowed_tools,
            "max_turns": req.max_turns or self.settings.max_turns,
            "model": req.model or self.settings.agent_model,
            "permission_mode": req.permission_mode or self.settings.permission_mode,
            "system_append": req.system_append,
            "metadata": req.metadata,
        }

    def _runtime_output_payload(
        self,
        *,
        session: LocalSession,
        sdk_session_id: Optional[str],
        answer: str,
        messages: list[dict[str, Any]],
        agent_activity: dict[str, Any],
        usage: Any,
        total_cost_usd: Optional[float],
        stop_reason: Optional[str],
        errors: list[str],
    ) -> dict[str, Any]:
        return {
            "api_session_id": session.session_id,
            "sdk_session_id": sdk_session_id,
            "answer": answer,
            "messages": messages,
            "agent_activity": agent_activity,
            "usage": to_plain(usage),
            "total_cost_usd": total_cost_usd,
            "stop_reason": stop_reason,
            "errors": errors,
        }

    def _agent_activity_payload(self, req: ChatRequest, messages: list[dict[str, Any]]) -> dict[str, Any]:
        allowed_tools = req.allowed_tools if req.allowed_tools is not None else self.settings.default_allowed_tools
        disallowed_tools = (
            req.disallowed_tools
            if req.disallowed_tools is not None
            else self.settings.default_disallowed_tools
        )
        requested_skills = req.skills if req.skills is not None else self.settings.default_skills
        tool_calls: list[dict[str, Any]] = []
        tool_results: list[dict[str, Any]] = []
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
            "allowed_tools": allowed_tools,
            "disallowed_tools": disallowed_tools,
            "tool_names": tool_names,
            "tool_calls": tool_calls,
            "tool_results": tool_results,
            "skill_calls": skill_calls,
        }

    def _walk_records(self, value: Any) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        if isinstance(value, dict):
            records.append(value)
            for item in value.values():
                records.extend(self._walk_records(item))
        elif isinstance(value, list):
            for item in value:
                records.extend(self._walk_records(item))
        return records

    def _tool_call_from_record(self, record: dict[str, Any]) -> dict[str, Any] | None:
        record_type = self._string_value(record.get("type")) or ""
        hook_event = self._hook_event(record) or ""
        name = self._tool_name(record)
        is_tool_use = "tool_use" in record_type.lower()
        has_tool_use_shape = bool(name and "input" in record and any(key in record for key in ("id", "tool_use_id", "toolUseID")))
        is_hook_tool_use = hook_event in {"PreToolUse", "PermissionRequest"}
        if not name or not (is_tool_use or has_tool_use_shape or is_hook_tool_use):
            return None

        entry: dict[str, Any] = {"name": name}
        self._copy_first(record, entry, ("id", "tool_use_id", "toolUseID"), "tool_use_id")
        self._copy_first(record, entry, ("input", "tool_input", "toolInput"), "input")
        self._copy_first(record, entry, ("agent_id", "agentId"), "agent_id")
        self._copy_first(record, entry, ("agent_type", "agentType"), "agent_type")
        self._copy_first(record, entry, ("session_id", "sessionId"), "session_id")
        if hook_event:
            entry["hook_event_name"] = hook_event
        return entry

    def _tool_result_from_record(self, record: dict[str, Any]) -> dict[str, Any] | None:
        record_type = self._string_value(record.get("type")) or ""
        hook_event = self._hook_event(record) or ""
        is_tool_result = "tool_result" in record_type.lower()
        has_tool_result_shape = "tool_use_id" in record and "content" in record
        is_hook_result = hook_event in {"PostToolUse", "PostToolUseFailure"}
        if not (is_tool_result or has_tool_result_shape or is_hook_result):
            return None

        entry: dict[str, Any] = {}
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

    def _agent_activity_events_from_raw(
        self,
        raw_message: Any,
        tool_context: dict[str, dict[str, str]],
    ) -> list[dict[str, Any]]:
        activities: list[dict[str, Any]] = []
        for record in self._walk_records(raw_message):
            tool_call = self._tool_call_from_record(record)
            if tool_call:
                activity = self._activity_from_tool_call(tool_call)
                activities.append(activity)
                tool_use_id = self._string_value(tool_call.get("tool_use_id"))
                if tool_use_id:
                    tool_context[tool_use_id] = {
                        "toolName": self._string_value(tool_call.get("name")) or "",
                        "label": activity["label"],
                    }

            tool_result = self._tool_result_from_record(record)
            if tool_result:
                activities.append(self._activity_from_tool_result(tool_result, tool_context))
        return activities

    def _activity_from_tool_call(self, call: dict[str, Any]) -> dict[str, Any]:
        tool_name = self._string_value(call.get("name")) or "unknown"
        label, detail = self._tool_activity_copy(tool_name, "running")
        tool_use_id = self._string_value(call.get("tool_use_id")) or self._activity_id("tool-call", tool_name)
        kind = "ui_generation" if tool_name.startswith("mcp__ai-soc-ui__") else "tool_call"
        return {
            "activityId": tool_use_id,
            "kind": kind,
            "status": "running",
            "label": label,
            "detail": detail,
            "toolName": tool_name,
            "toolUseId": tool_use_id,
        }

    def _activity_from_tool_result(
        self,
        result: dict[str, Any],
        tool_context: dict[str, dict[str, str]],
    ) -> dict[str, Any]:
        tool_use_id = self._string_value(result.get("tool_use_id"))
        context = tool_context.get(tool_use_id or "", {})
        tool_name = self._string_value(result.get("name")) or context.get("toolName") or "unknown"
        status = "error" if self._is_tool_result_error(result) else "finished"
        label, detail = self._tool_activity_copy(tool_name, status)
        label = context.get("label") or label
        kind = "ui_generation" if tool_name.startswith("mcp__ai-soc-ui__") else "tool_result"
        return {
            "activityId": tool_use_id or self._activity_id("tool-result", tool_name),
            "kind": kind,
            "status": status,
            "label": label,
            "detail": detail,
            "toolName": tool_name,
            "toolUseId": tool_use_id,
        }

    def _tool_activity_copy(self, tool_name: str, status: str) -> tuple[str, str]:
        action = self._tool_activity_label(tool_name)
        if status == "running":
            return action, self._tool_activity_running_detail(tool_name, action)
        if status == "error":
            return action, f"{action}失败，已保留当前回复上下文。"
        return action, f"{action}完成。"

    def _tool_activity_label(self, tool_name: str) -> str:
        if tool_name == "mcp__ai-soc-ui__emit_cards":
            return "生成结构化视图"
        if tool_name == "mcp__ai-soc-ui__emit_a2ui":
            return "生成 A2UI 视图"
        if tool_name.startswith("Skill(") and tool_name.endswith(")"):
            skill_name = tool_name.removeprefix("Skill(").removesuffix(")")
            if skill_name == "ai-soc-a2ui-response":
                return "准备结构化响应策略"
            return f"加载 {skill_name} 能力"
        if tool_name == "Skill":
            return "加载 Agent 能力"
        if tool_name == "Read":
            return "读取上下文文件"
        if tool_name == "Grep":
            return "检索上下文内容"
        if tool_name == "Glob":
            return "查找上下文文件"

        lowered = tool_name.lower()
        if "list_assets" in lowered or "_assets_" in lowered:
            return "查询资产列表"
        if "alert" in lowered:
            return "查询告警数据"
        if "event" in lowered:
            return "查询安全事件"
        if "ioc" in lowered or "indicator" in lowered:
            return "查询威胁指标"
        if "vulnerab" in lowered or "cve" in lowered:
            return "查询漏洞数据"
        if tool_name.startswith("mcp__sec-ops-data__"):
            return "查询安全运营数据"
        if tool_name.startswith("mcp__"):
            return "调用外部工具"
        return "执行工具调用"

    def _tool_activity_running_detail(self, tool_name: str, action: str) -> str:
        if tool_name.startswith("mcp__sec-ops-data__"):
            return f"正在从安全运营数据源{action.replace('查询', '获取')}。"
        if tool_name.startswith("mcp__ai-soc-ui__"):
            return "正在将 Agent 结果转换为可渲染的结构化 UI。"
        if tool_name.startswith("Skill") or tool_name == "Skill":
            return "正在加载任务相关能力和响应约束。"
        return f"正在{action}。"

    def _is_tool_result_error(self, result: dict[str, Any]) -> bool:
        hook_event = self._string_value(result.get("hook_event_name")) or ""
        if hook_event == "PostToolUseFailure":
            return True
        content = result.get("content")
        if isinstance(content, dict):
            if content.get("is_error") is True or content.get("ok") is False:
                return True
        return False

    def _activity_id(self, prefix: str, value: str) -> str:
        clean = "".join(char if char.isalnum() else "-" for char in value).strip("-")
        return f"{prefix}-{clean or 'unknown'}"

    def _skill_call_from_tool_call(self, call: dict[str, Any]) -> dict[str, Any] | None:
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

    def _tool_name(self, record: dict[str, Any]) -> str | None:
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

    def _hook_event(self, record: dict[str, Any]) -> str | None:
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

    def _copy_first(
        self,
        source: dict[str, Any],
        target: dict[str, Any],
        candidates: tuple[str, ...],
        target_key: str,
    ) -> None:
        for key in candidates:
            if key in source:
                target[target_key] = source[key]
                return

    def _append_unique(self, items: list[dict[str, Any]], entry: dict[str, Any], seen: set[str]) -> None:
        key = json.dumps(entry, sort_keys=True, ensure_ascii=False, default=str)
        if key in seen:
            return
        seen.add(key)
        items.append(entry)

    def _unique_strings(self, values: Any) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            if not isinstance(value, str) or not value or value in seen:
                continue
            seen.add(value)
            result.append(value)
        return result

    def _string_value(self, value: Any) -> str | None:
        return value if isinstance(value, str) and value else None

    def _usage_details(self, usage: Any) -> Optional[dict[str, int]]:
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

    def _cost_details(self, total_cost_usd: Optional[float]) -> Optional[dict[str, float]]:
        if total_cost_usd is None:
            return None
        return {"total_cost_usd": float(total_cost_usd)}

    def _get_langfuse_client(self) -> Any | None:
        if not self.settings.langfuse_enabled or self._langfuse_unavailable:
            return None
        if not self.settings.langfuse_public_key or not self.settings.langfuse_secret_key:
            return None
        if self._langfuse_client is not None:
            return self._langfuse_client
        try:
            from langfuse import Langfuse

            self._langfuse_client = Langfuse(
                public_key=self.settings.langfuse_public_key,
                secret_key=self.settings.langfuse_secret_key,
                base_url=self.settings.langfuse_base_url,
                environment=self.settings.langfuse_deployment_environment,
                flush_interval=max(self.settings.langfuse_export_interval_ms / 1000, 0.1),
            )
            return self._langfuse_client
        except Exception as exc:
            self._langfuse_unavailable = True
            print(f"[WARN] failed to initialize Langfuse runtime enrichment: {exc}", flush=True)
            return None

    def _start_langfuse_observation(self, **kwargs: Any) -> Any:
        client = self._get_langfuse_client()
        if client is None:
            return nullcontext(None)
        try:
            return client.start_as_current_observation(**kwargs)
        except Exception as exc:
            print(f"[WARN] failed to start Langfuse observation: {exc}", flush=True)
            return nullcontext(None)

    def _update_langfuse_observation(self, observation: Any, **kwargs: Any) -> None:
        if observation is None:
            return
        clean = {key: value for key, value in kwargs.items() if value is not None}
        try:
            observation.update(**clean)
        except Exception as exc:
            print(f"[WARN] failed to update Langfuse observation: {exc}", flush=True)

    def _set_langfuse_trace_io(self, observation: Any, *, input: Any, output: Any) -> None:
        if observation is None:
            return
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message="Trace-level input/output is deprecated.*",
                    category=DeprecationWarning,
                )
                observation.set_trace_io(input=input, output=output)
        except Exception as exc:
            print(f"[WARN] failed to set Langfuse trace input/output: {exc}", flush=True)

    def _flush_langfuse(self) -> None:
        client = self._langfuse_client or self._get_langfuse_client()
        if client is None:
            return
        try:
            client.flush()
        except Exception as exc:
            print(f"[WARN] failed to flush Langfuse runtime enrichment: {exc}", flush=True)

    def _build_options(self, req: ChatRequest, session: LocalSession) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions

        env = dict(os.environ)
        env.update(self._build_langfuse_env())
        env.update(self.settings.claude_env)
        if self.settings.provider_api_key:
            env["ANTHROPIC_API_KEY"] = self.settings.provider_api_key
        if self.settings.provider_api_url:
            env["ANTHROPIC_BASE_URL"] = self.settings.provider_api_url
        env["CLAUDE_AGENT_SDK_CLIENT_APP"] = "claude-agent-runtime-api/0.1.0"
        if self.settings.resolved_claude_config_dir:
            env["CLAUDE_CONFIG_DIR"] = str(self.settings.resolved_claude_config_dir)
            Path(env["CLAUDE_CONFIG_DIR"]).mkdir(parents=True, exist_ok=True)
        else:
            env.pop("CLAUDE_CONFIG_DIR", None)

        agents = None
        if self.settings.enable_programmatic_agents:
            try:
                agents = load_programmatic_agents(self.settings.workspace_dir, self.settings.claude_home)
            except Exception as exc:  # Do not prevent service use because of malformed agent file.
                print(f"[WARN] failed to load programmatic agents: {exc}", flush=True)

        system_append = "\n\n".join(
            part for part in [self.settings.claude_system_append, req.system_append] if part
        )
        system_prompt = {"type": "preset", "preset": "claude_code"}
        if system_append:
            system_prompt = {"type": "preset", "preset": "claude_code", "append": system_append}

        allowed_tools = req.allowed_tools if req.allowed_tools is not None else self.settings.default_allowed_tools
        disallowed_tools = (
            req.disallowed_tools
            if req.disallowed_tools is not None
            else self.settings.default_disallowed_tools
        )

        kwargs: dict[str, Any] = {
            "tools": self.settings.claude_tools,
            "cwd": self.settings.workspace_dir,
            "model": req.model or self.settings.agent_model,
            "fallback_model": self.settings.fallback_model,
            "allowed_tools": allowed_tools,
            "disallowed_tools": disallowed_tools,
            "permission_mode": req.permission_mode or self.settings.permission_mode,
            "max_turns": req.max_turns or self.settings.max_turns,
            "max_budget_usd": self.settings.max_budget_usd,
            "system_prompt": system_prompt,
            "env": env,
            "settings": str(self.settings.claude_settings_file) if self.settings.claude_settings_file else None,
            "mcp_servers": self.settings.claude_mcp_servers,
            "strict_mcp_config": self.settings.strict_mcp_config,
            "skills": self._skills_option(req),
            "include_hook_events": self.settings.include_hook_events,
            "include_partial_messages": self.settings.include_partial_messages,
            "hooks": build_default_hooks() if self.settings.enable_policy_hooks else None,
            "can_use_tool": guard_tool_use if self.settings.enable_policy_hooks else None,
            "agents": agents,
            "cli_path": self.settings.claude_cli_path,
            "add_dirs": self.settings.claude_add_dirs,
            "betas": self.settings.claude_betas,
            "permission_prompt_tool_name": self.settings.permission_prompt_tool_name,
            "max_buffer_size": self.settings.max_buffer_size,
            "user": self.settings.claude_user,
            "setting_sources": self.settings.setting_sources,
            "extra_args": self.settings.claude_extra_args,
            "max_thinking_tokens": self.settings.max_thinking_tokens,
            "effort": self.settings.effort,
            "enable_file_checkpointing": self.settings.enable_file_checkpointing,
            "session_store_flush": self.settings.session_store_flush,
            "load_timeout_ms": self.settings.load_timeout_ms,
        }

        # Resume the previous Claude Code session when possible. The API session id
        # is not necessarily equal to the internal Claude session id returned by SDK.
        if self.settings.enable_sdk_session_resume and session.sdk_session_id:
            kwargs["resume"] = session.sdk_session_id
        else:
            # If caller provides a UUID-looking session id, use it for the first Claude session.
            # Invalid IDs are simply ignored by the SDK if omitted.
            import uuid

            try:
                uuid.UUID(session.session_id)
                kwargs["session_id"] = session.session_id
            except ValueError:
                pass

        # Remove None values because older SDK versions may not accept them everywhere.
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        return ClaudeAgentOptions(**kwargs)

    def _build_langfuse_env(self) -> dict[str, str]:
        if not self.settings.langfuse_enabled:
            return {}
        if not self.settings.langfuse_public_key or not self.settings.langfuse_secret_key:
            raise ValueError("LANGFUSE_ENABLED=true requires LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY")

        signals = set(self.settings.langfuse_otel_signals)
        if not signals:
            raise ValueError("LANGFUSE_OTEL_SIGNALS must include at least one of: traces, metrics, logs")

        env = {
            "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
            "OTEL_EXPORTER_OTLP_ENDPOINT": self.settings.langfuse_effective_otel_endpoint,
            "OTEL_EXPORTER_OTLP_HEADERS": self.settings.langfuse_otel_headers,
            "OTEL_SERVICE_NAME": self.settings.langfuse_service_name,
            "OTEL_RESOURCE_ATTRIBUTES": self.settings.langfuse_resource_attributes,
            "OTEL_METRIC_EXPORT_INTERVAL": str(self.settings.langfuse_export_interval_ms),
            "OTEL_LOGS_EXPORT_INTERVAL": str(self.settings.langfuse_export_interval_ms),
            "OTEL_TRACES_EXPORT_INTERVAL": str(self.settings.langfuse_export_interval_ms),
            "OTEL_LOG_USER_PROMPTS": "1",
            "OTEL_LOG_TOOL_DETAILS": "1",
            "OTEL_LOG_TOOL_CONTENT": "1",
            "OTEL_LOG_RAW_API_BODIES": "1",
        }
        if "traces" in signals:
            env["CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"] = "1"
            env["OTEL_TRACES_EXPORTER"] = "otlp"
        if "metrics" in signals:
            env["OTEL_METRICS_EXPORTER"] = "otlp"
        if "logs" in signals:
            env["OTEL_LOGS_EXPORTER"] = "otlp"
        return env

    async def run(self, req: ChatRequest) -> dict[str, Any]:
        from claude_agent_sdk import ResultMessage, query

        session = self.session_store.get_or_create(req.session_id, metadata=req.metadata)
        prompt = self._build_prompt(req)
        telemetry_input = self._request_telemetry_input(req, prompt, session)
        messages: list[dict[str, Any]] = []
        answer_parts: list[str] = []
        usage: Optional[dict[str, Any]] = None
        total_cost_usd: Optional[float] = None
        stop_reason: Optional[str] = None
        errors: list[str] = []
        partial_text_seen = False
        sdk_session_id: Optional[str] = session.sdk_session_id

        with self._start_langfuse_observation(
            as_type="span",
            name="runtime.chat",
            input=telemetry_input,
            metadata={"api_session_id": session.session_id, "mode": "non_stream"},
        ) as root_span:
            with self._start_langfuse_observation(
                as_type="generation",
                name="runtime.claude_sdk_query",
                input={"prompt": prompt, "model": req.model or self.settings.agent_model},
                model=req.model or self.settings.agent_model,
            ) as generation:
                try:
                    options = self._build_options(req, session)
                    async for msg in query(prompt=self._single_prompt_stream(prompt), options=options):
                        plain = to_plain(msg)
                        plain["event"] = message_event_name(msg)
                        messages.append(plain)
                        stream_delta = extract_stream_event_text(msg)
                        if stream_delta:
                            text = stream_delta
                            partial_text_seen = True
                        elif (
                            self.settings.include_partial_messages
                            and partial_text_seen
                            and msg.__class__.__name__ == "AssistantMessage"
                        ):
                            text = ""
                        else:
                            text = extract_text(msg)
                        if text:
                            answer_parts.append(text)

                        candidate_session_id = getattr(msg, "session_id", None)
                        if candidate_session_id:
                            sdk_session_id = candidate_session_id

                        if isinstance(msg, ResultMessage):
                            usage = getattr(msg, "usage", None) or getattr(msg, "model_usage", None)
                            total_cost_usd = getattr(msg, "total_cost_usd", None)
                            stop_reason = getattr(msg, "stop_reason", None)
                            errors.extend(self._result_errors(msg))
                except Exception as exc:
                    if not self._should_suppress_exception(exc, errors):
                        errors.append(f"{exc.__class__.__name__}: {exc}")

                answer = self._dedupe_answer_parts(answer_parts)
                agent_activity = self._agent_activity_payload(req, messages)
                output = self._runtime_output_payload(
                    session=session,
                    sdk_session_id=sdk_session_id,
                    answer=answer,
                    messages=messages,
                    agent_activity=agent_activity,
                    usage=usage,
                    total_cost_usd=total_cost_usd,
                    stop_reason=stop_reason,
                    errors=errors,
                )
                self._update_langfuse_observation(
                    generation,
                    output=output,
                    usage_details=self._usage_details(usage),
                    cost_details=self._cost_details(total_cost_usd),
                    level="ERROR" if errors else "DEFAULT",
                    status_message="\n".join(errors) if errors else None,
                )
            self._update_langfuse_observation(
                root_span,
                input=telemetry_input,
                output=output,
                level="ERROR" if errors else "DEFAULT",
                status_message="\n".join(errors) if errors else None,
            )
            self._set_langfuse_trace_io(
                root_span,
                input=telemetry_input,
                output=output,
            )
        self._flush_langfuse()

        if sdk_session_id:
            session.sdk_session_id = sdk_session_id
        session.turns += 1
        if not session.title:
            session.title = req.message[:80]
        self.session_store.save(session)
        return {
            "session_id": session.session_id,
            "sdk_session_id": session.sdk_session_id,
            "answer": answer,
            "messages": messages,
            "agent_activity": agent_activity,
            "usage": usage,
            "total_cost_usd": total_cost_usd,
            "stop_reason": stop_reason,
            "errors": errors,
        }

    async def stream(self, req: ChatRequest) -> AsyncIterator[dict[str, Any]]:
        from claude_agent_sdk import ResultMessage, query

        session = self.session_store.get_or_create(req.session_id, metadata=req.metadata)
        prompt = self._build_prompt(req)
        telemetry_input = self._request_telemetry_input(req, prompt, session)
        sdk_session_id: Optional[str] = session.sdk_session_id
        messages: list[dict[str, Any]] = []
        answer_parts: list[str] = []
        usage: Any = None
        total_cost_usd: Optional[float] = None
        stop_reason: Optional[str] = None
        errors: list[str] = []
        partial_text_seen = False

        with self._start_langfuse_observation(
            as_type="span",
            name="runtime.chat",
            input=telemetry_input,
            metadata={"api_session_id": session.session_id, "mode": "stream"},
        ) as root_span:
            yield {"event": "session", "data": {"session_id": session.session_id, "sdk_session_id": session.sdk_session_id}}
            with self._start_langfuse_observation(
                as_type="generation",
                name="runtime.claude_sdk_query",
                input={"prompt": prompt, "model": req.model or self.settings.agent_model},
                model=req.model or self.settings.agent_model,
            ) as generation:
                try:
                    options = self._build_options(req, session)
                    async for msg in query(prompt=self._single_prompt_stream(prompt), options=options):
                        stream_delta = extract_stream_event_text(msg)
                        if stream_delta:
                            text = stream_delta
                            partial_text_seen = True
                        elif (
                            self.settings.include_partial_messages
                            and partial_text_seen
                            and msg.__class__.__name__ == "AssistantMessage"
                        ):
                            text = ""
                        else:
                            text = extract_text(msg)
                        plain = to_plain(msg)
                        event = message_event_name(msg)
                        plain["event"] = event
                        messages.append(plain)
                        if text:
                            answer_parts.append(text)
                        yield {"event": "message", "data": {"event": event, "text": text, "raw": plain}}

                        candidate_session_id = getattr(msg, "session_id", None)
                        if candidate_session_id:
                            sdk_session_id = candidate_session_id

                        if isinstance(msg, ResultMessage):
                            usage = getattr(msg, "usage", None) or getattr(msg, "model_usage", None)
                            total_cost_usd = getattr(msg, "total_cost_usd", None)
                            stop_reason = getattr(msg, "stop_reason", None)
                            result_errors = self._result_errors(msg)
                            errors.extend(result_errors)
                            agent_activity = self._agent_activity_payload(req, messages)
                            yield {
                                "event": "result",
                                "data": {
                                    "session_id": session.session_id,
                                    "sdk_session_id": sdk_session_id,
                                    "agent_activity": agent_activity,
                                    "usage": usage,
                                    "total_cost_usd": total_cost_usd,
                                    "stop_reason": stop_reason,
                                    "errors": result_errors,
                                },
                            }
                except Exception as exc:
                    if not self._should_suppress_exception(exc, errors):
                        errors.append(f"{exc.__class__.__name__}: {exc}")
                        yield {"event": "error", "data": {"errors": errors}}
                finally:
                    if sdk_session_id:
                        session.sdk_session_id = sdk_session_id
                    session.turns += 1
                    if not session.title:
                        session.title = req.message[:80]
                    self.session_store.save(session)

                    answer = self._dedupe_answer_parts(answer_parts)
                    agent_activity = self._agent_activity_payload(req, messages)
                    output = self._runtime_output_payload(
                        session=session,
                        sdk_session_id=sdk_session_id,
                        answer=answer,
                        messages=messages,
                        agent_activity=agent_activity,
                        usage=usage,
                        total_cost_usd=total_cost_usd,
                        stop_reason=stop_reason,
                        errors=errors,
                    )
                    self._update_langfuse_observation(
                        generation,
                        output=output,
                        usage_details=self._usage_details(usage),
                        cost_details=self._cost_details(total_cost_usd),
                        level="ERROR" if errors else "DEFAULT",
                        status_message="\n".join(errors) if errors else None,
                    )
                    self._update_langfuse_observation(
                        root_span,
                        input=telemetry_input,
                        output=output,
                        level="ERROR" if errors else "DEFAULT",
                        status_message="\n".join(errors) if errors else None,
                    )
                    self._set_langfuse_trace_io(
                        root_span,
                        input=telemetry_input,
                        output=output,
                    )
                    yield {"event": "done", "data": "[DONE]"}
        self._flush_langfuse()

    async def stream_ag_ui(self, req: RunAgentInput) -> AsyncIterator[dict[str, Any]]:
        chat_req = run_input_to_chat_request(req)
        message_id = f"{req.run_id}-assistant-1"
        text_started = False
        run_failed = False
        final_result: dict[str, Any] | None = None
        activity_seen: set[str] = set()
        tool_activity_context: dict[str, dict[str, str]] = {}

        yield run_started_event(req)

        async for item in self.stream(chat_req):
            event_name = item.get("event")
            data = item.get("data")

            if event_name == "message" and isinstance(data, dict):
                runtime_event = data.get("event")
                if isinstance(runtime_event, str) and runtime_event.startswith("ResultMessage"):
                    continue
                for activity in self._agent_activity_events_from_raw(data.get("raw"), tool_activity_context):
                    activity_key = json.dumps(activity, sort_keys=True, ensure_ascii=False, default=str)
                    if activity_key in activity_seen:
                        continue
                    activity_seen.add(activity_key)
                    yield custom_event(AGENT_ACTIVITY_EVENT_NAME, activity)
                tool_extraction = extract_a2ui_tool_payloads(data.get("raw"))
                for payload in tool_extraction.payloads:
                    yield custom_event(A2UI_CUSTOM_EVENT_NAME, payload)
                for error in tool_extraction.errors:
                    print(f"[WARN] skipped invalid A2UI tool payload: {error}", flush=True)
                text = data.get("text")
                if isinstance(text, str) and text:
                    if _is_internal_skill_payload(text):
                        continue
                    if not text_started:
                        yield text_message_start_event(message_id)
                        text_started = True
                    yield text_message_content_event(message_id, text)
                continue

            if event_name == "result" and isinstance(data, dict):
                errors = data.get("errors")
                final_result = {
                    "sessionId": data.get("session_id"),
                    "sdkSessionId": data.get("sdk_session_id"),
                    "agentActivity": data.get("agent_activity"),
                    "usage": data.get("usage"),
                    "totalCostUsd": data.get("total_cost_usd"),
                    "stopReason": data.get("stop_reason"),
                }
                if isinstance(errors, list) and errors:
                    run_failed = True
                    yield run_error_event(req, "\n".join(str(error) for error in errors))
                continue

            if event_name == "error" and isinstance(data, dict):
                errors = data.get("errors")
                message = "\n".join(str(error) for error in errors) if isinstance(errors, list) else "Runtime error"
                run_failed = True
                yield run_error_event(req, message)

        if text_started:
            yield text_message_end_event(message_id)

        if not run_failed:
            yield run_finished_event(req, final_result)
