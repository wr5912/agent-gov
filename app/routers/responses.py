from __future__ import annotations

import time
from collections.abc import Callable
from typing import NamedTuple, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from app.runtime.agent_profile_resolver import resolve_business_profile
from app.runtime.agent_profiles import AgentRuntimeProfile
from app.runtime.api_auth import ApiAuthenticator, ApiPrincipal
from app.runtime.claude_runtime import ClaudeRuntime
from app.runtime.errors import BusinessRuleViolation, NotFoundError
from app.runtime.json_types import JsonObject
from app.runtime.openai_responses_adapter import (
    build_chat_request,
    public_metadata,
    response_from_chat_response,
    response_from_run_payload,
    run_id_from_response,
    session_id_from_conversation,
    store_disabled,
)
from app.runtime.openai_responses_schemas import ResponseObject, ResponsesRequest
from app.runtime.openai_responses_stream import iter_responses_sse
from app.runtime.response_disposition_control import (
    ResponseDispositionControlError,
    TrustedResponseDispositionContext,
    validate_response_disposition_control,
)
from app.runtime.response_disposition_stream import observe_response_disposition_stream
from app.runtime.schemas import RuntimeChatRequest
from app.runtime.session_store import LocalSessionStore
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_registry_store import AgentRegistryStore
from app.runtime.stores.feedback_store import FeedbackStore
from app.runtime.stores.response_disposition_claim_store import (
    ResponseDispositionClaimConflict,
    ResponseDispositionClaimStore,
)
from app.runtime.stores.runtime_settings_store import RuntimeSettingsStore

_MAIN_AGENT_DISPLAY = "main-agent"


class _RunPlan(NamedTuple):
    chat_req: RuntimeChatRequest
    profile: Optional[AgentRuntimeProfile]
    effective_agent_id: str
    control: bool
    sdk_raw: bool
    response_disposition: TrustedResponseDispositionContext | None


def _resolve_session_id(
    req: ResponsesRequest,
    *,
    feedback_store: FeedbackStore,
    session_store: LocalSessionStore,
    effective_agent_id: str,
) -> Optional[str]:
    """由 ``conversation`` / ``previous_response_id`` 解析服务端 session_id（权威续接源）。"""
    conv_session = session_id_from_conversation(req.conversation)
    prev_session: Optional[str] = None
    if req.previous_response_id:
        prev_run_id = run_id_from_response(req.previous_response_id)
        prev = feedback_store.find_run(run_id=prev_run_id) if prev_run_id else None
        if not prev:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"previous_response_id {req.previous_response_id} not found",
            )
        raw_prev_session = prev.get("session_id")
        prev_session = raw_prev_session if isinstance(raw_prev_session, str) else None
        prev_agent_id = prev.get("agent_id")
        if not isinstance(prev_agent_id, str) or prev_agent_id != effective_agent_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="previous_response_id belongs to a different business agent",
            )
        if not prev_session:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="previous_response_id has no resumable conversation",
            )
        if conv_session and prev_session and conv_session != prev_session:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="conversation is inconsistent with previous_response_id",
            )
    resolved_session = prev_session or conv_session
    existing = session_store.get(resolved_session) if resolved_session else None
    if req.previous_response_id and existing is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="previous_response_id conversation mapping no longer exists",
        )
    if existing and existing.agent_id is None and (existing.turns > 0 or existing.sdk_session_id is not None):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="conversation has no unambiguous business agent owner",
        )
    if existing and existing.agent_id and existing.agent_id != effective_agent_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="conversation belongs to a different business agent",
        )
    return resolved_session


