from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Optional

from .agent_profiles import (
    ATTRIBUTION_ANALYZER_PROFILE,
    EVAL_CASE_GOVERNOR_PROFILE,
    EXECUTION_OPTIMIZER_PROFILE,
    MAIN_AGENT_PROFILE,
    PROFILE_VERSION_IDS,
    PROPOSAL_GENERATOR_PROFILE,
    REGRESSION_IMPACT_ANALYZER_PROFILE,
    AgentRuntimeProfile,
    build_profiles,
)
from .agent_job_runner import AgentJobRunner, ClaudeCodeResultError
from .agent_loader import load_programmatic_agents
from .agent_profile_versions import profile_version_snapshot
from .agent_version_store import AgentVersionStore
from .errors import RuntimeUnavailableError
from .runtime_db import utc_now
from .stores.feedback_store import FeedbackStore
from .message_utils import extract_text, message_event_name, to_plain
from .mcp_config import filtered_mcp_servers
from .output_formatter import DSPyOutputFormatter
from .policy import build_default_hooks, guard_tool_use
from .runtime_activity import RuntimeActivityExtractor
from .integrations.runtime_langfuse import RuntimeLangfuseClient, ensure_langfuse_otel_compat
from .schemas import ChatRequest
from .session_store import LocalSession, LocalSessionStore
from .settings import AppSettings
from app.services.feedback_job_orchestrator import FeedbackJobOrchestrator
from app.services.feedback_eval_runner import FeedbackEvalRunner


@dataclass
class RuntimeRequestContext:
    session: LocalSession
    run_id: str
    agent_version_id: Optional[str]
    created_at: str
    prompt: str
    telemetry_input: dict[str, Any]
    langfuse_trace_id: Optional[str] = None
    langfuse_trace_url: Optional[str] = None


