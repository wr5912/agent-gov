from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated

import httpx
from agentgov_agentscope_contract import is_runtime_template_restart_response
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from app.runtime.json_types import JsonObject
from app.version import APP_VERSION

from ._router_operations import (
    RUN_ID_HEADER,
    SESSION_ID_HEADER,
    _audit_error,
    _call,
    _cancel_response,
    _compensate_session_creation,
    _json_upstream,
)
from ._router_recovery import (
    RuntimeGatewayRecoveryReport as RuntimeGatewayRecoveryReport,
)
from ._router_recovery import (
    reconcile_runtime_gateway as reconcile_runtime_gateway,
)
from .client import (
    AgentScopeRuntimeClient,
    RuntimeUpstreamError,
    canonical_session_id_from_view,
    copy_response_headers,
)
from .contracts import (
    TERMINAL_RUN_STATUSES,
    AgentRunResponse,
    AgentRunTraceResponse,
    RuntimeBootAck,
    RuntimeBootAnnouncement,
    RuntimeChatRequest,
    RuntimeChildSessionRegistration,
    RuntimeContextResponse,
    RuntimeCurrentVersionResponse,
    RuntimePendingActionResponse,
    RuntimeReceipt,
    RuntimeSessionCreateRequest,
    RuntimeTeamInboxAck,
    RuntimeTeamInboxDelivery,
)
from .models import RuntimeSessionCreationIntentModel
from .native_schema import register_native_agent_schema_route
from .provisioning import RuntimeAgentProvisioner
from .run_trigger import admit_and_trigger_chat, interrupt_active_run
from .security import verify_internal_request
from .session_resources import register_session_resource_routes
from .store import (
    RuntimeAuthenticationError,
    RuntimeObjectNotFound,
    RuntimeRestartRequired,
    RuntimeRunStore,
    RuntimeStateConflict,
    RuntimeStoreError,
    SessionCreationStatus,
)
from .trace_validation import trace_has_complete_governed_run

_MAX_INTERNAL_BODY_BYTES = 1024 * 1024
_RUNTIME_SSE_READINESS_COMMENT = b":\n\n"


@dataclass(frozen=True)
class RuntimeSessionUpstreamConfig:
    model_type: str
    credential_id: str
    model_name: str
    model_parameters: JsonObject

    def request_body(self, *, runtime_agent_id: str, workspace_id: str, name: str | None) -> JsonObject:
        return {
            "agent_id": runtime_agent_id,
            "workspace_id": workspace_id,
            "name": name,
            "chat_model_config": {
                "type": self.model_type,
                "credential_id": self.credential_id,
                "model": self.model_name,
                "parameters": dict(self.model_parameters),
            },
        }


@dataclass
class _LockEntry:
    lock: asyncio.Lock
    users: int = 0


class _IdempotencyLocks:
    """同一进程内让并发重放拿到首个请求的最终结果。"""

    def __init__(self) -> None:
        self._guard = asyncio.Lock()
        self._entries: dict[str, _LockEntry] = {}

    @asynccontextmanager
    async def hold(self, key: str | None) -> AsyncIterator[None]:
        if key is None:
            yield
            return
        async with self._guard:
            entry = self._entries.setdefault(key, _LockEntry(asyncio.Lock()))
            entry.users += 1
        acquired = False
        try:
            await entry.lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                entry.lock.release()
            async with self._guard:
                entry.users -= 1
                if entry.users == 0:
                    self._entries.pop(key, None)


