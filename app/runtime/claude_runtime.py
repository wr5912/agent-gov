from __future__ import annotations

import os
import uuid
from contextlib import nullcontext
from collections.abc import AsyncIterator
from typing import Any, Optional

from .agent_profiles import (
    MAIN_AGENT_PROFILE,
    AgentRuntimeProfile,
    build_profiles,
)
from .agent_job_runner import AgentJobRunner
from .agent_loader import load_programmatic_agents
from .agent_version_store import AgentVersionStore
from .feedback_eval_runner import FeedbackEvalRunner
from .feedback_job_orchestrator import FeedbackJobOrchestrator
from .feedback_store import FeedbackStore, utc_now
from .message_utils import extract_text, message_event_name, to_plain
from .output_formatter import DSPyOutputFormatter
from .policy import build_default_hooks, guard_tool_use
from .runtime_activity import RuntimeActivityExtractor
from .runtime_langfuse import RuntimeLangfuseClient, ensure_langfuse_otel_compat
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
                provider_configured=lambda: self._provider_configured(),
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

    async def _single_prompt_stream(self, prompt: str) -> AsyncIterator[dict[str, Any]]:
        async for item in AgentJobRunner.single_prompt_stream(prompt):
            yield item

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
        return AgentJobRunner.result_errors(msg)

    def _should_suppress_exception(self, exc: Exception, errors: list[str]) -> bool:
        if not errors:
            return False
        text = str(exc)
        return text.startswith("Claude Code returned an error result:")

    def _dedupe_answer_parts(self, parts: list[str]) -> str:
        return AgentJobRunner.dedupe_answer_parts(parts)

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
        return self.activity_extractor.agent_activity_payload(req, messages)

    def _usage_details(self, usage: Any) -> Optional[dict[str, int]]:
        return self.activity_extractor.usage_details(usage)

    def _cost_details(self, total_cost_usd: Optional[float]) -> Optional[dict[str, float]]:
        return self.activity_extractor.cost_details(total_cost_usd)

    def _get_langfuse_client(self) -> Any | None:
        return self.langfuse.get_client()

    def _current_langfuse_trace_ref(self) -> tuple[Optional[str], Optional[str]]:
        return self.langfuse.current_trace_ref()

    def fetch_langfuse_trace(self, trace_id: str) -> Optional[dict[str, Any]]:
        return self.langfuse.fetch_trace(trace_id)

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
        self.langfuse.update_observation(observation, **kwargs)

    def _set_langfuse_trace_io(self, observation: Any, *, input: Any, output: Any) -> None:
        self.langfuse.set_trace_io(observation, input=input, output=output)

    def _flush_langfuse(self) -> None:
        client = self._get_langfuse_client()
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

        profile = self.profiles[MAIN_AGENT_PROFILE]
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
        return self.job_runner.build_options(profile)

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
        self.job_runner.output_formatter = self.output_formatter
        return await self.job_runner.run_profile_json(
            profile_name=profile_name,
            prompt=prompt,
            expected_schema_version=expected_schema_version,
            job_type=job_type,
            job_input=job_input,
        )

    def _direct_schema_candidate(self, raw_text: str, expected_schema_version: str) -> dict[str, Any] | None:
        return AgentJobRunner.direct_schema_candidate(raw_text, expected_schema_version)

    async def _format_agent_text(
        self,
        *,
        job_type: str,
        raw_text: str,
        job_input: dict[str, Any],
        expected_schema_version: str,
    ) -> dict[str, Any] | None:
        self.job_runner.output_formatter = self.output_formatter
        return await self.job_runner.format_agent_text(
            job_type=job_type,
            raw_text=raw_text,
            job_input=job_input,
            expected_schema_version=expected_schema_version,
        )

    def _raw_agent_text_payload(self, raw_text: str, expected_schema_version: str) -> dict[str, Any]:
        return AgentJobRunner.raw_agent_text_payload(raw_text, expected_schema_version)

    async def run_attribution_job(self, feedback_case_id: str, *, force: bool = False) -> dict[str, Any] | None:
        if self.job_orchestrator is None:
            return None
        return await self.job_orchestrator.run_attribution_job(feedback_case_id, force=force)

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

    async def run_execution_job(self, optimization_task_id: str, *, force: bool = False) -> dict[str, Any] | None:
        if self.job_orchestrator is None:
            return None
        return await self.job_orchestrator.run_execution_job(optimization_task_id, force=force)

    async def run_feedback_eval(
        self,
        *,
        eval_case_ids: Optional[list[str]] = None,
        optimization_task_id: Optional[str] = None,
        source: str = "manual_feedback_dataset",
    ) -> dict[str, Any] | None:
        if self.eval_runner is None:
            return None
        return await self.eval_runner.run_feedback_eval(
            eval_case_ids=eval_case_ids,
            optimization_task_id=optimization_task_id,
            source=source,
        )

    def _selected_eval_cases(self, eval_case_ids: Optional[list[str]]) -> list[dict[str, Any]]:
        if self.eval_runner is None:
            return []
        return self.eval_runner._selected_eval_cases(eval_case_ids)

    def _evaluate_eval_case(self, eval_case: dict[str, Any], result: dict[str, Any]) -> tuple[str, float, list[dict[str, Any]]]:
        if self.eval_runner is None:
            return "failed", 0.0, []
        return self.eval_runner._evaluate_eval_case(eval_case, result)

    def _eval_tool_names(self, activity: dict[str, Any]) -> list[str]:
        return FeedbackEvalRunner._eval_tool_names(activity)

    def _build_langfuse_env(self) -> dict[str, str]:
        return self.langfuse.build_env()

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