@dataclass
class RuntimeQueryState:
    sdk_session_id: Optional[str]
    messages: list[dict[str, Any]] = field(default_factory=list)
    answer_parts: list[str] = field(default_factory=list)
    usage: Any = None
    total_cost_usd: Optional[float] = None
    stop_reason: Optional[str] = None
    errors: list[str] = field(default_factory=list)


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
        self.activity_extractor = RuntimeActivityExtractor(settings)
        self.langfuse = RuntimeLangfuseClient(settings)
        self.output_formatter = DSPyOutputFormatter(settings)
        self.job_runner = AgentJobRunner(
            settings=settings,
            profiles=self.profiles,
            env_builder=self._profile_env,
            output_formatter=self.output_formatter,
        )
        self.job_orchestrator = (
            FeedbackJobOrchestrator(
                feedback_store=feedback_store,
                profiles=self.profiles,
                run_profile_json=lambda **kwargs: self._run_profile_json(**kwargs),
            )
            if feedback_store is not None
            else None
        )
        self.eval_runner = (
            FeedbackEvalRunner(
                feedback_store=feedback_store,
                run_chat=self.run,
                current_agent_version_id=self._current_agent_version_id,
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

    def _should_suppress_exception(self, exc: Exception, errors: list[str]) -> bool:
        if not errors:
            return False
        return isinstance(exc, ClaudeCodeResultError)

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

    def profile_version_snapshot(self, profile_name: str) -> dict[str, Any] | None:
        profile = self.profiles.get(profile_name)
        if profile is None:
            return None
        version_id = PROFILE_VERSION_IDS.get(profile_name)  # type: ignore[arg-type]
        return profile_version_snapshot(profile, version_id=version_id) if version_id else profile_version_snapshot(profile)

    def _raise_if_version_maintenance(self) -> None:
        if self.agent_version_store is not None and self.agent_version_store.is_maintenance_active():
            raise RuntimeUnavailableError("Agent version maintenance is in progress; retry after restore completes.")

    def fetch_langfuse_trace(self, trace_id: str) -> Optional[dict[str, Any]]:
        return self.langfuse.fetch_trace(trace_id)

    def _main_observation_name(self) -> str:
        return self.profiles[MAIN_AGENT_PROFILE].langfuse_observation_name

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

        profile = self.profiles[MAIN_AGENT_PROFILE]
        env = self._profile_env(profile)
        if self.settings.provider_api_key:
            env["ANTHROPIC_API_KEY"] = self.settings.provider_api_key
        if self.settings.provider_api_url:
            env["ANTHROPIC_BASE_URL"] = self.settings.provider_api_url

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
            "mcp_servers": filtered_mcp_servers(profile.mcp_config_path, profile.allowed_mcp_servers),
            "strict_mcp_config": self.settings.strict_mcp_config,
            "skills": self._skills_option(req),
            "include_hook_events": self.settings.include_hook_events,
            "include_partial_messages": self.settings.include_partial_messages,
            "hooks": build_default_hooks(profile) if self.settings.enable_policy_hooks else None,
            "can_use_tool": guard_tool_use if self.settings.enable_policy_hooks else None,
            "agents": self._load_main_profile_agents(profile),
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
            try:
                uuid.UUID(session.session_id)
                kwargs["session_id"] = session.session_id
            except ValueError:
                pass

        # Remove None values because older SDK versions may not accept them everywhere.
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        return ClaudeAgentOptions(**kwargs)

    def _load_main_profile_agents(self, profile: AgentRuntimeProfile) -> Any | None:
        if not self.settings.enable_programmatic_agents:
            return None
        try:
            return load_programmatic_agents(profile.workspace_dir, profile.claude_config_dir)
        except Exception as exc:  # Do not prevent service use because of malformed agent file.
            print(f"[WARN] failed to load programmatic agents: {exc}", flush=True)
            return None

    async def _run_profile_json(
        self,
        *,
        profile_name: str,
        prompt: str,
        expected_schema_version: str,
        job_type: str,
        job_input: dict[str, Any],
    ) -> dict[str, Any]:
        self.job_runner.output_formatter = self.output_formatter
        return await self.job_runner.run_profile_json(
            profile_name=profile_name,
            prompt=prompt,
            expected_schema_version=expected_schema_version,
            job_type=job_type,
            job_input=job_input,
        )

    async def run_attribution_job(self, feedback_case_id: str, *, force: bool = False) -> dict[str, Any] | None:
        if self.job_orchestrator is None:
            return None
        return await self.job_orchestrator.run_attribution_job(feedback_case_id, force=force)

    def queue_attribution_job(self, feedback_case_id: str, *, force: bool = False) -> dict[str, Any] | None:
        if self.feedback_store is None:
            return None
        return self.feedback_store.queue_attribution_agent_job(
            feedback_case_id,
            profile_version=self.profile_version_snapshot(ATTRIBUTION_ANALYZER_PROFILE),
            force=force,
        )

    async def run_proposal_job(
        self,
        feedback_case_id: str,
        *,
        force: bool = False,
        regeneration_instruction: Optional[str] = None,
    ) -> dict[str, Any] | None:
        if self.job_orchestrator is None:
            return None
        return await self.job_orchestrator.run_proposal_job(
            feedback_case_id,
            force=force,
            regeneration_instruction=regeneration_instruction,
        )

    def queue_proposal_job(
        self,
        feedback_case_id: str,
        *,
        force: bool = False,
        regeneration_instruction: Optional[str] = None,
    ) -> dict[str, Any] | None:
        if self.feedback_store is None:
            return None
        return self.feedback_store.queue_proposal_agent_job(
            feedback_case_id,
            profile_version=self.profile_version_snapshot(PROPOSAL_GENERATOR_PROFILE),
            force=force,
            regeneration_instruction=regeneration_instruction,
        )

    async def run_batch_optimization_plan(
        self,
        batch_id: str,
        *,
        regeneration_instruction: Optional[str] = None,
        force: bool = True,
    ) -> dict[str, Any] | None:
        if self.job_orchestrator is None:
            return None
        return await self.job_orchestrator.run_batch_optimization_plan(
            batch_id,
            regeneration_instruction=regeneration_instruction,
            force=force,
        )

    def queue_batch_optimization_plan(
        self,
        batch_id: str,
        *,
        regeneration_instruction: Optional[str] = None,
        force: bool = True,
    ) -> dict[str, Any] | None:
        if self.feedback_store is None:
            return None
        return self.feedback_store.queue_batch_plan_agent_job(
            batch_id,
            profile_version=self.profile_version_snapshot(PROPOSAL_GENERATOR_PROFILE),
            force=force,
            regeneration_instruction=regeneration_instruction,
        )

    async def run_execution_job(self, optimization_task_id: str, *, force: bool = False) -> dict[str, Any] | None:
        if self.job_orchestrator is None:
            return None
        return await self.job_orchestrator.run_execution_job(optimization_task_id, force=force)

    def queue_execution_job(self, optimization_task_id: str, *, force: bool = False) -> dict[str, Any] | None:
        if self.feedback_store is None:
            return None
        return self.feedback_store.queue_execution_agent_job(
            optimization_task_id,
            profile_version=self.profile_version_snapshot(EXECUTION_OPTIMIZER_PROFILE),
            force=force,
        )

    def queue_eval_case_generation_job(
        self,
        *,
        feedback_case_id: Optional[str] = None,
        source_refs: Optional[list[dict[str, Any]]] = None,
        batch_id: Optional[str] = None,
        limit: int = 100,
        force: bool = False,
    ) -> dict[str, Any] | None:
        if self.feedback_store is None:
            return None
        return self.feedback_store.queue_feedback_eval_case_generation_agent_job(
            feedback_case_id=feedback_case_id,
            source_refs=source_refs,
            batch_id=batch_id,
            limit=limit,
            force=force,
            profile_version=self.profile_version_snapshot(EVAL_CASE_GOVERNOR_PROFILE),
        )

    def queue_regression_impact_analysis_job(self, eval_run_id: str, *, force: bool = False) -> dict[str, Any] | None:
        if self.feedback_store is None:
            return None
        return self.feedback_store.queue_regression_impact_agent_job(
            eval_run_id,
            profile_version=self.profile_version_snapshot(REGRESSION_IMPACT_ANALYZER_PROFILE),
            force=force,
        )

    async def run_feedback_eval(
        self,
        *,
        eval_case_ids: Optional[list[str]] = None,
        optimization_task_id: Optional[str] = None,
        source: str = "manual_feedback_dataset",
        regression_plan_id: Optional[str] = None,
        existing_eval_run_id: Optional[str] = None,
    ) -> dict[str, Any] | None:
        if self.eval_runner is None:
            return None
        return await self.eval_runner.run_feedback_eval(
            eval_case_ids=eval_case_ids,
            optimization_task_id=optimization_task_id,
            source=source,
            regression_plan_id=regression_plan_id,
            existing_eval_run_id=existing_eval_run_id,
        )

    def _new_runtime_request_context(self, req: ChatRequest) -> RuntimeRequestContext:
        self._raise_if_version_maintenance()
        session = self.session_store.get_or_create(req.session_id, metadata=req.metadata)
        run_id = str(uuid.uuid4())
        agent_version_id = self._current_agent_version_id()
        created_at = utc_now()
        prompt = self._build_prompt(req)
        telemetry_input = self._request_telemetry_input(req, prompt, session, run_id, agent_version_id)
        return RuntimeRequestContext(
            session=session,
            run_id=run_id,
            agent_version_id=agent_version_id,
            created_at=created_at,
            prompt=prompt,
            telemetry_input=telemetry_input,
        )

    def _runtime_observation_metadata(self, context: RuntimeRequestContext, mode: str) -> dict[str, Any]:
        return {
            "api_session_id": context.session.session_id,
            "run_id": context.run_id,
            "agent_version_id": context.agent_version_id,
            "mode": mode,
        }

    def _generation_input(self, req: ChatRequest, context: RuntimeRequestContext) -> dict[str, Any]:
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
    ) -> tuple[str, str, dict[str, Any], bool, list[str]]:
        text = extract_text(msg)
        plain = to_plain(msg)
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
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        answer = AgentJobRunner.dedupe_answer_parts(state.answer_parts)
        agent_activity = self.activity_extractor.agent_activity_payload(req, state.messages)
        output = self._runtime_output_payload(
            run_id=context.run_id,
            agent_version_id=context.agent_version_id,
            session=context.session,
            sdk_session_id=state.sdk_session_id,
            alert_id=req.alert_id,
            case_id=req.case_id,
            answer=answer,
            messages=state.messages,
            agent_activity=agent_activity,
            usage=state.usage,
            total_cost_usd=state.total_cost_usd,
            stop_reason=state.stop_reason,
            errors=state.errors,
        )
        return answer, agent_activity, output

    def _update_runtime_observations(self, root_span: Any, generation: Any, context: RuntimeRequestContext, state: RuntimeQueryState, output: dict[str, Any]) -> None:
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
        self.langfuse.set_trace_io(root_span, input=context.telemetry_input, output=output)

    def _complete_runtime_request(
        self,
        req: ChatRequest,
        context: RuntimeRequestContext,
        state: RuntimeQueryState,
        answer: str,
        agent_activity: dict[str, Any],
    ) -> None:
        if state.sdk_session_id:
            context.session.sdk_session_id = state.sdk_session_id
        context.session.turns += 1
        if not context.session.title:
            context.session.title = req.message[:80]
        self.session_store.save(context.session)
        self._record_feedback_run(
            run_id=context.run_id,
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

    def _run_response(self, context: RuntimeRequestContext, state: RuntimeQueryState, answer: str, agent_activity: dict[str, Any]) -> dict[str, Any]:
        return {
            "run_id": context.run_id,
            "agent_version_id": context.agent_version_id,
            "session_id": context.session.session_id,
            "sdk_session_id": context.session.sdk_session_id,
            "answer": answer,
            "messages": state.messages,
            "agent_activity": agent_activity,
            "usage": state.usage,
            "total_cost_usd": state.total_cost_usd,
            "stop_reason": state.stop_reason,
            "errors": state.errors,
        }

    async def run(self, req: ChatRequest) -> dict[str, Any]:
        from claude_agent_sdk import ResultMessage, query

        context = self._new_runtime_request_context(req)
        state = RuntimeQueryState(sdk_session_id=context.session.sdk_session_id)
        with self.langfuse.start_observation(
            as_type="span",
            name=self._main_observation_name(),
            input=context.telemetry_input,
            metadata=self._runtime_observation_metadata(context, "non_stream"),
        ) as root_span:
            context.langfuse_trace_id, context.langfuse_trace_url = self.langfuse.current_trace_ref()
            with self.langfuse.start_observation(
                as_type="generation",
                name=f"{self._main_observation_name()}.claude_sdk_query",
                input=self._generation_input(req, context),
                model=req.model or self.settings.agent_model,
            ) as generation:
                try:
                    options = self._build_options(req, context.session)
                    stream = AgentJobRunner.single_prompt_stream(context.prompt)
                    async for msg in query(prompt=stream, options=options):
                        self._track_query_message(msg, state, ResultMessage)
                except Exception as exc:
                    if not self._should_suppress_exception(exc, state.errors):
                        state.errors.append(f"{exc.__class__.__name__}: {exc}")

                answer, agent_activity, output = self._runtime_output_from_state(req, context, state)
                self._update_runtime_observations(root_span, generation, context, state, output)
        self._flush_langfuse()
        self._complete_runtime_request(req, context, state, answer, agent_activity)
        return self._run_response(context, state, answer, agent_activity)

    async def stream(self, req: ChatRequest) -> AsyncIterator[dict[str, Any]]:
        from claude_agent_sdk import ResultMessage, query

        context = self._new_runtime_request_context(req)
        state = RuntimeQueryState(sdk_session_id=context.session.sdk_session_id)
        with self.langfuse.start_observation(
            as_type="span",
            name=self._main_observation_name(),
            input=context.telemetry_input,
            metadata=self._runtime_observation_metadata(context, "stream"),
        ) as root_span:
            context.langfuse_trace_id, context.langfuse_trace_url = self.langfuse.current_trace_ref()
            yield {
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
            with self.langfuse.start_observation(
                as_type="generation",
                name=f"{self._main_observation_name()}.claude_sdk_query",
                input=self._generation_input(req, context),
                model=req.model or self.settings.agent_model,
            ) as generation:
                try:
                    options = self._build_options(req, context.session)
                    stream = AgentJobRunner.single_prompt_stream(context.prompt)
                    async for msg in query(prompt=stream, options=options):
                        event, text, plain, is_result, result_errors = self._track_query_message(msg, state, ResultMessage)
                        yield {"event": "message", "data": {"event": event, "text": text, "raw": plain}}
                        if is_result:
                            agent_activity = self.activity_extractor.agent_activity_payload(req, state.messages)
                            yield {
                                "event": "result",
                                "data": {
                                    "session_id": context.session.session_id,
                                    "sdk_session_id": state.sdk_session_id,
                                    "run_id": context.run_id,
                                    "agent_version_id": context.agent_version_id,
                                    "alert_id": req.alert_id,
                                    "case_id": req.case_id,
                                    "agent_activity": agent_activity,
                                    "usage": state.usage,
                                    "total_cost_usd": state.total_cost_usd,
                                    "stop_reason": state.stop_reason,
                                    "errors": result_errors,
                                },
                            }
                except Exception as exc:
                    if not self._should_suppress_exception(exc, state.errors):
                        state.errors.append(f"{exc.__class__.__name__}: {exc}")
                        yield {"event": "error", "data": {"errors": state.errors}}
                finally:
                    answer, agent_activity, output = self._runtime_output_from_state(req, context, state)
                    self._complete_runtime_request(req, context, state, answer, agent_activity)
                    self._update_runtime_observations(root_span, generation, context, state, output)
                    yield {"event": "done", "data": "[DONE]"}
        self._flush_langfuse()
