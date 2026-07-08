from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from app.services.feedback_eval_runner import FeedbackEvalRunner

from .agent_git_store import AgentVersionProvider
from .agent_job_runner import AgentJobRunner, ClaudeCodeResultError
from .agent_job_types import FormatterOutputModel
from .agent_profile_versions import profile_version_snapshot
from .agent_profiles import (
    MAIN_AGENT_PROFILE,
    PROFILE_VERSION_IDS,
    AgentRuntimeProfile,
    build_profiles,
    candidate_profile,
)
from .claude_user_input_service import ClaudeUserInputService
from .errors import RuntimeUnavailableError
from .feedback_runtime_jobs import FeedbackRuntimeJobsMixin
from .governor_job_trace import run_governor_profile_json
from .integrations.runtime_langfuse import RuntimeLangfuseClient
from .json_types import JsonObject
from .message_utils import extract_text, message_event_name, to_plain
from .model_provider import ModelProviderRouter
from .output_formatter import DSPyOutputFormatter
from .runtime_activity import RuntimeActivityExtractor
from .runtime_db import utc_now
from .schemas import ChatRequest, ChatResponse
from .sdk_session_errors import is_missing_sdk_session_error
from .session_store import LocalSession, LocalSessionStore
from .settings import AppSettings
from .stores.feedback_store import FeedbackStore

_LANGFUSE_ATTRIBUTE_MAX_LENGTH = 200


def clean_langfuse_attribute_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (int, float, str)):
        text = str(value)
    else:
        return None
    text = text.strip()
    if not text:
        return None
    return text[:_LANGFUSE_ATTRIBUTE_MAX_LENGTH]


@dataclass
class RuntimeRequestContext:
    session: LocalSession
    run_id: str
    agent_version_id: Optional[str]
    created_at: str
    prompt: str
    telemetry_input: JsonObject
    langfuse_trace_id: Optional[str] = None
    langfuse_trace_url: Optional[str] = None
    agent_id: str = MAIN_AGENT_PROFILE


@dataclass
class RuntimeQueryState:
    sdk_session_id: Optional[str]
    messages: list[JsonObject] = field(default_factory=list)
    answer_parts: list[str] = field(default_factory=list)
    usage: Any = None
    total_cost_usd: Optional[float] = None
    stop_reason: Optional[str] = None
    errors: list[str] = field(default_factory=list)