def _resolve_run_target(
    req: ResponsesRequest,
    *,
    settings: AppSettings,
    agent_registry_store: AgentRegistryStore,
    runtime_settings_store: RuntimeSettingsStore,
):
    """解析运行目标：返回 (profile, effective_agent_id, system_append)。

    control（有 agentgov）：agent_id 必填缺失 -> 422；instructions 按 append-only。
    strict（无 agentgov）：运营者配置出口 Agent（失效 503）；instructions 拒绝（不静默 append）。
    """
    if req.agentgov is not None:
        agent_id = (req.agentgov.agent_id or "").strip()
        if not agent_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="agentgov.agent_id is required in control mode (main-agent or a registered business agent)",
            )
        profile = resolve_business_profile(settings, agent_registry_store, agent_id)
        return profile, agent_id, req.instructions

    if req.instructions is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "instructions is not supported on the strict /v1/responses surface: AgentGov instructions is "
                "append-only (Claude Code preset + workspace CLAUDE.md single source of truth), not OpenAI "
                "replace/swap. Send it via a control request (with agentgov) or omit it."
            ),
        )
    configured_agent_id = runtime_settings_store.get_openai_compat_agent_id()
    try:
        profile = resolve_business_profile(settings, agent_registry_store, configured_agent_id)
    except (NotFoundError, BusinessRuleViolation) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Configured OpenAI-compat agent is unavailable: {exc}. Reconfigure via /api/settings/openai-compat-agent.",
        ) from exc
    return profile, configured_agent_id or _MAIN_AGENT_DISPLAY, None


def _prepare_run(
    req: ResponsesRequest,
    *,
    settings: AppSettings,
    agent_registry_store: AgentRegistryStore,
    runtime_settings_store: RuntimeSettingsStore,
    feedback_store: FeedbackStore,
    session_store: LocalSessionStore,
    principal: ApiPrincipal,
    authenticator: ApiAuthenticator,
    claim_store: ResponseDispositionClaimStore,
    web_hitl_available: bool,
) -> _RunPlan:
    ext = req.agentgov
    response_disposition_requested = bool(
        ext and any(value is not None for value in (ext.phase, ext.approval_request_id, ext.playbook_digest, ext.execution_run_id))
    )
    if response_disposition_requested:
        authenticator.require_response_orchestrator(principal)
    elif principal == ApiPrincipal.RESPONSE_ORCHESTRATOR:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Response orchestrator credential may only create response-disposition runs",
        )
    control = req.agentgov is not None
    profile, effective_agent_id, system_append = _resolve_run_target(
        req,
        settings=settings,
        agent_registry_store=agent_registry_store,
        runtime_settings_store=runtime_settings_store,
    )
    session_id = _resolve_session_id(
        req,
        feedback_store=feedback_store,
        session_store=session_store,
        effective_agent_id=effective_agent_id,
    )
    try:
        response_disposition = validate_response_disposition_control(
            phase=ext.phase if ext else None,
            agent_id=effective_agent_id,
            stream=req.stream,
            web_hitl_available=web_hitl_available,
            case_id=ext.case_id if ext else None,
            approval_request_id=ext.approval_request_id if ext else None,
            playbook_digest=ext.playbook_digest if ext else None,
            execution_run_id=ext.execution_run_id if ext else None,
        )
    except ResponseDispositionControlError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    if response_disposition and response_disposition.phase == "approved_execution":
        try:
            claim_store.claim(response_disposition)
        except ResponseDispositionClaimConflict as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    chat_req = build_chat_request(
        req,
        agent_id=effective_agent_id if control else None,
        system_append=system_append,
        session_id=session_id,
        response_disposition=response_disposition,
    )
    sdk_raw = bool(control and req.agentgov and req.agentgov.debug and req.agentgov.debug.sdk_raw)
    return _RunPlan(chat_req, profile, effective_agent_id, control, sdk_raw, response_disposition)


