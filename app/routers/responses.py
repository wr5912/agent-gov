from __future__ import annotations

import time
from collections.abc import Callable
from typing import NamedTuple, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from app.runtime.agent_profile_resolver import resolve_business_profile
from app.runtime.agent_profiles import AgentRuntimeProfile
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
from app.runtime.schemas import ChatRequest
from app.runtime.settings import AppSettings
from app.runtime.stores.agent_registry_store import AgentRegistryStore
from app.runtime.stores.feedback_store import FeedbackStore
from app.runtime.stores.runtime_settings_store import RuntimeSettingsStore

_MAIN_AGENT_DISPLAY = "main-agent"


class _RunPlan(NamedTuple):
    chat_req: ChatRequest
    profile: Optional[AgentRuntimeProfile]
    effective_agent_id: str
    control: bool
    sdk_raw: bool


def _resolve_session_id(req: ResponsesRequest, *, feedback_store: FeedbackStore) -> Optional[str]:
    """由 ``conversation`` / ``previous_response_id`` 解析服务端 session_id（权威续接源）。"""
    conv_session = session_id_from_conversation(req.conversation)
    if not req.previous_response_id:
        return conv_session
    prev_run_id = run_id_from_response(req.previous_response_id)
    prev = feedback_store.find_run(run_id=prev_run_id) if prev_run_id else None
    if not prev:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"previous_response_id {req.previous_response_id} not found",
        )
    prev_session = prev.get("session_id")
    prev_session = prev_session if isinstance(prev_session, str) else None
    if conv_session and prev_session and conv_session != prev_session:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="conversation is inconsistent with previous_response_id",
        )
    return prev_session or conv_session


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
) -> _RunPlan:
    session_id = _resolve_session_id(req, feedback_store=feedback_store)
    control = req.agentgov is not None
    profile, effective_agent_id, system_append = _resolve_run_target(
        req,
        settings=settings,
        agent_registry_store=agent_registry_store,
        runtime_settings_store=runtime_settings_store,
    )
    chat_req = build_chat_request(
        req,
        agent_id=effective_agent_id if control else None,
        system_append=system_append,
        session_id=session_id,
    )
    sdk_raw = bool(control and req.agentgov and req.agentgov.debug and req.agentgov.debug.sdk_raw)
    return _RunPlan(chat_req, profile, effective_agent_id, control, sdk_raw)


async def _create_response_impl(
    req: ResponsesRequest,
    *,
    settings: AppSettings,
    runtime: ClaudeRuntime,
    agent_registry_store: AgentRegistryStore,
    runtime_settings_store: RuntimeSettingsStore,
    feedback_store: FeedbackStore,
) -> ResponseObject | StreamingResponse:
    plan = _prepare_run(
        req,
        settings=settings,
        agent_registry_store=agent_registry_store,
        runtime_settings_store=runtime_settings_store,
        feedback_store=feedback_store,
    )
    if req.stream:
        return StreamingResponse(
            iter_responses_sse(
                runtime.stream(plan.chat_req, profile=plan.profile),
                model=req.model,
                effective_agent_id=plan.effective_agent_id,
                control=plan.control,
                sdk_raw=plan.sdk_raw,
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
) -> APIRouter:
    """OpenAI Responses-first canonical 接口（薄路由，逻辑在 ``openai_responses_adapter`` 与本模块 impl）。"""

    router = APIRouter(prefix="/v1", tags=["openai-responses"], dependencies=[Depends(require_api_key)])

    @router.post(
        "/responses",
        response_model=ResponseObject,
        response_model_exclude_none=True,
        summary="Run an AgentGov business agent (OpenAI Responses-compatible)",
        description=(
            "Canonical run endpoint. No `agentgov` = strict (operator-configured agent, pure OpenAI shape). "
            "`agentgov` present = control (requires `agentgov.agent_id`). `stream=true` returns Responses-style SSE "
            "(`response.*`; plus `agentgov.*` control envelope in control mode)."
        ),
    )
    async def create_response(req: ResponsesRequest) -> ResponseObject | StreamingResponse:
        return await _create_response_impl(
            req,
            settings=settings,
            runtime=runtime,
            agent_registry_store=agent_registry_store,
            runtime_settings_store=runtime_settings_store,
            feedback_store=feedback_store,
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
    async def retrieve_response(response_id: str) -> ResponseObject:
        return _retrieve_response_impl(response_id, feedback_store=feedback_store)

    return router