class _SessionCreator:
    def __init__(
        self,
        *,
        client: AgentScopeRuntimeClient,
        store: RuntimeRunStore,
        provisioner: RuntimeAgentProvisioner,
        config: RuntimeSessionUpstreamConfig,
    ) -> None:
        self.client = client
        self.store = store
        self.provisioner = provisioner
        self.config = config
        self.locks = _IdempotencyLocks()

    async def create(self, payload: RuntimeSessionCreateRequest, key: str | None) -> Response:
        async with self.locks.hold(key):
            if key:
                existing = self.store.session_creation_for_request(
                    key=key,
                    runtime_agent_id=payload.agent_id,
                    requested_name=payload.name,
                )
                if existing is not None:
                    return _intent_replay_response(existing, payload.agent_id)
            binding = self.provisioner.require_current_runtime(payload.agent_id)
            intent, owned = self.store.start_session_creation(
                idempotency_key=key,
                agent_id=binding.agent_id,
                agent_version_id=binding.agent_version_id,
                runtime_agent_id=binding.runtime_agent_id,
                digest=binding.harness_digest,
                workspace_id=binding.workspace_id,
                requested_name=payload.name,
            )
            if not owned:
                return _intent_replay_response(intent, payload.agent_id)
            return await self._execute(
                intent.intent_id,
                binding.runtime_agent_id,
                intent.workspace_id,
                payload.name,
                binding.permission_mode,
                binding.cwd,
            )

    async def _execute(
        self,
        intent_id: str,
        runtime_agent_id: str,
        workspace_id: str,
        name: str | None,
        permission_mode: str,
        cwd: str,
    ) -> Response:
        try:
            upstream = await _call(
                self.client,
                "POST",
                "/sessions/",
                json=self.config.request_body(runtime_agent_id=runtime_agent_id, workspace_id=workspace_id, name=name),
            )
        except RuntimeUpstreamError as exc:
            if is_runtime_template_restart_response(exc.status_code, exc.body):
                self.store.mark_session_creation(
                    intent_id,
                    status=SessionCreationStatus.FAILED_CLEANED,
                    error=_audit_error("runtime_restart_required", exc),
                    cleanup_attempt=False,
                )
                raise RuntimeRestartRequired(
                    "Published subagent templates are prepared; restart AgentScope Runtime and retry Session creation",
                ) from exc
            self.store.mark_session_creation(
                intent_id,
                status=SessionCreationStatus.PENDING,
                error=_audit_error("upstream_create_uncertain", exc),
            )
            raise
        except Exception as exc:
            self.store.mark_session_creation(
                intent_id,
                status=SessionCreationStatus.PENDING,
                error=_audit_error("upstream_create_uncertain", exc),
            )
            raise
        session_id = upstream.body.get("session_id") if isinstance(upstream.body, dict) else None
        if not isinstance(session_id, str) or not session_id:
            error = RuntimeUpstreamError(502, b'{"detail":"Runtime did not return session_id"}')
            self.store.mark_session_creation(
                intent_id,
                status=SessionCreationStatus.PENDING,
                error=_audit_error("upstream_response_uncertain", error),
            )
            raise error
        try:
            self.store.record_session_creation_upstream(intent_id, session_id)
            await _call(
                self.client,
                "PATCH",
                f"/sessions/{session_id}",
                params={"agent_id": runtime_agent_id},
                json={"permission_mode": permission_mode, "cwd": cwd},
            )
            persisted = self.store.complete_session_creation(intent_id)
        except Exception as exc:
            await _compensate_session_creation(self.client, self.store, intent_id, session_id, runtime_agent_id, exc)
            raise
        return JSONResponse(
            {"session_id": persisted.session_id},
            status_code=upstream.status_code,
            headers={**copy_response_headers(upstream.headers), SESSION_ID_HEADER: persisted.session_id},
        )


def create_runtime_router(
    *,
    client: AgentScopeRuntimeClient,
    store: RuntimeRunStore,
    provisioner: RuntimeAgentProvisioner,
    model_type: str,
    credential_id: str,
    model_name: str,
    model_parameters: JsonObject,
    require_api_key: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/api/runtime", tags=["runtime"], dependencies=[Depends(require_api_key)])
    session_creator = _SessionCreator(
        client=client,
        store=store,
        provisioner=provisioner,
        config=RuntimeSessionUpstreamConfig(model_type, credential_id, model_name, model_parameters),
    )
    _register_agent_version_routes(router, provisioner)
    _register_session_creation_route(router, session_creator)
    _register_session_listing_route(router, client, store, provisioner)
    _register_session_read_routes(router, client, provisioner)
    register_session_resource_routes(router, client=client, provisioner=provisioner)
    register_native_agent_schema_route(router, client=client)
    _register_chat_route(router, client, store, provisioner)
    _register_session_write_routes(router, client, store, provisioner)
    return router


def _register_agent_version_routes(router: APIRouter, provisioner: RuntimeAgentProvisioner) -> None:
    @router.get(
        "/agents/{governance_agent_id}/current",
        response_model=RuntimeCurrentVersionResponse,
        summary="Read the exact current governed-to-Runtime version tuple",
    )
    def current_runtime_version(governance_agent_id: str) -> RuntimeCurrentVersionResponse:
        current = provisioner.inspect_current(governance_agent_id)
        return RuntimeCurrentVersionResponse(
            governance_agent_id=current.governance_agent_id,
            agent_version_id=current.agent_version_id,
            harness_digest=current.harness_digest,
            runtime_agent_id=current.runtime_agent_id,
            provisioned=current.provisioned,
        )


def _register_session_creation_route(router: APIRouter, session_creator: _SessionCreator) -> None:
    @router.post("/sessions/", status_code=201, summary="Create a version-pinned AgentScope session")
    async def create_session(
        request_data: RuntimeSessionCreateRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=256)] = None,
    ) -> Response:
        clean_key = idempotency_key.strip() if idempotency_key else None
        return await session_creator.create(request_data, clean_key)


