from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, Callable

from .agent_profiles import AgentRuntimeProfile
from .message_utils import extract_text
from .mcp_config import filtered_mcp_servers
from .output_formatter import DSPyOutputFormatter
from .policy import build_default_hooks, guard_tool_use
from .json_types import JsonObject
from .settings import AppSettings


class ClaudeCodeResultError(RuntimeError):
    """Raised when Claude Code reports a structured result error."""


class AgentJobRunner:
    """Runs isolated feedback-loop Agent profiles and normalizes JSON output."""

    def __init__(
        self,
        *,
        settings: AppSettings,
        profiles: dict[str, AgentRuntimeProfile],
        env_builder: Callable[[AgentRuntimeProfile], dict[str, str]],
        output_formatter: DSPyOutputFormatter,
    ) -> None:
        self.settings = settings
        self.profiles = profiles
        self.env_builder = env_builder
        self.output_formatter = output_formatter

    def build_options(self, profile: AgentRuntimeProfile) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions

        env = self.env_builder(profile)
        if self.settings.provider_api_key:
            env["ANTHROPIC_API_KEY"] = self.settings.provider_api_key
        if self.settings.provider_api_url:
            env["ANTHROPIC_BASE_URL"] = self.settings.provider_api_url

        kwargs: dict[str, object] = {
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
            "mcp_servers": filtered_mcp_servers(profile.mcp_config_path, profile.allowed_mcp_servers, env),
            "strict_mcp_config": True,
            "include_hook_events": self.settings.include_hook_events,
            "include_partial_messages": False,
            "hooks": build_default_hooks(profile) if self.settings.enable_policy_hooks else None,
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
        kwargs = {key: value for key, value in kwargs.items() if value is not None}
        return ClaudeAgentOptions(**kwargs)

    async def run_profile_json(
        self,
        *,
        profile_name: str,
        prompt: str,
        expected_schema_version: str,
        job_type: str,
        job_input: JsonObject,
    ) -> JsonObject:
        from claude_agent_sdk import ResultMessage, query

        profile = self.profiles[profile_name]
        answer_parts: list[str] = []
        errors: list[str] = []
        options = self.build_options(profile)

        async def collect() -> JsonObject:
            async for msg in query(prompt=self.single_prompt_stream(prompt), options=options):
                text = extract_text(msg)
                if text:
                    answer_parts.append(text)
                    output_bytes = len("".join(answer_parts).encode("utf-8"))
                    if output_bytes > profile.max_output_bytes:
                        raise RuntimeError(f"Agent output exceeded {profile.max_output_bytes} bytes")
                if isinstance(msg, ResultMessage):
                    errors.extend(self.result_errors(msg))
            answer = self.dedupe_answer_parts(answer_parts)
            if errors and not answer:
                raise ClaudeCodeResultError("; ".join(errors))
            return await self.format_agent_text(
                job_type=job_type,
                raw_text=answer,
                job_input=job_input,
                expected_schema_version=expected_schema_version,
            )

        return await asyncio.wait_for(collect(), timeout=profile.max_runtime_seconds)

    async def format_agent_text(
        self,
        *,
        job_type: str,
        raw_text: str,
        job_input: JsonObject,
        expected_schema_version: str,
    ) -> JsonObject:
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
        return result.payload

    @staticmethod
    async def single_prompt_stream(prompt: str) -> AsyncIterator[JsonObject]:
        yield {
            "type": "user",
            "message": {"role": "user", "content": prompt},
            "parent_tool_use_id": None,
            "session_id": "default",
        }

    @staticmethod
    def result_errors(msg: Any) -> list[str]:
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

    @staticmethod
    def dedupe_answer_parts(parts: list[str]) -> str:
        seen: set[str] = set()
        unique: list[str] = []
        for part in parts:
            text = part.strip()
            if not text or text in seen:
                continue
            seen.add(text)
            unique.append(text)
        return "\n".join(unique).strip()
