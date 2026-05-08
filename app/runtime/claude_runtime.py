from __future__ import annotations

import os
from pathlib import Path
from typing import Any, AsyncIterator, Optional

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

    def _build_options(self, req: ChatRequest, session: LocalSession) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions

        env = dict(os.environ)
        env.update(self.settings.claude_env)
        if self.settings.provider_api_key:
            env["ANTHROPIC_API_KEY"] = self.settings.provider_api_key
        if self.settings.provider_api_url:
            env["ANTHROPIC_BASE_URL"] = self.settings.provider_api_url
        env["CLAUDE_AGENT_SDK_CLIENT_APP"] = "claude-agent-runtime-api/0.1.0"
        env["CLAUDE_CONFIG_DIR"] = str(self.settings.resolved_claude_config_dir)
        Path(env["CLAUDE_CONFIG_DIR"]).mkdir(parents=True, exist_ok=True)

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

    async def run(self, req: ChatRequest) -> dict[str, Any]:
        from claude_agent_sdk import ResultMessage, query

        session = self.session_store.get_or_create(req.session_id, metadata=req.metadata)
        prompt = self._build_prompt(req)
        options = self._build_options(req, session)

        messages: list[dict[str, Any]] = []
        answer_parts: list[str] = []
        usage: Optional[dict[str, Any]] = None
        total_cost_usd: Optional[float] = None
        stop_reason: Optional[str] = None
        errors: list[str] = []
        sdk_session_id: Optional[str] = session.sdk_session_id

        try:
            async for msg in query(prompt=prompt, options=options):
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
                    if getattr(msg, "errors", None):
                        errors.extend([str(e) for e in msg.errors])
        except Exception as exc:
            errors.append(f"{exc.__class__.__name__}: {exc}")

        if sdk_session_id:
            session.sdk_session_id = sdk_session_id
        session.turns += 1
        if not session.title:
            session.title = req.message[:80]
        self.session_store.save(session)

        # Deduplicate ResultMessage.result when it equals concatenated text from assistant messages.
        answer = "\n".join(part for part in answer_parts if part).strip()
        return {
            "session_id": session.session_id,
            "sdk_session_id": session.sdk_session_id,
            "answer": answer,
            "messages": messages,
            "usage": usage,
            "total_cost_usd": total_cost_usd,
            "stop_reason": stop_reason,
            "errors": errors,
        }

    async def stream(self, req: ChatRequest) -> AsyncIterator[dict[str, Any]]:
        from claude_agent_sdk import ResultMessage, query

        session = self.session_store.get_or_create(req.session_id, metadata=req.metadata)
        yield {"event": "session", "data": {"session_id": session.session_id, "sdk_session_id": session.sdk_session_id}}

        prompt = self._build_prompt(req)
        options = self._build_options(req, session)
        sdk_session_id: Optional[str] = session.sdk_session_id
        errors: list[str] = []

        try:
            async for msg in query(prompt=prompt, options=options):
                text = extract_text(msg)
                plain = to_plain(msg)
                event = message_event_name(msg)
                yield {"event": "message", "data": {"event": event, "text": text, "raw": plain}}

                candidate_session_id = getattr(msg, "session_id", None)
                if candidate_session_id:
                    sdk_session_id = candidate_session_id

                if isinstance(msg, ResultMessage):
                    yield {
                        "event": "result",
                        "data": {
                            "session_id": session.session_id,
                            "sdk_session_id": sdk_session_id,
                            "usage": getattr(msg, "usage", None) or getattr(msg, "model_usage", None),
                            "total_cost_usd": getattr(msg, "total_cost_usd", None),
                            "stop_reason": getattr(msg, "stop_reason", None),
                            "errors": getattr(msg, "errors", None) or [],
                        },
                    }
        except Exception as exc:
            errors.append(f"{exc.__class__.__name__}: {exc}")
            yield {"event": "error", "data": {"errors": errors}}
        finally:
            if sdk_session_id:
                session.sdk_session_id = sdk_session_id
            session.turns += 1
            if not session.title:
                session.title = req.message[:80]
            self.session_store.save(session)
            yield {"event": "done", "data": "[DONE]"}
