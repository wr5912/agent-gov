from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from agentscope.agent import Agent
from agentscope.credential import OpenAICredential
from agentscope.event import (
    ReplyEndEvent,
    ReplyStartEvent,
    RequireExternalExecutionEvent,
    RequireUserConfirmEvent,
    ToolResultEndEvent,
)
from agentscope.message import AssistantMsg, ToolCallBlock, ToolResultState
from agentscope.model import OpenAIChatModel
from agentscope.state import AgentState
from agentscope.types import ReplyFinishedReason
from agentscope_runtime.context_registry import RuntimeContext, bind_reply_context
from agentscope_runtime.credential_storage import ProvisionedAsyncSQLAlchemyStorage
from agentscope_runtime.observability import OTelRuntime
from agentscope_runtime.receipt_middleware import (
    CURRENT_RUNTIME_CONTEXT,
    AgentGovReceiptDeliveryError,
    AgentGovReceiptDispatcher,
    AgentGovReceiptFlushError,
    AgentGovReceiptLifespanMiddleware,
    AgentGovReceiptMiddleware,
    RuntimeReceipt,
    build_interrupted_receipt,
    build_runtime_receipt,
)
from agentscope_runtime.run_trace import AgentGovRunTraceRegistry, AgentGovTraceIdGenerator
from agentscope_runtime.service import create_runtime_app
from agentscope_runtime.settings import RUNTIME_USER_ID, RuntimeSettings
from app.api_mode import ApiModeGateMiddleware
from app.routers.error_handlers import register_error_handlers
from app.runtime.runtime_db import make_session_factory
from app.runtime_gateway.contracts import RunStatus
from app.runtime_gateway.contracts import RuntimeReceipt as ControlPlaneReceipt
from app.runtime_gateway.models import RuntimeReceiptModel
from app.runtime_gateway.router import create_internal_runtime_router
from app.runtime_gateway.store import RuntimeRunStore
from fastapi import FastAPI
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode
from sqlalchemy import select

from runtime_loopback import serve_loopback, serve_single_response_loss_proxy, unused_loopback_port

_SHARED_SECRET = "runtime-control-receipt-secret"
_CUTOVER_ID = "runtime-control-receipt-cutover"
_PROVIDER_LOG_CANARY = "provider-secret-must-not-enter-runtime-logs"


def _settings(
    base_url: str,
    *,
    request_timeout: float = 0.1,
    retry_backoff: float = 0.02,
    flush_timeout: float = 0.3,
) -> RuntimeSettings:
    return RuntimeSettings(
        shared_secret=_SHARED_SECRET,
        provider_api_key=_PROVIDER_LOG_CANARY,
        agentgov_api_base_url=base_url,
        request_timeout_seconds=request_timeout,
        receipt_retry_attempts=2,
        receipt_retry_backoff_seconds=retry_backoff,
        receipt_flush_timeout_seconds=flush_timeout,
    )


def _storage_settings(tmp_path: Path, base_url: str, flush_timeout: float) -> RuntimeSettings:
    data_dir = tmp_path / "runtime-data"
    business_root = tmp_path / "business-agents"
    candidates_root = tmp_path / "candidates"
    workspaces_root = tmp_path / "workspaces"
    for directory in (data_dir, business_root, candidates_root, workspaces_root):
        directory.mkdir(parents=True, exist_ok=True)
    return RuntimeSettings(
        shared_secret=_SHARED_SECRET,
        provider_api_key=_PROVIDER_LOG_CANARY,
        agentgov_api_base_url=base_url,
        data_dir=data_dir,
        business_agents_root=business_root,
        candidates_root=candidates_root,
        workspaces_root=workspaces_root,
        database_url=f"sqlite+aiosqlite:///{data_dir / 'agentscope.db'}",
        request_timeout_seconds=0.1,
        receipt_retry_attempts=2,
        receipt_retry_backoff_seconds=0.02,
        receipt_flush_timeout_seconds=flush_timeout,
    )


def _control_store(tmp_path: Path):
    session_id = f"session-{tmp_path.name}"
    store = RuntimeRunStore(make_session_factory(tmp_path / "control.db"))
    store.bind_agent_version(
        agent_id="agent-control",
        agent_version_id="version-control",
        digest="a" * 64,
        runtime_agent_id="runtime-agent-control",
    )
    store.bind_session(
        session_id=session_id,
        agent_id="agent-control",
        agent_version_id="version-control",
        runtime_agent_id="runtime-agent-control",
        digest="a" * 64,
    )
    run = store.begin_run(
        session_id=session_id,
        runtime_agent_id="runtime-agent-control",
        input_value={"role": "user", "content": []},
        alert_id=None,
        case_id=None,
        metadata={},
    )
    store.mark_trigger_started(run.run_id)
    return store, store.get_run(run.run_id)


