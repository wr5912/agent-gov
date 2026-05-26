from __future__ import annotations

import asyncio
import json
import os
import uuid
import warnings
from contextlib import nullcontext
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Optional

from .agent_profiles import AgentRuntimeProfile, build_profiles
from .agent_profile_versions import profile_version_snapshot
from .agent_loader import load_programmatic_agents
from .agent_version_store import AgentVersionStore
from .feedback_jobs import EXPECTED_SCHEMA_FIELDS, attribution_prompt, extract_json_candidates, proposal_prompt
from .feedback_store import FeedbackStore, utc_now
from .message_utils import extract_text, message_event_name, to_plain
from .output_formatter import DSPyOutputFormatter
from .policy import build_default_hooks, guard_tool_use
from .schemas import ChatRequest
from .session_store import LocalSession, LocalSessionStore
from .settings import AppSettings


def ensure_langfuse_otel_compat() -> None:
    """Backfill the OpenTelemetry env constant expected by Langfuse 4.x."""
    try:
        import opentelemetry.sdk.environment_variables as otel_env
    except Exception:
        return
    if not hasattr(otel_env, "OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED"):
        # google-adk 2.0.0 pins OpenTelemetry <=1.41.1, while Langfuse 4.6.1
        # imports this newer constant. The constant value is only the env name.
        otel_env.OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED = "OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED"


