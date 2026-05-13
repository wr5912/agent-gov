from __future__ import annotations

import json
import os
import warnings
from contextlib import nullcontext
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Optional

from .agent_loader import load_programmatic_agents
from .message_utils import extract_text, message_event_name, to_plain
from .policy import build_default_hooks, guard_tool_use
from .schemas import ChatRequest
from .session_store import LocalSession, LocalSessionStore
from .settings import AppSettings


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
