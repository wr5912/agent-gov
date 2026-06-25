from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

from .agent_job_errors import AgentAuthenticationRequiredError, provider_api_key_configured
from .agent_job_types import AgentJobType, FormatterOutputModel
from .agent_profiles import AgentRuntimeProfile
from .json_types import JsonObject
from .message_utils import extract_text
from .model_provider import ModelProviderRouter
from .output_formatter import DSPyOutputFormatter
from .settings import AppSettings

logger = logging.getLogger(__name__)


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
        provider_router: ModelProviderRouter | None = None,
    ) -> None:
        self.settings = settings
        self.profiles = profiles
        self.env_builder = env_builder
        self.output_formatter = output_formatter
        self.provider_router = provider_router or ModelProviderRouter(settings)

    def build_options(self, profile: AgentRuntimeProfile) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions

        env = self.env_builder(profile)
        env.update(self.provider_router.claude_env())

        kwargs: dict[str, object] = {
            "cwd": profile.workspace_dir,
            "model": self.settings.agent_model,
            "fallback_model": self.settings.fallback_model,
            "max_turns": max(self.settings.max_turns, profile.max_turns or 0),
            "max_budget_usd": self.settings.max_budget_usd,
            "env": env,
            "include_hook_events": self.settings.include_hook_events,
            "include_partial_messages": False,
            "cli_path": self.settings.claude_cli_path,
            "add_dirs": self.settings.claude_add_dirs,
            "betas": self.settings.claude_betas,
            "permission_prompt_tool_name": self.settings.permission_prompt_tool_name,
            "max_buffer_size": self.settings.max_buffer_size,
            "user": self.settings.claude_user,
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
        job_type: AgentJobType | str,
        job_input: JsonObject,
    ) -> FormatterOutputModel:
        from claude_agent_sdk import ResultMessage, query

        profile = self.profiles[profile_name]
        self.raise_if_missing_model_credentials(profile)
        self.provider_router.ensure_agent_runtime_ready()
        answer_parts: list[str] = []
        errors: list[str] = []
        options = self.build_options(profile)

        async def collect() -> FormatterOutputModel:
            async for msg in query(prompt=self.single_prompt_stream(prompt, session_id=f"governor-job-{uuid.uuid4()}"), options=options):
                text = extract_text(msg)
                if text:
                    logger.debug(
                        "agent profile stream text profile_name=%s job_type=%s text=%s",
                        profile_name,
                        getattr(job_type, "value", job_type),
                        text,
                    )
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
            )

        return await asyncio.wait_for(collect(), timeout=profile.max_runtime_seconds)

    async def format_agent_text(
        self,
        *,
        job_type: AgentJobType | str,
        raw_text: str,
        job_input: JsonObject,
    ) -> FormatterOutputModel:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                self.output_formatter.format,
                job_type=job_type,
                raw_text=raw_text,
                job_input=job_input,
            ),
            timeout=self.settings.dspy_output_formatter_timeout_seconds,
        )
        return result.output

    def raise_if_missing_model_credentials(self, profile: AgentRuntimeProfile) -> None:
        if self.provider_credentials_configured():
            return
        env_file = self.settings.settings_env_file
        missing = ["MODEL_PROVIDER_API_URL"] if not self.provider_router.route().provider_api_key_required else ["MODEL_PROVIDER_API_KEY", "ANTHROPIC_API_KEY"]
        raise AgentAuthenticationRequiredError(
            profile_name=profile.name,
            runtime_volume_mode=self.settings.runtime_volume_mode,
            settings_env_file=env_file.as_posix() if env_file else None,
            missing=missing,
        )

    def provider_api_key_configured(self) -> bool:
        return provider_api_key_configured(self.settings.provider_api_key)

    def provider_credentials_configured(self) -> bool:
        return self.provider_router.provider_credentials_configured()

    @staticmethod
    async def single_prompt_stream(prompt: str, *, session_id: str | None = None) -> AsyncIterator[JsonObject]:
        # session_id 是喂给 SDK 的单条 user 消息 envelope id。主聊天沿用 "default"；
        # 治理 job 由调用方传入独立 id，取代所有 job 共用 "default"（整改方案 §5.6 动作 1）。
        yield {
            "type": "user",
            "message": {"role": "user", "content": prompt},
            "parent_tool_use_id": None,
            "session_id": session_id or "default",
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