def _context(run) -> RuntimeContext:
    return RuntimeContext(
        run_id=run.run_id,
        session_id=run.session_id,
        root_session_id=run.session_id,
        role="root",
        agent_id=run.agent_id,
        agent_version_id=run.agent_version_id,
        runtime_agent_id=run.runtime_agent_id,
        harness_digest=run.harness_digest,
        trace_id=run.trace_id,
        team_generation=run.team_generation,
    )


def _receipt(
    run,
    event_type: str,
    event_id: str,
    *,
    reply_id: str | None = "reply-control",
    payload: dict[str, object] | None = None,
) -> RuntimeReceipt:
    receipt_id = hashlib.sha256(
        f"{run.run_id}\n{run.session_id}\n{event_id}".encode(),
    ).hexdigest()
    return RuntimeReceipt(
        receipt_id=receipt_id,
        event_id=event_id,
        run_id=run.run_id,
        session_id=run.session_id,
        reply_id=reply_id,
        trace_id=run.trace_id,
        type=event_type,
        payload=payload or {},
    )


def _apply(store: RuntimeRunStore, receipt: RuntimeReceipt):
    return store.apply_receipt(
        ControlPlaneReceipt.model_validate(receipt.model_dump(mode="json")),
    )


def _terminal_reply_receipts(
    run,
    *,
    reply_end_event_id: str,
) -> tuple[RuntimeReceipt, RuntimeReceipt, RuntimeReceipt, RuntimeReceipt]:
    return (
        _receipt(run, "REPLY_START", "seed-reply-start"),
        _receipt(
            run,
            "REPLY_END",
            reply_end_event_id,
            payload={"finished_reason": "completed"},
        ),
        _receipt(
            run,
            "MESSAGE_PERSISTED",
            "seed-message-persisted",
            payload={
                "message_id": "reply-control",
                "message_persisted": True,
                "finished_reason": "completed",
                "error": None,
                "trace_complete": False,
            },
        ),
        _receipt(
            run,
            "SESSION_PERSISTED",
            "seed-session-persisted",
            reply_id=None,
            payload={"reply_ids": ["reply-control"], "team_generation": run.team_generation},
        ),
    )


def _seed_terminal_reply_evidence(
    store: RuntimeRunStore,
    run,
    *,
    reply_end_event_id: str,
) -> RuntimeReceipt:
    receipts = _terminal_reply_receipts(run, reply_end_event_id=reply_end_event_id)
    for receipt in receipts:
        _apply(store, receipt)
    return receipts[1]


def _write_gate(path: Path, *, blocked: bool) -> None:
    payload = (
        {
            "schema_version": 1,
            "state": "drain",
            "cutover_id": "another-cutover",
        }
        if blocked
        else {
            "schema_version": 1,
            "state": "open",
            "cutover_id": _CUTOVER_ID,
            "irreversible_at": "2026-09-11T00:00:00Z",
        }
    )
    replacement = path.with_suffix(".next")
    replacement.write_text(json.dumps(payload), encoding="utf-8")
    replacement.replace(path)


def _control_app(
    store: RuntimeRunStore,
    gate_file: Path,
    *,
    shared_secret: str = _SHARED_SECRET,
) -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        ApiModeGateMiddleware,
        mode="open",
        acceptance_identity=_CUTOVER_ID,
        acceptance_api_key=None,
        state_file=gate_file,
    )
    register_error_handlers(app)
    app.include_router(
        create_internal_runtime_router(
            store=store,
            shared_secret=shared_secret,
        ),
    )
    return app


def _trace_registry() -> tuple[AgentGovRunTraceRegistry, InMemorySpanExporter]:
    registry, exporter, _provider = _trace_components()
    return registry, exporter


def _trace_components() -> tuple[
    AgentGovRunTraceRegistry,
    InMemorySpanExporter,
    TracerProvider,
]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(id_generator=AgentGovTraceIdGenerator())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return (
        AgentGovRunTraceRegistry(provider.get_tracer("receipt-delivery-test")),
        exporter,
        provider,
    )