class ClaudeRuntime(FeedbackRuntimeJobsMixin):
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
        agent_version_store: AgentVersionProvider | None = None,
        user_input_service: ClaudeUserInputService | None = None,
    ) -> None:
        self.settings = settings
        self.session_store = session_store
        self.feedback_store = feedback_store
        self.agent_version_store = agent_version_store
        self.user_input_service = user_input_service
        self.profiles = build_profiles(settings)
        self.activity_extractor = RuntimeActivityExtractor(settings)
        self.langfuse = RuntimeLangfuseClient(settings)
        self.model_provider_router = ModelProviderRouter(settings)
        self.output_formatter = DSPyOutputFormatter(settings, langfuse=self.langfuse, provider_router=self.model_provider_router)
        self.job_runner = AgentJobRunner(
            settings=settings,
            profiles=self.profiles,
            env_builder=self._profile_env,
            output_formatter=self.output_formatter,
            provider_router=self.model_provider_router,
        )
        self.eval_runner = (
            FeedbackEvalRunner(
                feedback_store=feedback_store,
                run_chat=self.run,
                current_agent_version_id=self._current_agent_version_id,
                run_candidate_chat=lambda req, wt, commit, cs, aid: self.run_candidate(
                    req, worktree_path=wt, candidate_commit_sha=commit, change_set_id=cs, agent_id=aid
                ),
            )
            if feedback_store is not None
            else None
        )

    def _build_prompt(self, req: ChatRequest) -> str:
        parts: list[str] = []
        agent = req.agent or self.settings.default_agent
        skills = req.skills if req.skills is not None else self.settings.default_skills
        if agent:
            parts.append(
                f"请优先委派或使用名为 `{agent}` 的 Claude Code subagent 处理本次任务；如果运行时无法直接切换到该 subagent，则按该 subagent 的职责边界执行。"
            )
        if skills:
            parts.append(f"本次任务优先使用这些 Skills：{', '.join(skills)}。")
        parts.append(req.message)
        return "\n\n".join(parts)

    def _should_suppress_exception(self, exc: Exception, errors: list[str]) -> bool:
        if not errors:
            return False
        return isinstance(exc, ClaudeCodeResultError)

    def _should_retry_without_sdk_resume(
        self,
        exc: Exception,
        context: RuntimeRequestContext,
        state: RuntimeQueryState,
    ) -> bool:
        return (
            self.settings.enable_sdk_session_resume
            and context.session.sdk_session_id is not None
            and not state.messages
            and not state.errors
            and is_missing_sdk_session_error(exc)
        )

    def _clear_stale_sdk_session(self, context: RuntimeRequestContext) -> None:
        context.session.sdk_session_id = None
        self.session_store.save(context.session)

    def _request_telemetry_input(
        self,
        req: ChatRequest,
        prompt: str,
        session: LocalSession,
        run_id: str,
        agent_version_id: Optional[str],
    ) -> JsonObject:
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
            "allowed_tools": None,
            "disallowed_tools": None,
            "max_turns": req.max_turns or self.settings.max_turns,
            "model": req.model or self.settings.agent_model,
            "permission_mode": None,
            "claude_config_source": "official_files",
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
        langfuse_trace_id: Optional[str],
        langfuse_trace_url: Optional[str],
        answer: str,
        messages: list[JsonObject],
        agent_activity: JsonObject,
        usage: Any,
        total_cost_usd: Optional[float],
        stop_reason: Optional[str],
        errors: list[str],
    ) -> JsonObject:
        return {
            "run_id": run_id,
            "agent_version_id": agent_version_id,
            "api_session_id": session.session_id,
            "sdk_session_id": sdk_session_id,
            "alert_id": alert_id,
            "case_id": case_id,
            "langfuse_trace_id": langfuse_trace_id,
            "langfuse_trace_url": langfuse_trace_url,
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
        agent_id: str,
        agent_version_id: Optional[str],
        session: LocalSession,
        sdk_session_id: Optional[str],
        req: ChatRequest,
        answer: str,
        messages: list[JsonObject],
        agent_activity: JsonObject,
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
                "agent_id": agent_id,
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

    def _current_agent_version_id(self, agent_id: Optional[str] = None) -> Optional[str]:
        # #24-D：复用 feedback_store 的 per-agent 版本解析器；无 feedback_store 时回退主 store。
        if self.feedback_store is not None:
            return self.feedback_store._current_agent_version_id(agent_id)
        return self.agent_version_store.current_version_id() if self.agent_version_store else None

    def profile_version_snapshot(self, profile_name: str) -> JsonObject | None:
        profile = self.profiles.get(profile_name)
        if profile is None:
            return None
        version_id = PROFILE_VERSION_IDS.get(profile_name)  # type: ignore[arg-type]
        return profile_version_snapshot(profile, version_id=version_id) if version_id else profile_version_snapshot(profile)

    def _raise_if_version_maintenance(self) -> None:
        if self.agent_version_store is not None and self.agent_version_store.is_maintenance_active():
            raise RuntimeUnavailableError("Agent version maintenance is in progress; retry after restore completes.")

    def fetch_langfuse_trace(self, trace_id: str) -> Optional[JsonObject]:
        return self.langfuse.fetch_trace(trace_id)

    def _flush_langfuse(self) -> None:
        client = self.langfuse.get_client()
        if client is None:
            return
        try:
            client.flush()
        except Exception as exc:
            print(f"[WARN] failed to flush Langfuse runtime enrichment: {exc}", flush=True)

    def _profile_env(self, profile: AgentRuntimeProfile) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self.langfuse.build_env())
        claude_env = self.settings.claude_env
        env.update(claude_env)
        env["HOME"] = str(profile.claude_root)
        env["CLAUDE_CONFIG_DIR"] = str(profile.claude_config_dir)
        env["DATA_DIR"] = str(profile.data_dir)
        if "CLAUDE_HOOK_AUDIT_LOG" not in claude_env:
            env["CLAUDE_HOOK_AUDIT_LOG"] = str(profile.data_dir / "transcripts" / "claude-hook-audit.jsonl")
        env["AGENT_PROFILE"] = profile.name
        env["CLAUDE_AGENT_SDK_CLIENT_APP"] = f"secops-runtime/{profile.name}"
        profile.claude_root.mkdir(parents=True, exist_ok=True)
        profile.claude_config_dir.mkdir(parents=True, exist_ok=True)
        return env

    def _build_options(
        self,
        req: ChatRequest,
        session: LocalSession,
        *,
        profile: AgentRuntimeProfile | None = None,
        execution_mode: str = "stream",
        can_use_tool: Any = None,
    ) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions

        profile = profile or self.profiles[MAIN_AGENT_PROFILE]
        env = self._profile_env(profile)
        env.update(self.model_provider_router.claude_env())

        system_append = "\n\n".join(part for part in [self.settings.claude_system_append, req.system_append] if part)
        system_prompt = {"type": "preset", "preset": "claude_code"}
        if system_append:
            system_prompt = {"type": "preset", "preset": "claude_code", "append": system_append}

        kwargs: dict[str, object] = {
            "cwd": profile.workspace_dir,
            "model": req.model or self.settings.agent_model,
            "fallback_model": self.settings.fallback_model,
            "max_turns": req.max_turns or self.settings.max_turns,
            "max_budget_usd": self.settings.max_budget_usd,
            "system_prompt": system_prompt,
            "env": env,
            "include_hook_events": self.settings.include_hook_events,
            "include_partial_messages": self.settings.include_partial_messages,
            "cli_path": self.settings.claude_cli_path,
            "add_dirs": self.settings.claude_add_dirs,
            "betas": self.settings.claude_betas,
            "max_buffer_size": self.settings.max_buffer_size,
            "user": self.settings.claude_user,
            "extra_args": self.settings.claude_extra_args,
            "max_thinking_tokens": self.settings.max_thinking_tokens,
            "effort": self.settings.effort,
            "enable_file_checkpointing": self.settings.enable_file_checkpointing,
            "session_store_flush": self.settings.session_store_flush,
            "load_timeout_ms": self.settings.load_timeout_ms,
        }
        if execution_mode == "non_stream_bypass":
            kwargs["permission_mode"] = "bypassPermissions"
        elif execution_mode == "stream_hitl":
            kwargs["permission_mode"] = "default"
        if can_use_tool is not None:
            kwargs["can_use_tool"] = can_use_tool
        if self.settings.setting_sources is not None:
            kwargs["setting_sources"] = self.settings.setting_sources
        if self.settings.permission_prompt_tool_name and execution_mode not in {"non_stream_bypass", "stream_hitl"}:
            kwargs["permission_prompt_tool_name"] = self.settings.permission_prompt_tool_name

        # Resume the previous Claude Code session when possible. The API session id
        # is not necessarily equal to the internal Claude session id returned by SDK.
        if self.settings.enable_sdk_session_resume and session.sdk_session_id:
            kwargs["resume"] = session.sdk_session_id
        elif session.turns == 0:
            # If caller provides a UUID-looking session id, use it for the first Claude session.
            # Invalid IDs are simply ignored by the SDK if omitted.
            try:
                uuid.UUID(session.session_id)
                kwargs["session_id"] = session.session_id
            except ValueError:
                pass

        # Remove None values because older SDK versions may not accept them everywhere.
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        return ClaudeAgentOptions(**kwargs)

    async def _run_profile_json(
        self,
        *,
        profile_name: str,
        prompt: str,
        job_type: str,
        job_input: JsonObject,
        governor: Optional[JsonObject] = None,
        trace_callback: Callable[[JsonObject], None] | None = None,
    ) -> FormatterOutputModel:
        self.job_runner.output_formatter = self.output_formatter

        async def run() -> FormatterOutputModel:
            return await self.job_runner.run_profile_json(profile_name=profile_name, prompt=prompt, job_type=job_type, job_input=job_input)

        if governor is not None:
            governor = {
                **governor,
                "input": {
                    "profile_name": profile_name,
                    "job_type": job_type,
                    "prompt": prompt,
                    "job_input": job_input,
                    "governor": dict(governor),
                },
            }
        return await run_governor_profile_json(self.langfuse, run, governor, trace_callback=trace_callback)

    async def _format_agent_text(self, *, job_type: str, raw_text: str, job_input: JsonObject) -> FormatterOutputModel:
        """直接把一段原始文本经 DSPy formatter 结构化（无 governor loop）；供反馈整理等无需工具的归纳复用。"""
        self.job_runner.output_formatter = self.output_formatter
        return await self.job_runner.format_agent_text(job_type=job_type, raw_text=raw_text, job_input=job_input)

    def _new_runtime_request_context(
        self, req: ChatRequest, *, agent_version_id_override: Optional[str] = None, agent_id: str = MAIN_AGENT_PROFILE
    ) -> RuntimeRequestContext:
        self._raise_if_version_maintenance()
        session = self.session_store.get_or_create(req.session_id, metadata=req.metadata)
        run_id = str(uuid.uuid4())
        agent_version_id = agent_version_id_override if agent_version_id_override is not None else self._current_agent_version_id(agent_id)
        created_at = utc_now()
        prompt = self._build_prompt(req)
        telemetry_input = self._request_telemetry_input(req, prompt, session, run_id, agent_version_id)
        return RuntimeRequestContext(
            session=session,
            run_id=run_id,
            agent_version_id=agent_version_id,
            agent_id=agent_id,
            created_at=created_at,
            prompt=prompt,
            telemetry_input=telemetry_input,
        )

    def _runtime_observation_metadata(
        self,
        context: RuntimeRequestContext,
        mode: str,
        *,
        profile: AgentRuntimeProfile | None = None,
    ) -> JsonObject:
        telemetry = context.telemetry_input
        return {
            "api_session_id": context.session.session_id,
            "sdk_session_id": context.session.sdk_session_id,
            "run_id": context.run_id,
            "agent_version_id": context.agent_version_id,
            "alert_id": telemetry.get("alert_id"),
            "case_id": telemetry.get("case_id"),
            "mode": mode,
            "permission_mode": telemetry.get("permission_mode"),
            "claude_web_hitl_enabled": telemetry.get("claude_web_hitl_enabled"),
            "profile": (profile or self.profiles[MAIN_AGENT_PROFILE]).name,
            "agent": telemetry.get("agent"),
            "skills_mode": telemetry.get("skills_mode"),
        }

    def _langfuse_propagation_attributes(
        self,
        req: ChatRequest,
        context: RuntimeRequestContext,
        mode: str,
        *,
        profile: AgentRuntimeProfile | None = None,
    ) -> JsonObject:
        metadata = {key: clean_langfuse_attribute_value(value) for key, value in self._runtime_observation_metadata(context, mode, profile=profile).items()}
        metadata.update(self._business_metadata(req.metadata))
        return {
            "user_id": self._langfuse_user_id(req.metadata),
            "session_id": context.session.session_id,
            "trace_name": (profile or self.profiles[MAIN_AGENT_PROFILE]).langfuse_observation_name,
            "tags": ["role:business", f"agent:{(profile or self.profiles[MAIN_AGENT_PROFILE]).name}"],  # §4.4 多主体
            "metadata": {key: value for key, value in metadata.items() if value},
        }

    @staticmethod
    def _business_metadata(metadata: JsonObject) -> Mapping[str, str]:
        aliases = {
            "tenant_id": ("tenant_id", "tenantId"),
            "agent_id": ("agent_id", "agentId"),
        }
        values: dict[str, str] = {}
        for target, names in aliases.items():
            for name in names:
                value = clean_langfuse_attribute_value(metadata.get(name))
                if value:
                    values[target] = value
                    break
        return values

    @staticmethod
    def _langfuse_user_id(metadata: JsonObject) -> Optional[str]:
        for name in ("user_id", "userId", "user.id", "langfuse.user.id"):
            value = clean_langfuse_attribute_value(metadata.get(name))
            if value:
                return value
        return None

    def _generation_input(self, req: ChatRequest, context: RuntimeRequestContext) -> JsonObject:
        return {
            "run_id": context.run_id,
            "agent_version_id": context.agent_version_id,
            "prompt": context.prompt,
            "model": req.model or self.settings.agent_model,
        }

    def _track_query_message(
        self,
        msg: Any,
        state: RuntimeQueryState,
        result_message_type: type,
    ) -> tuple[str, str, JsonObject, bool, list[str]]:
        text = extract_text(msg)
        plain_value = to_plain(msg)
        plain: JsonObject = plain_value if isinstance(plain_value, dict) else {"value": plain_value}
        event = message_event_name(msg)
        plain["event"] = event
        state.messages.append(plain)
        if text:
            state.answer_parts.append(text)

        candidate_session_id = getattr(msg, "session_id", None)
        if candidate_session_id:
            state.sdk_session_id = candidate_session_id

        if not isinstance(msg, result_message_type):
            return event, text, plain, False, []
        state.usage = getattr(msg, "usage", None) or getattr(msg, "model_usage", None)
        state.total_cost_usd = getattr(msg, "total_cost_usd", None)
        state.stop_reason = getattr(msg, "stop_reason", None)
        result_errors = AgentJobRunner.result_errors(msg)
        state.errors.extend(result_errors)
        return event, text, plain, True, result_errors

    def _runtime_output_from_state(
        self,
        req: ChatRequest,
        context: RuntimeRequestContext,
        state: RuntimeQueryState,
    ) -> tuple[str, JsonObject, JsonObject]:
        answer = AgentJobRunner.dedupe_answer_parts(state.answer_parts)
        agent_activity = self.activity_extractor.agent_activity_payload(req, state.messages)
        output = self._runtime_output_payload(
            run_id=context.run_id,
            agent_version_id=context.agent_version_id,
            session=context.session,
            sdk_session_id=state.sdk_session_id,
            alert_id=req.alert_id,
            case_id=req.case_id,
            langfuse_trace_id=context.langfuse_trace_id,
            langfuse_trace_url=context.langfuse_trace_url,
            answer=answer,
            messages=state.messages,
            agent_activity=agent_activity,
            usage=state.usage,
            total_cost_usd=state.total_cost_usd,
            stop_reason=state.stop_reason,
            errors=state.errors,
        )
        return answer, agent_activity, output

    def _update_runtime_observations(
        self,
        root_span: Any,
        generation: Any,
        context: RuntimeRequestContext,
        state: RuntimeQueryState,
        output: JsonObject,
        trace_attributes: JsonObject,
    ) -> None:
        level = "ERROR" if state.errors else "DEFAULT"
        status_message = "\n".join(state.errors) if state.errors else None
        self.langfuse.update_observation(
            generation,
            output=output,
            usage_details=self.activity_extractor.usage_details(state.usage),
            cost_details=self.activity_extractor.cost_details(state.total_cost_usd),
            level=level,
            status_message=status_message,
        )
        self.langfuse.update_observation(
            root_span,
            input=context.telemetry_input,
            output=output,
            level=level,
            status_message=status_message,
        )
        self.langfuse.set_trace_attributes(root_span, **trace_attributes)
        self.langfuse.set_trace_attributes(generation, **trace_attributes)
        self.langfuse.set_trace_io(root_span, input=context.telemetry_input, output=output)
        # 从 SDK message 流投影逐工具/逐轮 I/O 子观测（补 claude_code.* span 的空 Input/Output）
        self.langfuse.emit_sdk_child_observations(generation, self.activity_extractor.sdk_child_observations(state.messages))

    def _sync_langfuse_trace(self, context: RuntimeRequestContext, trace_attributes: JsonObject, output: JsonObject) -> None:
        self.langfuse.upsert_trace(
            context.langfuse_trace_id,
            name=trace_attributes.get("trace_name"),
            session_id=trace_attributes.get("session_id"),
            user_id=trace_attributes.get("user_id"),
            input=context.telemetry_input,
            output=output,
            metadata=trace_attributes.get("metadata") if isinstance(trace_attributes.get("metadata"), dict) else None,
            tags=trace_attributes.get("tags") if isinstance(trace_attributes.get("tags"), list) else None,
        )

    def _complete_runtime_request(
        self,
        req: ChatRequest,
        context: RuntimeRequestContext,
        state: RuntimeQueryState,
        answer: str,
        agent_activity: JsonObject,
    ) -> None:
        if state.sdk_session_id:
            context.session.sdk_session_id = state.sdk_session_id
        context.session.turns += 1
        context.session.title = context.session.title or req.message[:80]
        context.session.agent_id = context.agent_id  # 归属 Agent 随完成态落库，端点据此强校验/定位 transcript
        self.session_store.save(context.session)
        self._record_feedback_run(
            run_id=context.run_id,
            agent_id=context.agent_id,
            agent_version_id=context.agent_version_id,
            session=context.session,
            sdk_session_id=state.sdk_session_id,
            req=req,
            answer=answer,
            messages=state.messages,
            agent_activity=agent_activity,
            usage=state.usage,
            total_cost_usd=state.total_cost_usd,
            stop_reason=state.stop_reason,
            errors=state.errors,
            created_at=context.created_at,
            completed_at=utc_now(),
            langfuse_trace_id=context.langfuse_trace_id,
            langfuse_trace_url=context.langfuse_trace_url,
        )

    def _run_response(self, context: RuntimeRequestContext, state: RuntimeQueryState, answer: str, agent_activity: JsonObject) -> ChatResponse:
        return ChatResponse(
            run_id=context.run_id,
            agent_version_id=context.agent_version_id,
            langfuse_trace_id=context.langfuse_trace_id,
            langfuse_trace_url=context.langfuse_trace_url,
            session_id=context.session.session_id,
            sdk_session_id=context.session.sdk_session_id,
            answer=answer,
            messages=state.messages,
            agent_activity=agent_activity,
            usage=to_plain(state.usage),
            total_cost_usd=state.total_cost_usd,
            stop_reason=state.stop_reason,
            errors=state.errors,
        )

    @staticmethod
    def _stream_session_event(req: ChatRequest, context: RuntimeRequestContext) -> JsonObject:
        return {
            "event": "session",
            "data": {
                "run_id": context.run_id,
                "agent_version_id": context.agent_version_id,
                "session_id": context.session.session_id,
                "sdk_session_id": context.session.sdk_session_id,
                "alert_id": req.alert_id,
                "case_id": req.case_id,
            },
        }

    async def run(
        self,
        req: ChatRequest,
        *,
        profile: AgentRuntimeProfile | None = None,
        agent_version_id_override: Optional[str] = None,
    ) -> ChatResponse:
        from claude_agent_sdk import ResultMessage, query

        profile = profile or self.profiles[MAIN_AGENT_PROFILE]
        context = self._new_runtime_request_context(req, agent_version_id_override=agent_version_id_override, agent_id=profile.name)
        context.telemetry_input["permission_mode"] = "bypassPermissions"
        context.telemetry_input["claude_web_hitl_enabled"] = False
        state = RuntimeQueryState(sdk_session_id=context.session.sdk_session_id)
        root_metadata = self._runtime_observation_metadata(context, "non_stream", profile=profile)
        propagation = self._langfuse_propagation_attributes(req, context, "non_stream", profile=profile)
        with self.langfuse.propagate_attributes(**propagation):
            with self.langfuse.start_observation(
                as_type="span",
                name=profile.langfuse_observation_name,
                input=context.telemetry_input,
                metadata=root_metadata,
            ) as root_span:
                context.langfuse_trace_id, context.langfuse_trace_url = self.langfuse.current_trace_ref()
                self.langfuse.set_trace_attributes(root_span, **propagation)
                with self.langfuse.start_observation(
                    as_type="generation",
                    name=f"{profile.langfuse_observation_name}.claude_sdk_query",
                    input=self._generation_input(req, context),
                    model=req.model or self.settings.agent_model,
                    metadata=root_metadata,
                ) as generation:
                    self.langfuse.set_trace_attributes(generation, **propagation)
                    try:

                        async def execute_query() -> None:
                            self.model_provider_router.ensure_agent_runtime_ready()
                            options = self._build_options(req, context.session, profile=profile, execution_mode="non_stream_bypass")
                            prompt_stream = AgentJobRunner.single_prompt_stream(context.prompt)
                            async for msg in query(prompt=prompt_stream, options=options):
                                self._track_query_message(msg, state, ResultMessage)

                        try:
                            await execute_query()
                        except Exception as exc:
                            if not self._should_retry_without_sdk_resume(exc, context, state):
                                raise
                            self._clear_stale_sdk_session(context)
                            state = RuntimeQueryState(sdk_session_id=None)
                            await execute_query()
                    except Exception as exc:
                        if not self._should_suppress_exception(exc, state.errors):
                            state.errors.append(f"{exc.__class__.__name__}: {exc}")

                    answer, agent_activity, output = self._runtime_output_from_state(req, context, state)
                    self._update_runtime_observations(root_span, generation, context, state, output, propagation)
        self._flush_langfuse()
        self._sync_langfuse_trace(context, propagation, output)
        self._complete_runtime_request(req, context, state, answer, agent_activity)
        return self._run_response(context, state, answer, agent_activity)

    async def run_candidate(
        self, req: ChatRequest, *, worktree_path: Path, candidate_commit_sha: str, change_set_id: str, agent_id: str = MAIN_AGENT_PROFILE
    ) -> ChatResponse:
        # #24-A：候选 profile 按 change_set.agent_id 派生（归属/trace/隔离落到该业务 Agent）。
        profile = candidate_profile(self.settings, agent_id=agent_id, workspace_dir=worktree_path, candidate_id=change_set_id)
        return await self.run(req, profile=profile, agent_version_id_override=candidate_commit_sha)

    async def stream(self, req: ChatRequest, *, profile: AgentRuntimeProfile | None = None) -> AsyncIterator[JsonObject]:
        from .claude_runtime_stream import stream_claude_runtime

        async for event in stream_claude_runtime(self, req, profile=profile):
            yield event