def _register_session_listing_route(
    router: APIRouter,
    client: AgentScopeRuntimeClient,
    store: RuntimeRunStore,
    provisioner: RuntimeAgentProvisioner,
) -> None:
    @router.get("/sessions/", summary="List sessions across all versions of one governed Agent")
    async def list_sessions(
        governance_agent_id: Annotated[str, Query(min_length=1, max_length=128)],
    ) -> Response:
        versions = store.agent_versions_for_agent(governance_agent_id)
        allowed_by_runtime = {version.runtime_agent_id: set() for version in versions if version.source_kind == "published"}
        active_run_by_session: dict[str, str | None] = {}
        for candidate in store.sessions_for_agent(governance_agent_id):
            if candidate.runtime_agent_id not in allowed_by_runtime:
                continue
            try:
                binding = provisioner.require_session(candidate.session_id, candidate.runtime_agent_id)
            except RuntimeObjectNotFound:
                continue
            allowed_by_runtime[binding.runtime_agent_id].add(binding.session_id)
            active_run_by_session[binding.session_id] = binding.active_run_id
        sessions: list[object] = []
        for runtime_agent_id, allowed_ids in allowed_by_runtime.items():
            upstream = await _call(client, "GET", "/sessions/", params={"agent_id": runtime_agent_id})
            values = upstream.body.get("sessions") if isinstance(upstream.body, dict) else None
            if not isinstance(values, list):
                raise RuntimeUpstreamError(
                    502,
                    b'{"detail":"Runtime returned invalid Session list"}',
                )
            for value in values:
                session_id = canonical_session_id_from_view(value)
                if session_id in allowed_ids:
                    sessions.append(
                        _project_session_view(
                            value,
                            active_run_by_session,
                            session_id=session_id,
                        ),
                    )
        return JSONResponse({"sessions": sessions, "total": len(sessions)})


def _project_session_view(
    value: object,
    active_run_by_session: dict[str, str | None],
    *,
    session_id: str,
) -> object:
    """在 AgentScope SessionView 外层投影 AgentGov 拥有的活动 run fence。"""

    if not isinstance(value, dict) or session_id not in active_run_by_session:
        return value
    return {**value, "active_run_id": active_run_by_session[session_id]}