class ClaudeRuntime:
    """Thin runtime adapter around Claude Agent SDK.

    Design goals:
    - Keep Claude native config on disk: CLAUDE.md, .claude/settings.json,
      .claude/agents/*.md, .claude/skills/*/SKILL.md, .mcp.json.
    - Expose a stable HTTP API around it.
    - Persist a lightweight mapping from API session ids to Claude SDK session ids.
    """

    def __init__(
        self,
        settings: AppSettings,
        session_store: LocalSessionStore,
        feedback_store: FeedbackStore | None = None,
        agent_version_store: AgentVersionStore | None = None,
    ) -> None:
        self.settings = settings
        self.session_store = session_store
        self.feedback_store = feedback_store
        self.agent_version_store = agent_version_store
        self.profiles = build_profiles(settings)
        self.output_formatter = DSPyOutputFormatter(settings)
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

    def _request_telemetry_input(
        self,
        req: ChatRequest,
        prompt: str,
        session: LocalSession,
        run_id: str,
        agent_version_id: Optional[str],
    ) -> dict[str, Any]:
        allowed_tools = req.allowed_tools if req.allowed_tools is not None else self.settings.default_allowed_tools
        disallowed_tools = (
            req.disallowed_tools
            if req.disallowed_tools is not None
            else self.settings.default_disallowed_tools
        )
        return {
            "run_id": run_id,
            "agent_version_id": agent_version_id,
            "message": req.message,
            "prompt": prompt,
            "api_session_id": session.session_id,
            "sdk_session_id": session.sdk_session_id,
            "alert_id": req.alert_id,
            "case_id": req.case_id,
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
        run_id: str,
        agent_version_id: Optional[str],
        session: LocalSession,
        sdk_session_id: Optional[str],
        alert_id: Optional[str],
        case_id: Optional[str],
        answer: str,
        messages: list[dict[str, Any]],
        agent_activity: dict[str, Any],
        usage: Any,
        total_cost_usd: Optional[float],
        stop_reason: Optional[str],
        errors: list[str],
    ) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "agent_version_id": agent_version_id,
            "api_session_id": session.session_id,
            "sdk_session_id": sdk_session_id,
            "alert_id": alert_id,
            "case_id": case_id,
            "answer": answer,
            "messages": messages,
            "agent_activity": agent_activity,
            "usage": to_plain(usage),
            "total_cost_usd": total_cost_usd,
            "stop_reason": stop_reason,
            "errors": errors,
        }

    def _record_feedback_run(
        self,
        *,
        run_id: str,
        agent_version_id: Optional[str],
        session: LocalSession,
        sdk_session_id: Optional[str],
        req: ChatRequest,
        answer: str,
        messages: list[dict[str, Any]],
        agent_activity: dict[str, Any],
        usage: Any,
        total_cost_usd: Optional[float],
        stop_reason: Optional[str],
        errors: list[str],
        created_at: str,
        completed_at: str,
        langfuse_trace_id: Optional[str] = None,
        langfuse_trace_url: Optional[str] = None,
    ) -> None:
        if self.feedback_store is None:
            return
        answer_summary = answer.strip().replace("\n", " ")[:500]
        self.feedback_store.record_run(
            {
                "run_id": run_id,
                "agent_version_id": agent_version_id,
                "session_id": session.session_id,
                "sdk_session_id": sdk_session_id,
                "alert_id": req.alert_id,
                "case_id": req.case_id,
                "message": req.message,
                "answer_summary": answer_summary,
                "messages": messages,
                "agent_activity": agent_activity,
                "langfuse_trace_id": langfuse_trace_id,
                "langfuse_trace_url": langfuse_trace_url,
                "usage": to_plain(usage),
                "total_cost_usd": total_cost_usd,
                "stop_reason": stop_reason,
                "errors": errors,
                "metadata": req.metadata,
                "created_at": created_at,
                "completed_at": completed_at,
            }
        )

    def _current_agent_version_id(self) -> Optional[str]:
        if self.agent_version_store is None:
            return None
        return self.agent_version_store.current_version_id()

    def _raise_if_version_maintenance(self) -> None:
        if self.agent_version_store is not None and self.agent_version_store.is_maintenance_active():
            raise RuntimeError("Agent version maintenance is in progress; retry after restore completes.")

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
            ensure_langfuse_otel_compat()
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

    def _current_langfuse_trace_ref(self) -> tuple[Optional[str], Optional[str]]:
        client = self._langfuse_client
        if client is None:
            return None, None
        try:
            trace_id = client.get_current_trace_id()
            trace_url = client.get_trace_url(trace_id=trace_id) if trace_id else None
            return trace_id, trace_url
        except Exception as exc:
            print(f"[WARN] failed to read current Langfuse trace: {exc}", flush=True)
            return None, None

    def fetch_langfuse_trace(self, trace_id: str) -> Optional[dict[str, Any]]:
        if not trace_id or not self.settings.langfuse_enabled:
            return None
        if not self.settings.langfuse_public_key or not self.settings.langfuse_secret_key:
            return None
        try:
            ensure_langfuse_otel_compat()
            from langfuse.api.client import LangfuseAPI

            client = LangfuseAPI(
                base_url=self.settings.langfuse_base_url,
                username=self.settings.langfuse_public_key,
                password=self.settings.langfuse_secret_key,
                x_langfuse_public_key=self.settings.langfuse_public_key,
                timeout=10,
            )
            trace = client.trace.get(trace_id, fields="core,io,scores,observations,metrics")
            return to_plain(trace)
        except Exception as exc:
            return {"fetch_status": "failed", "error": f"{exc.__class__.__name__}: {exc}"}

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

    def _profile_env(self, profile: AgentRuntimeProfile) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self._build_langfuse_env())
        env.update(self.settings.claude_env)
        env["HOME"] = str(profile.claude_root)
        env["CLAUDE_CONFIG_DIR"] = str(profile.claude_config_dir)
        env["AGENT_PROFILE"] = profile.name
        env["CLAUDE_AGENT_SDK_CLIENT_APP"] = f"secops-runtime/{profile.name}"
        profile.claude_root.mkdir(parents=True, exist_ok=True)
        profile.claude_config_dir.mkdir(parents=True, exist_ok=True)
        return env

    def _build_options(self, req: ChatRequest, session: LocalSession) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions

        profile = self.profiles["main"]
        env = self._profile_env(profile)
        if self.settings.provider_api_key:
            env["ANTHROPIC_API_KEY"] = self.settings.provider_api_key
        if self.settings.provider_api_url:
            env["ANTHROPIC_BASE_URL"] = self.settings.provider_api_url

        agents = None
        if self.settings.enable_programmatic_agents:
            try:
                agents = load_programmatic_agents(profile.workspace_dir, profile.claude_config_dir)
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
            "cwd": profile.workspace_dir,
            "model": req.model or self.settings.agent_model,
            "fallback_model": self.settings.fallback_model,
            "allowed_tools": allowed_tools,
            "disallowed_tools": disallowed_tools,
            "permission_mode": req.permission_mode or self.settings.permission_mode,
            "max_turns": req.max_turns or self.settings.max_turns,
            "max_budget_usd": self.settings.max_budget_usd,
            "system_prompt": system_prompt,
            "env": env,
            "settings": str(profile.project_settings_path) if profile.project_settings_path.exists() else None,
            "mcp_servers": str(profile.mcp_config_path) if profile.mcp_config_path.exists() else None,
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

    def _build_job_options(self, profile: AgentRuntimeProfile) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions

        env = self._profile_env(profile)
        if self.settings.provider_api_key:
            env["ANTHROPIC_API_KEY"] = self.settings.provider_api_key
        if self.settings.provider_api_url:
            env["ANTHROPIC_BASE_URL"] = self.settings.provider_api_url

        kwargs: dict[str, Any] = {
            "cwd": profile.workspace_dir,
            "model": self.settings.agent_model,
            "fallback_model": self.settings.fallback_model,
            "allowed_tools": list(profile.allowed_tools),
            "disallowed_tools": list(profile.disallowed_tools),
            "permission_mode": profile.permission_mode,
            "max_turns": max(self.settings.max_turns, profile.max_turns or 0),
            "max_budget_usd": self.settings.max_budget_usd,
            "env": env,
            "settings": str(profile.project_settings_path) if profile.project_settings_path.exists() else None,
            "mcp_servers": str(profile.mcp_config_path) if profile.mcp_config_path.exists() else None,
            "strict_mcp_config": True,
            "include_hook_events": self.settings.include_hook_events,
            "include_partial_messages": False,
            "hooks": build_default_hooks() if self.settings.enable_policy_hooks else None,
            "can_use_tool": guard_tool_use if self.settings.enable_policy_hooks else None,
            "cli_path": self.settings.claude_cli_path,
            "add_dirs": self.settings.claude_add_dirs,
            "betas": self.settings.claude_betas,
            "permission_prompt_tool_name": self.settings.permission_prompt_tool_name,
            "max_buffer_size": self.settings.max_buffer_size,
            "user": self.settings.claude_user,
            "setting_sources": ["user", "project"],
            "extra_args": self.settings.claude_extra_args,
            "max_thinking_tokens": self.settings.max_thinking_tokens,
            "effort": self.settings.effort,
            "enable_file_checkpointing": False,
            "session_store_flush": self.settings.session_store_flush,
            "load_timeout_ms": self.settings.load_timeout_ms,
        }
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        return ClaudeAgentOptions(**kwargs)

    def _provider_configured(self) -> bool:
        return bool(self.settings.provider_api_key)

    async def _run_profile_json(
        self,
        *,
        profile_name: str,
        prompt: str,
        expected_schema_version: str,
        job_type: str,
        job_input: dict[str, Any],
    ) -> dict[str, Any]:
        from claude_agent_sdk import ResultMessage, query

        profile = self.profiles[profile_name]
        answer_parts: list[str] = []
        errors: list[str] = []
        options = self._build_job_options(profile)
        async def collect() -> dict[str, Any]:
            async for msg in query(prompt=self._single_prompt_stream(prompt), options=options):
                text = extract_text(msg)
                if text:
                    answer_parts.append(text)
                    output_bytes = len("".join(answer_parts).encode("utf-8"))
                    if output_bytes > profile.max_output_bytes:
                        raise RuntimeError(f"Agent output exceeded {profile.max_output_bytes} bytes")
                if isinstance(msg, ResultMessage):
                    errors.extend(self._result_errors(msg))
            answer = self._dedupe_answer_parts(answer_parts)
            if errors and not answer:
                raise RuntimeError("; ".join(errors))
            direct = self._direct_schema_candidate(answer, expected_schema_version)
            if direct:
                return direct
            formatted = await self._format_agent_text(
                job_type=job_type,
                raw_text=answer,
                job_input=job_input,
                expected_schema_version=expected_schema_version,
            )
            if formatted:
                return formatted
            return self._raw_agent_text_payload(answer, expected_schema_version)

        return await asyncio.wait_for(collect(), timeout=profile.max_runtime_seconds)

    def _direct_schema_candidate(self, raw_text: str, expected_schema_version: str) -> dict[str, Any] | None:
        candidates = extract_json_candidates(raw_text)
        for candidate in reversed(candidates):
            if candidate.get("schema_version") == expected_schema_version:
                return candidate
        expected_fields = EXPECTED_SCHEMA_FIELDS.get(expected_schema_version)
        if not expected_fields:
            return None
        scored = sorted(candidates, key=lambda item: len(set(item) & expected_fields), reverse=True)
        if scored and len(set(scored[0]) & expected_fields) >= max(3, len(expected_fields) // 2):
            return scored[0]
        return None

    async def _format_agent_text(
        self,
        *,
        job_type: str,
        raw_text: str,
        job_input: dict[str, Any],
        expected_schema_version: str,
    ) -> dict[str, Any] | None:
        if job_type not in {"attribution", "proposal"}:
            return None
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    self.output_formatter.format,
                    job_type=job_type,
                    raw_text=raw_text,
                    job_input=job_input,
                    expected_schema_version=expected_schema_version,
                ),
                timeout=self.settings.dspy_output_formatter_timeout_seconds,
            )
        except Exception as exc:
            print(f"[WARN] failed to format Agent output: {exc}", flush=True)
            return None
        return result.payload if result else None

    def _raw_agent_text_payload(self, raw_text: str, expected_schema_version: str) -> dict[str, Any]:
        return {
            "_raw_agent_text": raw_text,
            "_candidate_json_objects": extract_json_candidates(raw_text),
            "_expected_schema_version": expected_schema_version,
        }

    async def run_attribution_job(self, feedback_case_id: str, *, force: bool = False) -> dict[str, Any] | None:
        if self.feedback_store is None:
            return None
        profile = self.profiles["feedback-attribution"]
        job = self.feedback_store.create_attribution_job(
            feedback_case_id,
            profile_version=profile_version_snapshot(profile, version_id="feedback-attribution-v0.1.0"),
            force=force,
        )
        if not job:
            return None
        if job.get("_reused_existing"):
            return job
        if job.get("status") != "queued":
            return job
        self.feedback_store.start_job(job["job_id"])
        if not self._provider_configured():
            self.feedback_store.complete_attribution_job(job["job_id"], self.feedback_store.offline_attribution_output(job))
            return self.feedback_store.get_job(job["job_id"])
        try:
            raw = await self._run_profile_json(
                profile_name="feedback-attribution",
                prompt=attribution_prompt(job["input_path"]),
                expected_schema_version="attribution-output/v1",
                job_type="attribution",
                job_input=job.get("input_json") if isinstance(job.get("input_json"), dict) else {},
            )
            self.feedback_store.complete_attribution_job(job["job_id"], raw)
        except asyncio.TimeoutError as exc:
            self.feedback_store.fail_job(job["job_id"], error_code="AGENT_TIMEOUT", message=f"{exc.__class__.__name__}: {exc}")
        except Exception as exc:
            self.feedback_store.fail_job(job["job_id"], error_code="AGENT_RUNTIME_ERROR", message=f"{exc.__class__.__name__}: {exc}")
        return self.feedback_store.get_job(job["job_id"])

    async def run_proposal_job(self, feedback_case_id: str, *, force: bool = False) -> dict[str, Any] | None:
        if self.feedback_store is None:
            return None
        profile = self.profiles["feedback-proposal"]
        job = self.feedback_store.create_proposal_job(
            feedback_case_id,
            profile_version=profile_version_snapshot(profile, version_id="feedback-proposal-v0.1.0"),
            force=force,
        )
        if not job:
            return None
        if job.get("_reused_existing"):
            return job
        if job.get("status") != "queued":
            return job
        self.feedback_store.start_job(job["job_id"])
        if not self._provider_configured():
            self.feedback_store.complete_proposal_job(job["job_id"], self.feedback_store.offline_proposal_output(job))
            return self.feedback_store.get_job(job["job_id"])
        try:
            attribution_job_id = job.get("attribution_job_id")
            attribution_output = self.feedback_store.get_job_output(str(attribution_job_id), "attribution") if attribution_job_id else None
            raw = await self._run_profile_json(
                profile_name="feedback-proposal",
                prompt=proposal_prompt(
                    job["input_path"],
                    input_payload=job.get("input_json"),
                    attribution_output=attribution_output,
                ),
                expected_schema_version="proposal-output/v1",
                job_type="proposal",
                job_input=job.get("input_json") if isinstance(job.get("input_json"), dict) else {},
            )
            self.feedback_store.complete_proposal_job(job["job_id"], raw)
        except asyncio.TimeoutError as exc:
            self.feedback_store.fail_job(job["job_id"], error_code="AGENT_TIMEOUT", message=f"{exc.__class__.__name__}: {exc}")
        except Exception as exc:
            self.feedback_store.fail_job(job["job_id"], error_code="AGENT_RUNTIME_ERROR", message=f"{exc.__class__.__name__}: {exc}")
        return self.feedback_store.get_job(job["job_id"])

    async def run_feedback_eval(
        self,
        *,
        eval_case_ids: Optional[list[str]] = None,
        optimization_task_id: Optional[str] = None,
        source: str = "manual_feedback_dataset",
    ) -> dict[str, Any] | None:
        if self.feedback_store is None:
            return None
        eval_cases = self._selected_eval_cases(eval_case_ids)
        if not eval_cases:
            return None
        eval_run = self.feedback_store.create_eval_run(
            eval_case_ids=[str(item["eval_case_id"]) for item in eval_cases],
            agent_version_id=self._current_agent_version_id(),
            optimization_task_id=optimization_task_id,
            source=source,
        )
        if optimization_task_id:
            self.feedback_store.update_task_status(
                optimization_task_id,
                status="regression_running",
                fields={"latest_regression_run_id": eval_run["eval_run_id"]},
            )
        try:
            for eval_case in eval_cases:
                result: dict[str, Any] | None = None
                try:
                    result = await self.run(
                        ChatRequest(
                            message=str(eval_case.get("prompt") or ""),
                            session_id=f"eval-{eval_run['eval_run_id']}-{eval_case['eval_case_id']}",
                            case_id=str(eval_case.get("source_feedback_case_id") or "") or None,
                            metadata={
                                "source": "regression_eval",
                                "eval_run_id": eval_run["eval_run_id"],
                                "eval_case_id": eval_case["eval_case_id"],
                                "optimization_task_id": optimization_task_id,
                            },
                        )
                    )
                    status, score, check_results = self._evaluate_eval_case(eval_case, result)
                    self.feedback_store.append_eval_run_item(
                        eval_run["eval_run_id"],
                        eval_case=eval_case,
                        agent_result=result,
                        status=status,
                        score=score,
                        check_results=check_results,
                    )
                except Exception as exc:
                    self.feedback_store.append_eval_run_item(
                        eval_run["eval_run_id"],
                        eval_case=eval_case,
                        agent_result=result,
                        status="failed",
                        score=0.0,
                        check_results=[],
                        error_json={"error_code": "EVAL_CASE_RUNTIME_ERROR", "message": f"{exc.__class__.__name__}: {exc}"},
                    )
            return self.feedback_store.finish_eval_run(eval_run["eval_run_id"])
        except Exception as exc:
            return self.feedback_store.fail_eval_run(
                eval_run["eval_run_id"],
                error_code="EVAL_RUN_RUNTIME_ERROR",
                message=f"{exc.__class__.__name__}: {exc}",
            )

    def _selected_eval_cases(self, eval_case_ids: Optional[list[str]]) -> list[dict[str, Any]]:
        if self.feedback_store is None:
            return []
        if eval_case_ids:
            selected = [self.feedback_store.find_eval_case(eval_case_id) for eval_case_id in eval_case_ids]
            return [item for item in selected if item and item.get("status") == "active"]
        return self.feedback_store.list_eval_cases(status="active", limit=100)

    def _evaluate_eval_case(self, eval_case: dict[str, Any], result: dict[str, Any]) -> tuple[str, float, list[dict[str, Any]]]:
        checks = eval_case.get("checks_json") if isinstance(eval_case.get("checks_json"), dict) else {}
        errors = result.get("errors") if isinstance(result.get("errors"), list) else []
        answer = str(result.get("answer") or "").strip()
        activity = result.get("agent_activity") if isinstance(result.get("agent_activity"), dict) else {}
        tool_names = self._eval_tool_names(activity)
        check_results: list[dict[str, Any]] = []

        def append_check(name: str, passed: bool, required: bool, detail: str) -> None:
            check_results.append({"name": name, "passed": passed, "required": required, "detail": detail})

        if checks.get("requires_non_empty_answer", True):
            append_check("non_empty_answer", bool(answer), True, "回答不应为空。")
        if checks.get("requires_no_runtime_errors", True):
            append_check("no_runtime_errors", not errors, True, "; ".join(map(str, errors)) if errors else "运行无错误。")
        if checks.get("requires_tool_use"):
            preferred = [str(item) for item in checks.get("preferred_tools") or [] if item]
            if preferred:
                tool_passed = any(any(tool == expected or expected in tool for tool in tool_names) for expected in preferred)
                detail = f"期望工具：{', '.join(preferred)}；实际工具：{', '.join(tool_names) or '-'}。"
            else:
                tool_passed = bool(tool_names)
                detail = f"实际工具：{', '.join(tool_names) or '-'}。"
            append_check("required_tool_use", tool_passed, True, detail)

        required_checks = [item for item in check_results if item["required"]]
        passed_required = sum(1 for item in required_checks if item["passed"])
        score = passed_required / len(required_checks) if required_checks else 0.0
        if any(not item["passed"] for item in required_checks):
            return "failed", score, check_results
        return "passed", score, check_results

    def _eval_tool_names(self, activity: dict[str, Any]) -> list[str]:
        names: list[str] = []
        for item in activity.get("tool_names") or []:
            if item:
                names.append(str(item))
        for call in activity.get("tool_calls") or []:
            if isinstance(call, dict) and call.get("name"):
                names.append(str(call["name"]))
        return sorted(set(names))

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

        self._raise_if_version_maintenance()
        session = self.session_store.get_or_create(req.session_id, metadata=req.metadata)
        run_id = str(uuid.uuid4())
        agent_version_id = self._current_agent_version_id()
        created_at = utc_now()
        prompt = self._build_prompt(req)
        telemetry_input = self._request_telemetry_input(req, prompt, session, run_id, agent_version_id)
        messages: list[dict[str, Any]] = []
        answer_parts: list[str] = []
        usage: Optional[dict[str, Any]] = None
        total_cost_usd: Optional[float] = None
        stop_reason: Optional[str] = None
        errors: list[str] = []
        sdk_session_id: Optional[str] = session.sdk_session_id
        langfuse_trace_id: Optional[str] = None
        langfuse_trace_url: Optional[str] = None

        with self._start_langfuse_observation(
            as_type="span",
            name="runtime.chat",
            input=telemetry_input,
            metadata={"api_session_id": session.session_id, "run_id": run_id, "agent_version_id": agent_version_id, "mode": "non_stream"},
        ) as root_span:
            langfuse_trace_id, langfuse_trace_url = self._current_langfuse_trace_ref()
            with self._start_langfuse_observation(
                as_type="generation",
                name="runtime.claude_sdk_query",
                input={"run_id": run_id, "agent_version_id": agent_version_id, "prompt": prompt, "model": req.model or self.settings.agent_model},
                model=req.model or self.settings.agent_model,
            ) as generation:
                try:
                    options = self._build_options(req, session)
                    async for msg in query(prompt=self._single_prompt_stream(prompt), options=options):
                        plain = to_plain(msg)
                        plain["event"] = message_event_name(msg)
                        messages.append(plain)
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
                    run_id=run_id,
                    agent_version_id=agent_version_id,
                    session=session,
                    sdk_session_id=sdk_session_id,
                    alert_id=req.alert_id,
                    case_id=req.case_id,
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
        completed_at = utc_now()
        self._record_feedback_run(
            run_id=run_id,
            agent_version_id=agent_version_id,
            session=session,
            sdk_session_id=sdk_session_id,
            req=req,
            answer=answer,
            messages=messages,
            agent_activity=agent_activity,
            usage=usage,
            total_cost_usd=total_cost_usd,
            stop_reason=stop_reason,
            errors=errors,
            created_at=created_at,
            completed_at=completed_at,
            langfuse_trace_id=langfuse_trace_id,
            langfuse_trace_url=langfuse_trace_url,
        )
        return {
            "run_id": run_id,
            "agent_version_id": agent_version_id,
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

        self._raise_if_version_maintenance()
        session = self.session_store.get_or_create(req.session_id, metadata=req.metadata)
        run_id = str(uuid.uuid4())
        agent_version_id = self._current_agent_version_id()
        created_at = utc_now()
        prompt = self._build_prompt(req)
        telemetry_input = self._request_telemetry_input(req, prompt, session, run_id, agent_version_id)
        sdk_session_id: Optional[str] = session.sdk_session_id
        messages: list[dict[str, Any]] = []
        answer_parts: list[str] = []
        usage: Any = None
        total_cost_usd: Optional[float] = None
        stop_reason: Optional[str] = None
        errors: list[str] = []
        langfuse_trace_id: Optional[str] = None
        langfuse_trace_url: Optional[str] = None

        with self._start_langfuse_observation(
            as_type="span",
            name="runtime.chat",
            input=telemetry_input,
            metadata={"api_session_id": session.session_id, "run_id": run_id, "agent_version_id": agent_version_id, "mode": "stream"},
        ) as root_span:
            langfuse_trace_id, langfuse_trace_url = self._current_langfuse_trace_ref()
            yield {
                "event": "session",
                "data": {
                    "run_id": run_id,
                    "agent_version_id": agent_version_id,
                    "session_id": session.session_id,
                    "sdk_session_id": session.sdk_session_id,
                    "alert_id": req.alert_id,
                    "case_id": req.case_id,
                },
            }
            with self._start_langfuse_observation(
                as_type="generation",
                name="runtime.claude_sdk_query",
                input={"run_id": run_id, "agent_version_id": agent_version_id, "prompt": prompt, "model": req.model or self.settings.agent_model},
                model=req.model or self.settings.agent_model,
            ) as generation:
                try:
                    options = self._build_options(req, session)
                    async for msg in query(prompt=self._single_prompt_stream(prompt), options=options):
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
                                    "run_id": run_id,
                                    "agent_version_id": agent_version_id,
                                    "alert_id": req.alert_id,
                                    "case_id": req.case_id,
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
                        run_id=run_id,
                        agent_version_id=agent_version_id,
                        session=session,
                        sdk_session_id=sdk_session_id,
                        alert_id=req.alert_id,
                        case_id=req.case_id,
                        answer=answer,
                        messages=messages,
                        agent_activity=agent_activity,
                        usage=usage,
                        total_cost_usd=total_cost_usd,
                        stop_reason=stop_reason,
                        errors=errors,
                    )
                    completed_at = utc_now()
                    self._record_feedback_run(
                        run_id=run_id,
                        agent_version_id=agent_version_id,
                        session=session,
                        sdk_session_id=sdk_session_id,
                        req=req,
                        answer=answer,
                        messages=messages,
                        agent_activity=agent_activity,
                        usage=usage,
                        total_cost_usd=total_cost_usd,
                        stop_reason=stop_reason,
                        errors=errors,
                        created_at=created_at,
                        completed_at=completed_at,
                        langfuse_trace_id=langfuse_trace_id,
                        langfuse_trace_url=langfuse_trace_url,
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