def _agent(context: RuntimeContext) -> Agent:
    state = AgentState(session_id=context.session_id)
    state.reply_id = "reply-control"
    credential = OpenAICredential(
        name="unused-local-regression-credential",
        api_key="not-used",
    )
    return Agent(
        name="Runtime receipt regression agent",
        system_prompt="",
        model=OpenAIChatModel(credential=credential, model="not-used"),
        state=state,
    )


def _receipt_types(store: RuntimeRunStore, run_id: str) -> set[str]:
    with store.Session() as db:
        return set(
            db.scalars(
                select(RuntimeReceiptModel.event_type).where(
                    RuntimeReceiptModel.run_id == run_id,
                ),
            ).all(),
        )


async def _wait_for_retry_log(caplog: pytest.LogCaptureFixture) -> None:
    deadline = asyncio.get_running_loop().time() + 3
    while "status=503" not in caplog.text:
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("control receipt did not reach the real API mode gate")
        await asyncio.sleep(0.01)


def test_reply_end_trace_finishes_after_waiter_cancellation_and_real_gate_503(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, run = _control_store(tmp_path)
    _seed_terminal_reply_evidence(
        store,
        run,
        reply_end_event_id="reply-end-after-503",
    )
    gate_file = tmp_path / "api-gate.json"
    _write_gate(gate_file, blocked=True)
    with serve_loopback(_control_app(store, gate_file)) as base_url:

        async def exercise() -> InMemorySpanExporter:
            context = _context(run)
            registry, exporter = _trace_registry()
            dispatcher = AgentGovReceiptDispatcher(
                _settings(base_url),
                trace_registry=registry,
            )
            middleware = AgentGovReceiptMiddleware(
                _settings(base_url),
                receipt_dispatcher=dispatcher,
            )

            async def native_events(**_kwargs) -> AsyncIterator[ReplyEndEvent]:
                yield ReplyEndEvent(
                    id="reply-end-after-503",
                    session_id=context.session_id,
                    reply_id="reply-control",
                )

            token = CURRENT_RUNTIME_CONTEXT.set(context)
            try:
                stream = middleware.on_reply(_agent(context), {}, native_events)
                waiter = asyncio.create_task(anext(stream))
            finally:
                CURRENT_RUNTIME_CONTEXT.reset(token)
            await _wait_for_retry_log(caplog)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            _write_gate(gate_file, blocked=False)
            await dispatcher.aclose()
            return exporter

        exporter = asyncio.run(exercise())

    terminal = store.get_run(run.run_id)
    assert terminal.status is RunStatus.SUCCEEDED
    assert "RUN_INTERRUPTED" not in _receipt_types(store, run.run_id)
    roots = [span for span in exporter.get_finished_spans() if span.name == "agentgov.run"]
    assert len(roots) == 1
    assert roots[0].status.status_code is StatusCode.OK
    assert roots[0].attributes["agentgov.run.finished_reason"] == "completed"
    assert _PROVIDER_LOG_CANARY not in caplog.text


@pytest.mark.parametrize("exit_mode", ("cancel", "error"))
def test_exceptional_reply_stream_posts_deterministic_interruption_to_real_store(
    tmp_path: Path,
    exit_mode: str,
) -> None:
    store, run = _control_store(tmp_path)
    gate_file = tmp_path / "api-gate.json"
    _write_gate(gate_file, blocked=False)
    with serve_loopback(_control_app(store, gate_file)) as base_url:

        async def exercise() -> InMemorySpanExporter:
            context = _context(run)
            registry, exporter = _trace_registry()
            dispatcher = AgentGovReceiptDispatcher(
                _settings(base_url),
                trace_registry=registry,
            )
            middleware = AgentGovReceiptMiddleware(
                _settings(base_url),
                receipt_dispatcher=dispatcher,
            )
            hold_stream = asyncio.Event()

            async def native_events(**_kwargs) -> AsyncIterator[ReplyStartEvent]:
                yield ReplyStartEvent(
                    id="reply-start-before-cancel",
                    session_id=context.session_id,
                    reply_id="reply-control",
                    name="Agent",
                )
                if exit_mode == "error":
                    raise RuntimeError("forced downstream regression failure")
                await hold_stream.wait()

            token = CURRENT_RUNTIME_CONTEXT.set(context)
            try:
                stream = middleware.on_reply(_agent(context), {}, native_events)
                first = await anext(stream)
                assert first.type == "REPLY_START"
                waiter = asyncio.create_task(anext(stream))
            finally:
                CURRENT_RUNTIME_CONTEXT.reset(token)
            await asyncio.sleep(0)
            if exit_mode == "cancel":
                waiter.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiter
            else:
                with pytest.raises(RuntimeError, match="forced downstream"):
                    await waiter
            await dispatcher.aclose()
            assert store.get_run(run.run_id).status is RunStatus.RUNNING
            assert [span for span in exporter.get_finished_spans() if span.name == "agentgov.run"] == []
            terminal = store.fail_recovery(
                run.run_id,
                error="confirmed quiescent after Runtime interruption",
            )
            assert terminal.status is RunStatus.INTERRUPTED
            terminal_dispatcher = AgentGovReceiptDispatcher(
                _settings(base_url),
                trace_registry=registry,
            )
            acknowledgement = await terminal_dispatcher.deliver(
                build_interrupted_receipt(context),
                context,
            )
            assert acknowledgement.status == RunStatus.INTERRUPTED
            await terminal_dispatcher.aclose()
            return exporter

        exporter = asyncio.run(exercise())

    interrupted = store.get_run(run.run_id)
    assert interrupted.status is RunStatus.INTERRUPTED
    assert interrupted.metadata["runtime_interrupted_session_ids"] == [run.session_id]
    assert _receipt_types(store, run.run_id) == {"REPLY_START", "RUN_INTERRUPTED"}
    roots = [span for span in exporter.get_finished_spans() if span.name == "agentgov.run"]
    assert len(roots) == 1
    assert roots[0].status.status_code is StatusCode.ERROR
    assert roots[0].attributes["agentgov.run.finished_reason"] == "interrupted"


def test_control_receipt_retries_real_connection_refusal_until_api_starts(
    tmp_path: Path,
) -> None:
    store, run = _control_store(tmp_path)
    receipt = _seed_terminal_reply_evidence(
        store,
        run,
        reply_end_event_id="connection-recovery-reply-end",
    )
    gate_file = tmp_path / "api-gate.json"
    _write_gate(gate_file, blocked=False)
    app = _control_app(store, gate_file)
    port = unused_loopback_port()

    async def exercise() -> None:
        registry, _exporter = _trace_registry()
        dispatcher = AgentGovReceiptDispatcher(
            _settings(f"http://127.0.0.1:{port}", retry_backoff=0.03),
            trace_registry=registry,
        )
        delivery = asyncio.create_task(dispatcher.deliver(receipt, _context(run)))
        await asyncio.sleep(0.08)
        assert not delivery.done()
        with serve_loopback(app, port=port):
            acknowledgement = await asyncio.wait_for(delivery, timeout=3)
        assert acknowledgement.run_id == run.run_id
        await dispatcher.aclose()

    asyncio.run(exercise())
    assert store.get_run(run.run_id).status is RunStatus.SUCCEEDED


def test_response_loss_replays_committed_receipt_and_finishes_same_trace(tmp_path: Path) -> None:
    store, run = _control_store(tmp_path)
    receipts = _terminal_reply_receipts(run, reply_end_event_id="response-loss-reply-end")
    for receipt in (receipts[0], receipts[1], receipts[3]):
        _apply(store, receipt)
    gate_file = tmp_path / "api-gate.json"
    _write_gate(gate_file, blocked=False)
    with serve_loopback(_control_app(store, gate_file)) as upstream_url:
        with serve_single_response_loss_proxy(upstream_url) as (proxy_url, response_dropped):

            async def exercise():
                context = _context(run)
                registry, exporter = _trace_registry()
                stage = registry.start_stage(
                    context,
                    stage="initial",
                    reply_id="reply-control",
                    runtime_version="test",
                    agentscope_version="2.0.8",
                )
                stage.end(failed=False)
                dispatcher = AgentGovReceiptDispatcher(
                    _settings(proxy_url),
                    trace_registry=registry,
                )
                acknowledgement = await dispatcher.deliver(receipts[2], context)
                await dispatcher.aclose()
                return acknowledgement, exporter

            acknowledgement, exporter = asyncio.run(exercise())

    assert response_dropped.is_set()
    assert acknowledgement.run_id == run.run_id
    assert acknowledgement.status == RunStatus.SUCCEEDED
    with store.Session() as db:
        persisted = db.scalars(
            select(RuntimeReceiptModel).where(RuntimeReceiptModel.receipt_id == receipts[2].receipt_id),
        ).all()
    assert len(persisted) == 1
    spans = exporter.get_finished_spans()
    assert [span.name for span in spans] == ["agentgov.run.stage", "agentgov.run"]
    assert {f"{span.context.trace_id:032x}" for span in spans} == {run.trace_id}


def test_control_receipt_shutdown_flush_is_bounded_against_real_gate_503(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, run = _control_store(tmp_path)
    gate_file = tmp_path / "api-gate.json"
    _write_gate(gate_file, blocked=True)
    with serve_loopback(_control_app(store, gate_file)) as base_url:

        async def exercise() -> float:
            registry, _exporter = _trace_registry()
            dispatcher = AgentGovReceiptDispatcher(
                _settings(base_url, flush_timeout=0.08),
                trace_registry=registry,
            )
            dispatcher.schedule(
                _receipt(run, "REPLY_END", "pending-event"),
                _context(run),
            )
            await _wait_for_retry_log(caplog)
            started = time.monotonic()
            with pytest.raises(AgentGovReceiptFlushError):
                await dispatcher.aclose()
            with pytest.raises(RuntimeError, match="is closing"):
                dispatcher.schedule(
                    _receipt(run, "REPLY_END", "late-event"),
                    _context(run),
                )
            return time.monotonic() - started

        elapsed = asyncio.run(exercise())

    assert elapsed < 0.5
    assert "flush deadline expired" in caplog.text
    assert "pending-event" in caplog.text
    assert _PROVIDER_LOG_CANARY not in caplog.text


def test_fire_and_forget_interruption_auth_failure_fails_shutdown_without_credentials(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, run = _control_store(tmp_path)
    gate_file = tmp_path / "api-gate.json"
    _write_gate(gate_file, blocked=False)
    with serve_loopback(
        _control_app(store, gate_file, shared_secret="different-runtime-secret"),
    ) as base_url:

        async def exercise() -> None:
            registry, _exporter = _trace_registry()
            dispatcher = AgentGovReceiptDispatcher(
                _settings(base_url),
                trace_registry=registry,
            )
            dispatcher.schedule(
                build_interrupted_receipt(_context(run)),
                _context(run),
            )
            deadline = asyncio.get_running_loop().time() + 3
            while "failed permanently" not in caplog.text:
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError("permanent receipt failure was not reported")
                await asyncio.sleep(0.01)
            with pytest.raises(AgentGovReceiptDeliveryError):
                await dispatcher.aclose()

        asyncio.run(exercise())

    assert _receipt_types(store, run.run_id) == set()
    assert "failed permanently" in caplog.text
    assert "HTTPStatusError" in caplog.text
    assert _SHARED_SECRET not in caplog.text
    assert _PROVIDER_LOG_CANARY not in caplog.text


@pytest.mark.parametrize(
    "shutdown_mode",
    ("normal", "error", "cancel", "flush_timeout"),
)
def test_lifespan_orders_storage_control_trace_and_provider_on_all_exit_paths(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    shutdown_mode: str,
) -> None:
    store, run = _control_store(tmp_path)
    receipts = _terminal_reply_receipts(run, reply_end_event_id="shutdown-reply-end")
    for receipt in receipts[:2]:
        _apply(store, receipt)
    gate_file = tmp_path / "api-gate.json"
    _write_gate(gate_file, blocked=True)
    with serve_loopback(_control_app(store, gate_file)) as control_url:
        settings = _storage_settings(
            tmp_path,
            control_url,
            0.12 if shutdown_mode == "flush_timeout" else 1.5,
        )
        registry, exporter, provider = _trace_components()
        otel_runtime = OTelRuntime(provider, flush_timeout_millis=300)
        dispatcher = AgentGovReceiptDispatcher(
            settings,
            trace_registry=registry,
        )
        context = _context(run)
        stage = registry.start_stage(
            context,
            stage="initial",
            reply_id="reply-control",
            runtime_version=settings.runtime_version,
            agentscope_version="2.0.8",
        )
        stage.end(failed=False)
        storage = ProvisionedAsyncSQLAlchemyStorage(
            settings,
            receipt_dispatcher=dispatcher,
        )

        @asynccontextmanager
        async def storage_lifespan(_app: FastAPI) -> AsyncIterator[None]:
            await storage.__aenter__()
            bind_reply_context(context, "reply-control")
            await storage.upsert_message(
                RUNTIME_USER_ID,
                context.session_id,
                AssistantMsg(
                    "Agent",
                    "completed",
                    id="reply-control",
                    finished_reason=ReplyFinishedReason.COMPLETED,
                ),
            )
            dispatcher.schedule(receipts[3], context)
            try:
                yield
            finally:
                await storage.aclose()
                assert [span.name for span in exporter.get_finished_spans()] == [
                    "agentgov.run.stage",
                ]
                if shutdown_mode != "flush_timeout":
                    _write_gate(gate_file, blocked=False)
                if shutdown_mode == "error":
                    raise RuntimeError("forced lifecycle regression failure")
                if shutdown_mode == "cancel":
                    current = asyncio.current_task()
                    assert current is not None
                    current.cancel()
                    await asyncio.sleep(0)

        inner = FastAPI(lifespan=storage_lifespan)
        runtime = AgentGovReceiptLifespanMiddleware(
            inner,
            dispatcher=dispatcher,
            trace_registry=registry,
            provider_shutdown=otel_runtime.shutdown,
        )
        with serve_loopback(runtime, lifespan="on"):
            deadline = time.monotonic() + 3
            while "status=503" not in caplog.text and time.monotonic() < deadline:
                time.sleep(0.01)

    expected_run_status = RunStatus.FINALIZING if shutdown_mode == "flush_timeout" else RunStatus.SUCCEEDED
    assert store.get_run(run.run_id).status is expected_run_status
    roots = [span for span in exporter.get_finished_spans() if span.name == "agentgov.run"]
    assert len(roots) == 1
    expected_status = StatusCode.ERROR if shutdown_mode == "flush_timeout" else StatusCode.OK
    expected_reason = "interrupted" if shutdown_mode == "flush_timeout" else "completed"
    assert roots[0].status.status_code is expected_status
    assert roots[0].attributes["agentgov.run.finished_reason"] == expected_reason
    assert exporter.export(()) is SpanExportResult.FAILURE
    if shutdown_mode != "normal":
        assert "shutdown stage failed" in caplog.text
    assert _PROVIDER_LOG_CANARY not in caplog.text


def test_runtime_service_wires_one_shared_control_receipt_dispatcher(tmp_path: Path) -> None:
    business_root = tmp_path / "business-agents"
    candidates_root = tmp_path / "candidates"
    business_root.mkdir()
    candidates_root.mkdir()
    settings = RuntimeSettings(
        shared_secret=_SHARED_SECRET,
        provider_api_key=_PROVIDER_LOG_CANARY,
        agentgov_api_base_url="http://127.0.0.1:9",
        data_dir=tmp_path / "runtime-data",
        business_agents_root=business_root,
        candidates_root=candidates_root,
        workspaces_root=tmp_path / "workspaces",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'runtime-data' / 'agentscope.db'}",
    )

    app = create_runtime_app(settings)

    assert isinstance(app.state.agentgov_receipt_dispatcher, AgentGovReceiptDispatcher)
    middleware_classes = [middleware.cls for middleware in app.user_middleware]
    assert middleware_classes.index(AgentGovReceiptLifespanMiddleware) == 1


def test_every_control_event_and_interruption_has_repeatable_identity() -> None:
    context = RuntimeContext(
        run_id="run-stable",
        session_id="session-stable",
        root_session_id="session-stable",
        role="root",
        agent_id="agent-stable",
        agent_version_id="version-stable",
        runtime_agent_id="runtime-agent-stable",
        harness_digest="e" * 64,
        trace_id="b" * 32,
        team_generation=0,
    )
    tool_call = ToolCallBlock(id="call-stable", name="Read", input='{"path":"private"}')
    events = [
        ReplyStartEvent(
            id="event-reply-start",
            session_id=context.session_id,
            reply_id="reply-stable",
            name="Agent",
        ),
        ReplyEndEvent(
            id="event-reply-end",
            session_id=context.session_id,
            reply_id="reply-stable",
        ),
        RequireUserConfirmEvent(
            id="event-user-confirm",
            reply_id="reply-stable",
            tool_calls=[tool_call],
        ),
        RequireExternalExecutionEvent(
            id="event-external-execution",
            reply_id="reply-stable",
            tool_calls=[tool_call],
        ),
        ToolResultEndEvent(
            id="event-tool-result-end",
            reply_id="reply-stable",
            tool_call_id=tool_call.id,
            state=ToolResultState.SUCCESS,
        ),
    ]

    for event in events:
        first = build_runtime_receipt(
            context,
            event,
            fallback_reply_id="reply-fallback",
        )
        repeated = build_runtime_receipt(
            context,
            event,
            fallback_reply_id="reply-fallback",
        )
        assert first == repeated
        assert first.event_id == event.id
        assert "private" not in json.dumps(first.model_dump(mode="json"))

    assert build_interrupted_receipt(context) == build_interrupted_receipt(context)
