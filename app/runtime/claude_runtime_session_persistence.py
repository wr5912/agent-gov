from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, Optional

from claude_agent_sdk import project_key_for_directory

from .agent_profiles import MAIN_AGENT_PROFILE, AgentRuntimeProfile
from .json_types import JsonObject
from .records.source_records import AgentRunRecord
from .runtime_db import utc_now
from .sdk_session_migration import ensure_sdk_store_ready
from .sdk_session_store import SqliteSdkSessionStore

if TYPE_CHECKING:
    from .claude_runtime import RuntimeQueryState, RuntimeRequestContext
    from .schemas import ChatRequest

_PERSISTENCE_FINALIZATION_ATTEMPTS = 3


class RuntimeSessionPersistenceMixin:
    settings: Any
    session_store: Any
    profiles: Any

    async def _new_runtime_request_context(
        self,
        req: ChatRequest,
        *,
        profile: AgentRuntimeProfile,
        agent_version_id_override: Optional[str] = None,
        agent_id: str = MAIN_AGENT_PROFILE,
    ) -> RuntimeRequestContext:
        from .claude_runtime import RuntimeRequestContext

        self._raise_if_version_maintenance(agent_id)
        session = self.session_store.get_or_create_owned(req.session_id, agent_id=agent_id, metadata=req.metadata)
        if session.sdk_session_id:
            session = await ensure_sdk_store_ready(
                self.session_store,
                session,
                workspace_dir=profile.workspace_dir,
                claude_config_dir=profile.claude_config_dir,
            )

        run_id = str(uuid.uuid4())
        attempted_sdk_session_id = session.sdk_session_id or str(uuid.uuid4())
        sdk_project_key = project_key_for_directory(str(profile.workspace_dir))
        agent_version_id = agent_version_id_override if agent_version_id_override is not None else self._current_agent_version_id(agent_id)
        created_at = utc_now()
        prompt = self._build_prompt(req)
        intent_request: JsonObject = {
            "agent_version_id": agent_version_id,
            "alert_id": req.alert_id,
            "case_id": req.case_id,
            "agent_id": agent_id,
            "metadata": req.metadata,
        }
        session = self.session_store.begin_persisted_turn(
            session,
            run_id=run_id,
            agent_id=agent_id,
            attempted_sdk_session_id=attempted_sdk_session_id,
            sdk_project_key=sdk_project_key,
            request=intent_request,
            created_at=created_at,
        )
        telemetry_input = self._request_telemetry_input(req, prompt, session, run_id, agent_version_id)
        return RuntimeRequestContext(
            session=session,
            run_id=run_id,
            run_generation=session.active_run_generation,
            attempted_sdk_session_id=attempted_sdk_session_id,
            sdk_project_key=sdk_project_key,
            sdk_session_store=SqliteSdkSessionStore.for_turn(
                self.session_store.Session,
                project_key=sdk_project_key,
                sdk_session_id=attempted_sdk_session_id,
                run_id=run_id,
            ),
            agent_version_id=agent_version_id,
            agent_id=agent_id,
            created_at=created_at,
            prompt=prompt,
            telemetry_input=telemetry_input,
        )

    def _complete_runtime_request(
        self,
        req: ChatRequest,
        context: RuntimeRequestContext,
        state: RuntimeQueryState,
        answer: str,
        agent_activity: JsonObject,
    ) -> None:
        if context.finalized:
            return
        if not state.result_observed:
            raise ValueError("SDK turn cannot commit without a ResultMessage")
        if state.mirror_errors:
            raise ValueError("SDK turn cannot commit after a SessionStore mirror failure")
        if state.sdk_session_id != context.attempted_sdk_session_id:
            raise ValueError("SDK ResultMessage returned an unexpected session id")
        completed_at = utc_now()
        run_record = self._runtime_run_record(
            req=req,
            context=context,
            state=state,
            answer=answer,
            agent_activity=agent_activity,
            completed_at=completed_at,
        )
        context.finalization_attempted = True
        for attempt in range(_PERSISTENCE_FINALIZATION_ATTEMPTS):
            try:
                context.session = self.session_store.finalize_persisted_turn(
                    session_id=context.session.session_id,
                    run_id=context.run_id,
                    run_generation=context.run_generation,
                    sdk_session_id=context.attempted_sdk_session_id,
                    title=req.message[:80],
                    run_record=run_record,
                    terminal_status="failed" if state.result_is_error else "succeeded",
                    completed_at=completed_at,
                )
                break
            except Exception:
                if attempt + 1 == _PERSISTENCE_FINALIZATION_ATTEMPTS:
                    raise
        context.finalized = True

    def _abort_runtime_request(
        self,
        req: ChatRequest,
        context: RuntimeRequestContext,
        state: RuntimeQueryState,
        *,
        terminal_status: str,
        error: BaseException | str,
    ) -> None:
        if context.finalized or context.finalization_attempted:
            return
        completed_at = utc_now()
        answer, agent_activity, _ = self._runtime_output_from_state(req, context, state)
        error_text = str(error)
        error_type = error.__class__.__name__ if isinstance(error, BaseException) else terminal_status
        if error_text and not state.errors:
            state.errors.append(f"{error_type}: {error_text}")
        run_record = self._runtime_run_record(
            req=req,
            context=context,
            state=state,
            answer=answer,
            agent_activity=agent_activity,
            completed_at=completed_at,
        )
        context.finalization_attempted = True
        for attempt in range(_PERSISTENCE_FINALIZATION_ATTEMPTS):
            try:
                context.session = self.session_store.abort_persisted_turn(
                    session_id=context.session.session_id,
                    run_id=context.run_id,
                    run_generation=context.run_generation,
                    run_record=run_record,
                    terminal_status=terminal_status,
                    error={"type": error_type, "message": error_text},
                    completed_at=completed_at,
                )
                break
            except Exception:
                if attempt + 1 == _PERSISTENCE_FINALIZATION_ATTEMPTS:
                    raise
        context.finalized = True

    def _runtime_run_record(
        self,
        *,
        req: ChatRequest,
        context: RuntimeRequestContext,
        state: RuntimeQueryState,
        answer: str,
        agent_activity: JsonObject,
        completed_at: str,
    ) -> AgentRunRecord:
        prepared = self._feedback_run_record(
            run_id=context.run_id,
            agent_id=context.agent_id,
            agent_version_id=context.agent_version_id,
            session=context.session,
            sdk_session_id=state.sdk_session_id or context.attempted_sdk_session_id,
            req=req,
            answer=answer,
            messages=state.messages,
            agent_activity=agent_activity,
            usage=state.usage,
            total_cost_usd=state.total_cost_usd,
            stop_reason=state.stop_reason,
            errors=state.errors,
            created_at=context.created_at,
            completed_at=completed_at,
            langfuse_trace_id=context.langfuse_trace_id,
            langfuse_trace_url=context.langfuse_trace_url,
        )
        if prepared is not None:
            return prepared
        return AgentRunRecord.from_payload(
            {
                "run_id": context.run_id,
                "agent_id": context.agent_id,
                "agent_version_id": context.agent_version_id,
                "session_id": context.session.session_id,
                "sdk_session_id": state.sdk_session_id or context.attempted_sdk_session_id,
                "alert_id": req.alert_id,
                "case_id": req.case_id,
                "message": req.message,
                "answer_summary": answer.strip().replace("\n", " ")[:500],
                "messages": state.messages,
                "agent_activity": agent_activity,
                "errors": state.errors,
                "metadata": req.metadata,
                "created_at": context.created_at,
                "completed_at": completed_at,
            }
        )