async def _create_response_impl(
    req: ResponsesRequest,
    *,
    settings: AppSettings,
    runtime: ClaudeRuntime,
    agent_registry_store: AgentRegistryStore,
    runtime_settings_store: RuntimeSettingsStore,
    feedback_store: FeedbackStore,
    principal: ApiPrincipal,
    authenticator: ApiAuthenticator,
    claim_store: ResponseDispositionClaimStore,
) -> ResponseObject | StreamingResponse:
    plan = _prepare_run(
        req,
        settings=settings,
        agent_registry_store=agent_registry_store,
        runtime_settings_store=runtime_settings_store,
        feedback_store=feedback_store,
        session_store=runtime.session_store,
        principal=principal,
        authenticator=authenticator,
        claim_store=claim_store,
        web_hitl_available=bool(settings.enable_claude_web_hitl and runtime.user_input_service is not None),
    )
    if req.stream:
        source = runtime.stream(plan.chat_req, profile=plan.profile)
        if plan.response_disposition and plan.response_disposition.phase == "approved_execution":
            source = observe_response_disposition_stream(
                source,
                context=plan.response_disposition,
                claim_store=claim_store,
            )
        return StreamingResponse(
            iter_responses_sse(
                source,
                model=req.model,
                effective_agent_id=plan.effective_agent_id,
                control=plan.control,
                sdk_raw=plan.sdk_raw,
                response_disposition=plan.response_disposition,
            ),
            media_type="text/event-stream",
        )
    result = await runtime.run(plan.chat_req, profile=plan.profile)
    return response_from_chat_response(
        result,
        model=req.model,
        agent_id=plan.effective_agent_id,
        metadata=public_metadata(req.metadata),
        created_at=int(time.time()),
        response_disposition=plan.response_disposition,
    )


def _retrieve_response_impl(response_id: str, *, feedback_store: FeedbackStore) -> ResponseObject:
    run_id = run_id_from_response(response_id)
    run: Optional[JsonObject] = feedback_store.find_run(run_id=run_id) if run_id else None
    if not run or store_disabled(run):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Response {response_id} not found")
    return response_from_run_payload(run)


def create_responses_router(
    *,
    settings: AppSettings,
    runtime: ClaudeRuntime,
    agent_registry_store: AgentRegistryStore,
    runtime_settings_store: RuntimeSettingsStore,
    feedback_store: FeedbackStore,
    require_api_key: Callable,
    authenticate_api_or_ro: Callable,
    authenticator: ApiAuthenticator,
    claim_store: ResponseDispositionClaimStore,
) -> APIRouter:
    """OpenAI Responses-first canonical 接口（薄路由，逻辑在 ``openai_responses_adapter`` 与本模块 impl）。"""

    router = APIRouter(prefix="/v1", tags=["openai-responses"])

    @router.post(
        "/responses",
        response_model=ResponseObject,
        response_model_exclude_none=True,
        summary="Run an AgentGov business agent (OpenAI Responses-compatible)",
        description=(
            "Canonical run endpoint. No `agentgov` = strict (operator-configured agent, pure OpenAI shape). "
            "`agentgov` present = control (requires `agentgov.agent_id`). `stream=true` returns Responses-style SSE "
            "(`response.*`; plus `agentgov.*` control envelope, including optional `agentgov.prompt_suggestion`, in control mode)."
        ),
    )
    async def create_response(
        req: ResponsesRequest,
        principal: ApiPrincipal = Depends(authenticate_api_or_ro),  # noqa: B008 - FastAPI dependency factory
    ) -> ResponseObject | StreamingResponse:
        return await _create_response_impl(
            req,
            settings=settings,
            runtime=runtime,
            agent_registry_store=agent_registry_store,
            runtime_settings_store=runtime_settings_store,
            feedback_store=feedback_store,
            principal=principal,
            authenticator=authenticator,
            claim_store=claim_store,
        )

    @router.get(
        "/responses/{response_id}",
        response_model=ResponseObject,
        response_model_exclude_none=True,
        summary="Retrieve a stored response (reconstructed from the agent run)",
        description=(
            "Rebuilds the response from the persisted agent run (resp_<run_id>). Minimal retrieve: completed runs only; "
            "status derived from errors/stop_reason; output_text from the message timeline. store=false -> 404 (internal audit stays)."
        ),
    )
    async def retrieve_response(response_id: str, _: None = Depends(require_api_key)) -> ResponseObject:
        return _retrieve_response_impl(response_id, feedback_store=feedback_store)

    return router