def _register_session_read_routes(
    router: APIRouter,
    client: AgentScopeRuntimeClient,
    provisioner: RuntimeAgentProvisioner,
) -> None:
    @router.get("/sessions/{session_id}/messages", summary="Read canonical AgentScope messages")
    async def messages(
        session_id: str,
        agent_id: Annotated[str, Query(min_length=1, max_length=128)],
        before: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> Response:
        binding = provisioner.require_session(session_id, agent_id)
        params: dict[str, object] = {"agent_id": binding.runtime_agent_id, "limit": limit}
        if before:
            params["before"] = before
        upstream = await _call(client, "GET", f"/sessions/{session_id}/messages", params=params)
        return _json_upstream(upstream)

    @router.get("/sessions/{session_id}/status", summary="Read canonical AgentScope session status")
    async def session_status(
        session_id: str,
        agent_id: Annotated[str, Query(min_length=1, max_length=128)],
    ) -> Response:
        binding = provisioner.require_session(session_id, agent_id)
        upstream = await _call(client, "GET", f"/sessions/{session_id}/status", params={"agent_id": binding.runtime_agent_id})
        return _json_upstream(upstream)

    @router.get(
        "/sessions/{session_id}/stream",
        summary="Readiness comment followed by raw AgentScope AgentEvent SSE",
        description=(
            "Immediately emits one minimal SSE comment so browser fetch observes readiness; "
            "every subsequent upstream chunk is forwarded in order without modification."
        ),
    )
    async def stream(
        session_id: str,
        agent_id: Annotated[str, Query(min_length=1, max_length=128)],
    ) -> StreamingResponse:
        binding = provisioner.require_session(session_id, agent_id)
        upstream = await client.start_stream(f"/sessions/{session_id}/stream", params={"agent_id": binding.runtime_agent_id})

        headers = copy_response_headers(dict(upstream.headers))
        headers.setdefault("Cache-Control", "no-cache")
        headers.setdefault("X-Accel-Buffering", "no")
        return StreamingResponse(
            _runtime_stream_body(upstream),
            status_code=upstream.status_code,
            headers=headers,
            media_type="text/event-stream",
        )


async def _runtime_stream_body(upstream: httpx.Response) -> AsyncIterator[bytes]:
    """只增加建连 comment；上游事件块、顺序和字节保持原样。"""

    try:
        yield _RUNTIME_SSE_READINESS_COMMENT
        async for chunk in upstream.aiter_raw():
            yield chunk
    finally:
        await upstream.aclose()


def _register_chat_route(
    router: APIRouter,
    client: AgentScopeRuntimeClient,
    store: RuntimeRunStore,
    provisioner: RuntimeAgentProvisioner,
) -> None:
    @router.post("/chat/", summary="Trigger one governed AgentScope run")
    async def chat(request_data: RuntimeChatRequest) -> Response:
        provisioner.require_session(request_data.session_id, request_data.agent_id)
        triggered = await admit_and_trigger_chat(
            client=client,
            store=store,
            session_id=request_data.session_id,
            runtime_agent_id=request_data.agent_id,
            input_value=request_data.input,
            alert_id=request_data.alert_id,
            case_id=request_data.case_id,
            metadata=request_data.metadata,
            client_operation_id=request_data.client_operation_id,
            confirmation_scope=request_data.confirmation_scope,
            expected_run_id=request_data.expected_run_id,
        )
        return Response(
            content=triggered.body,
            status_code=triggered.status_code,
            headers={
                **triggered.headers,
                "Content-Type": triggered.content_type,
                RUN_ID_HEADER: triggered.run.run_id,
                SESSION_ID_HEADER: triggered.run.session_id,
            },
        )


def _register_session_write_routes(
    router: APIRouter,
    client: AgentScopeRuntimeClient,
    store: RuntimeRunStore,
    provisioner: RuntimeAgentProvisioner,
) -> None:
    @router.post("/sessions/{session_id}/interrupt", status_code=202, summary="Interrupt an AgentScope run")
    async def interrupt(
        session_id: str,
        agent_id: Annotated[str, Query(min_length=1, max_length=128)],
    ) -> Response:
        binding = provisioner.require_session(session_id, agent_id)
        run = store.active_run_for_session(session_id)
        if run is not None:
            store.mark_cancel_requested(run.run_id)
            upstream = await interrupt_active_run(client, store, run, primary_session_id=session_id)
        else:
            upstream = await _call(
                client,
                "POST",
                f"/sessions/{session_id}/interrupt",
                params={"agent_id": binding.runtime_agent_id},
            )
        headers = {**copy_response_headers(upstream.headers), SESSION_ID_HEADER: session_id}
        if run is not None:
            headers[RUN_ID_HEADER] = run.run_id
        return JSONResponse(upstream.body, status_code=upstream.status_code, headers=headers)

    @router.delete("/sessions/{session_id}", status_code=204, summary="Delete an AgentScope session")
    async def delete_session(
        session_id: str,
        agent_id: Annotated[str, Query(min_length=1, max_length=128)],
    ) -> Response:
        binding = provisioner.require_session(session_id, agent_id)
        if store.active_run_for_session(session_id) is not None:
            raise RuntimeStoreError("An active run must terminate before the session can be deleted")
        try:
            upstream = await _call(client, "DELETE", f"/sessions/{session_id}", params={"agent_id": binding.runtime_agent_id})
            status_code = upstream.status_code
        except RuntimeUpstreamError as exc:
            if exc.status_code != 404:
                raise
            status_code = 204
        store.delete_session_binding(session_id)
        return Response(status_code=status_code, headers={SESSION_ID_HEADER: session_id})


def _intent_replay_response(intent: RuntimeSessionCreationIntentModel, agent_id: str) -> Response:
    if intent.runtime_agent_id != agent_id:
        raise RuntimeStateConflict("Idempotency-Key is already bound to another Runtime Agent")
    status = SessionCreationStatus(intent.status)
    if status is SessionCreationStatus.BOUND and intent.session_id:
        return JSONResponse(
            {"session_id": intent.session_id},
            status_code=200,
            headers={SESSION_ID_HEADER: intent.session_id},
        )
    if status in {SessionCreationStatus.FAILED, SessionCreationStatus.FAILED_CLEANED}:
        raise RuntimeStateConflict("Idempotent Session creation previously failed; use a new Idempotency-Key")
    raise RuntimeStateConflict("Idempotent Session creation is still being reconciled; retry later")


def create_agent_run_router(
    *,
    client: AgentScopeRuntimeClient,
    store: RuntimeRunStore,
    trace_fetcher: Callable[[str], dict[str, object] | None],
    authorize_run: Callable[[AgentRunResponse], None],
    require_api_key: Callable,
) -> APIRouter:
    router = APIRouter(prefix="/api/agent-runs", tags=["agent-runs"], dependencies=[Depends(require_api_key)])
    _register_pending_action_route(router, store, authorize_run)
    _register_run_cancel_route(router, client, store, authorize_run)

    @router.get(
        "/by-client-operation",
        response_model=AgentRunResponse,
        summary="Resolve one exact AgentGov run from a durable client operation identity",
    )
    async def get_run_by_client_operation(
        session_id: Annotated[str, Query(min_length=1, max_length=128)],
        client_operation_id: Annotated[
            str,
            Query(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"),
        ],
    ) -> AgentRunResponse:
        run = store.run_for_client_operation(
            session_id=session_id,
            client_operation_id=client_operation_id,
        )
        authorize_run(run)
        return run

    @router.get(
        "/{run_id}",
        response_model=AgentRunResponse,
        summary="Get one AgentGov run and its reply/trace links",
    )
    async def get_run(run_id: str) -> AgentRunResponse:
        run = store.get_run(run_id)
        authorize_run(run)
        return run

    @router.get("/{run_id}/trace", response_model=AgentRunTraceResponse, summary="Resolve an OTel trace from a run")
    async def get_trace(run_id: str) -> AgentRunTraceResponse:
        run = store.get_run(run_id)
        authorize_run(run)
        trace = await asyncio.to_thread(trace_fetcher, run.trace_id) if run.trace_id else None
        expectations = store.trace_expectations(run_id)
        if trace and run.status in TERMINAL_RUN_STATUSES and trace_has_complete_governed_run(trace, run, expectations):
            trace_url = trace.get("url")
            run = store.mark_trace_observed(
                run_id,
                trace_url=trace_url if isinstance(trace_url, str) else None,
            )
        return AgentRunTraceResponse(
            run_id=run.run_id,
            trace_id=run.trace_id,
            trace_url=run.trace_url,
            trace_status=run.trace_status,
        )

    return router


def _register_pending_action_route(
    router: APIRouter,
    store: RuntimeRunStore,
    authorize_run: Callable[[AgentRunResponse], None],
) -> None:
    @router.get(
        "/{run_id}/pending-actions",
        response_model=list[RuntimePendingActionResponse],
        summary="Read the still-pending HITL actions for one authorized run",
    )
    async def get_pending_actions(run_id: str) -> list[RuntimePendingActionResponse]:
        run = store.get_run(run_id)
        authorize_run(run)
        return store.pending_actions_for_run(run_id)


def _register_run_cancel_route(
    router: APIRouter,
    client: AgentScopeRuntimeClient,
    store: RuntimeRunStore,
    authorize_run: Callable[[AgentRunResponse], None],
) -> None:
    @router.post("/{run_id}/cancel", status_code=202, summary="Cancel one exact AgentGov run")
    async def cancel(run_id: str) -> Response:
        run = store.get_run(run_id)
        authorize_run(run)
        was_requested = run.metadata.get("cancellation_requested") is True
        requested = store.mark_cancel_requested(run_id)
        if requested.status in TERMINAL_RUN_STATUSES:
            return _cancel_response(requested)
        if was_requested and requested.metadata.get("recovery_interrupt_requested") is True:
            return _cancel_response(requested)
        try:
            await interrupt_active_run(
                client,
                store,
                requested,
                primary_session_id=requested.session_id,
            )
        except RuntimeStateConflict:
            settled = store.get_run(run_id)
            if settled.status in TERMINAL_RUN_STATUSES and settled.metadata.get("cancellation_requested") is True:
                return _cancel_response(settled)
            raise
        return _cancel_response(store.get_run(run_id))


def create_internal_runtime_router(*, store: RuntimeRunStore, shared_secret: str) -> APIRouter:
    router = APIRouter(prefix="/internal", tags=["internal"], include_in_schema=False)

    async def verify(request: Request) -> bytes:
        body = await _read_bounded_internal_body(request)
        if not verify_internal_request(
            secret=shared_secret,
            timestamp=request.headers.get("X-AgentGov-Timestamp"),
            signature=request.headers.get("X-AgentGov-Signature"),
            method=request.method,
            path=request.url.path,
            body=body,
        ):
            raise RuntimeAuthenticationError("Invalid internal Runtime signature")
        return body

    @router.get("/runtime-context/{session_id}")
    async def runtime_context(session_id: str, request: Request):
        await verify(request)
        return store.runtime_context(session_id)

    @router.post(
        "/runtime-boots",
        response_model=RuntimeBootAck,
    )
    async def runtime_boot(request: Request) -> RuntimeBootAck:
        body = await verify(request)
        try:
            announcement = RuntimeBootAnnouncement.model_validate_json(body)
        except ValueError as exc:
            raise RuntimeStoreError("Invalid Runtime boot announcement") from exc
        if announcement.runtime_version != APP_VERSION:
            raise RuntimeStateConflict("Runtime version does not match AgentGov")
        return RuntimeBootAck(
            boot_id=announcement.boot_id,
            runtime_version=APP_VERSION,
            recovery_run_ids=store.reconcile_after_runtime_boot(
                announcement.boot_id,
                announcement.runtime_version,
            ),
        )

    @router.post(
        "/runtime-child-sessions",
        response_model=RuntimeContextResponse,
    )
    async def runtime_child_session(request: Request) -> RuntimeContextResponse:
        body = await verify(request)
        try:
            registration = RuntimeChildSessionRegistration.model_validate_json(body)
        except ValueError as exc:
            raise RuntimeStoreError("Invalid Runtime child Session registration") from exc
        return store.bind_team_child(registration)

    @router.post(
        "/runtime-team-inbox",
        response_model=RuntimeTeamInboxAck,
    )
    async def runtime_team_inbox(request: Request) -> RuntimeTeamInboxAck:
        body = await verify(request)
        try:
            delivery = RuntimeTeamInboxDelivery.model_validate_json(body)
        except ValueError as exc:
            raise RuntimeStoreError("Invalid Runtime Team inbox delivery") from exc
        return store.record_team_inbox_delivery(delivery)

    @router.post("/runtime-receipts")
    async def runtime_receipt(request: Request):
        body = await verify(request)
        try:
            receipt = RuntimeReceipt.model_validate_json(body)
        except ValueError as exc:
            raise RuntimeStoreError("Invalid Runtime receipt") from exc
        return store.apply_receipt(receipt)

    return router


async def _read_bounded_internal_body(request: Request) -> bytes:
    declared_length = request.headers.get("content-length")
    if declared_length is not None:
        try:
            parsed_length = int(declared_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid internal Runtime Content-Length") from exc
        if parsed_length < 0:
            raise HTTPException(status_code=400, detail="Invalid internal Runtime Content-Length")
        if parsed_length > _MAX_INTERNAL_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Internal Runtime request body is too large")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_INTERNAL_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Internal Runtime request body is too large")
        body.extend(chunk)
    return bytes(body)
