from __future__ import annotations

import asyncio
import itertools
import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import httpx
import pytest
from agentscope.app import create_app as create_agentscope_app
from agentscope.app.storage import AsyncSQLAlchemyStorage
from agentscope.app.workspace_manager import LocalWorkspaceManager
from agentscope.credential import CredentialBase
from agentscope.formatter import OpenAIChatFormatter
from agentscope.message import AssistantMsg, HintBlock, Msg, TextBlock, ToolCallBlock
from agentscope.model import ChatModelBase, ChatResponse
from agentscope.state import AgentState
from agentscope.types import ReplyFinishedReason
from agentscope_runtime.context_registry import RuntimeContext, bind_reply_context
from agentscope_runtime.credential_storage import ProvisionedAsyncSQLAlchemyStorage
from agentscope_runtime.receipt_middleware import CURRENT_RUNTIME_CONTEXT, AgentGovReceiptMiddleware
from agentscope_runtime.run_trace import AgentGovRunTraceRegistry, AgentGovTraceIdGenerator
from agentscope_runtime.settings import RUNTIME_USER_ID, RuntimeSettings
from agentscope_runtime.signing import signed_headers
from agentscope_runtime.team_coordination import (
    AgentGovInMemoryMessageBus,
    RuntimeBootCoordinator,
    register_team_child_session,
)
from agentscope_runtime.trace_context_middleware import AgentGovTraceContextMiddleware
from app.routers.error_handlers import register_error_handlers
from app.runtime.runtime_db import make_session_factory
from app.runtime_gateway.contracts import (
    RunStatus,
    RuntimeChildSessionRegistration,
    RuntimeReceipt,
    RuntimeTeamInboxDelivery,
)
from app.runtime_gateway.models import RuntimePendingActionModel
from app.runtime_gateway.router import create_internal_runtime_router
from app.runtime_gateway.store import RuntimeRunStore, RuntimeStateConflict
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from pydantic import BaseModel, SecretStr


def _settings(tmp_path: Path) -> RuntimeSettings:
    return RuntimeSettings(
        shared_secret="s" * 32,
        provider_api_key="provider-key",
        data_dir=tmp_path / "data",
        business_agents_root=tmp_path / "business",
        candidates_root=tmp_path / "candidates",
        workspaces_root=tmp_path / "workspaces",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'data' / 'agentscope.db'}",
    )


def _context(**overrides: object) -> RuntimeContext:
    values = {
        "run_id": "run-1",
        "session_id": "leader-session",
        "root_session_id": "leader-session",
        "role": "root",
        "agent_id": "business-agent",
        "agent_version_id": "f" * 40,
        "runtime_agent_id": "leader-agent",
        "harness_digest": "a" * 64,
        "trace_id": "1" * 32,
        "team_generation": 0,
    }
    values.update(overrides)
    return RuntimeContext.model_validate(values)


class _TeamScriptCredential(CredentialBase):
    """只用于公共 AgentScope Team 链路测试的确定性模型凭据。"""

    type: Literal["agentgov_team_script"] = "agentgov_team_script"
    api_key: SecretStr

    @classmethod
    def get_chat_model_class(cls) -> type[ChatModelBase]:
        return _TeamScriptModel


class _TeamScriptModel(ChatModelBase):
    """根据公共消息历史依次选择 TeamCreate/AgentCreate/TeamSay。"""

    class Parameters(BaseModel):
        pass

    actions: list[str] = []

    def __init__(
        self,
        credential: CredentialBase,
        model: str,
        parameters: BaseModel | None = None,
        **_: object,
    ) -> None:
        super().__init__(
            credential=credential,
            model=model,
            parameters=parameters or self.Parameters(),
            stream=False,
        )
        self.formatter = OpenAIChatFormatter()

    async def _call_api(self, *args: object, **kwargs: object) -> ChatResponse:
        del args
        messages = kwargs["messages"]
        assert isinstance(messages, list)
        hints: list[str] = []
        called: set[str] = set()
        for message in messages:
            assert isinstance(message, Msg)
            for block in message.get_content_blocks("hint"):
                assert isinstance(block, HintBlock)
                if isinstance(block.hint, str):
                    hints.append(block.hint)
            for block in message.get_content_blocks("tool_call"):
                assert isinstance(block, ToolCallBlock)
                called.add(block.name)

        joined_hints = "\n".join(hints)
        if '<team-message from="worker">' in joined_hints:
            self.actions.append("leader_final")
            return ChatResponse(
                content=[TextBlock(text="Leader integrated the worker report.")],
                is_last=True,
            )
        if '<team-message from="leader">' in joined_hints:
            if "TeamSay" not in called:
                self.actions.append("TeamSay")
                return ChatResponse(
                    content=[
                        ToolCallBlock(
                            id="worker-team-say",
                            name="TeamSay",
                            input=json.dumps(
                                {
                                    "content": "Worker completed the delegated task.",
                                    "to": "leader",
                                },
                            ),
                        ),
                    ],
                    is_last=True,
                )
            self.actions.append("worker_final")
            return ChatResponse(
                content=[TextBlock(text="Worker report sent.")],
                is_last=True,
            )
        if "TeamCreate" not in called:
            self.actions.append("TeamCreate")
            return ChatResponse(
                content=[
                    ToolCallBlock(
                        id="leader-team-create",
                        name="TeamCreate",
                        input=json.dumps(
                            {
                                "name": "public-chain-team",
                                "description": "Exercise AgentScope public Team lifecycle.",
                            },
                        ),
                    ),
                ],
                is_last=True,
            )
        if "AgentCreate" not in called:
            self.actions.append("AgentCreate")
            return ChatResponse(
                content=[
                    ToolCallBlock(
                        id="leader-agent-create",
                        name="AgentCreate",
                        input=json.dumps(
                            {
                                "name": "worker",
                                "description": "Complete one deterministic task.",
                                "prompt": "Complete the task and report to leader.",
                            },
                        ),
                    ),
                ],
                is_last=True,
            )
        self.actions.append("leader_waiting")
        return ChatResponse(
            content=[TextBlock(text="Waiting for the worker report.")],
            is_last=True,
        )


def test_cross_session_inbox_records_fence_before_queueing(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={"run_id": body["run_id"], "event_id": body["event_id"], "generation": 1},
        )

    bus = AgentGovInMemoryMessageBus(
        _settings(tmp_path),
        transport=httpx.MockTransport(handler),
    )

    async def exercise() -> list[tuple[str, dict]]:
        token = CURRENT_RUNTIME_CONTEXT.set(_context())
        try:
            await bus.queue_push("agentscope:inbox:worker-session", {"secret": "must-not-leak"})
        finally:
            CURRENT_RUNTIME_CONTEXT.reset(token)
        return await bus.queue_drain("agentscope:inbox:worker-session")

    queued = asyncio.run(exercise())
    assert len(queued) == 1
    assert requests[0].url.path == "/internal/runtime-team-inbox"
    body = json.loads(requests[0].content)
    assert body["run_id"] == "run-1"
    assert body["source_session_id"] == "leader-session"
    assert body["target_session_id"] == "worker-session"
    assert "secret" not in requests[0].content.decode()


def test_same_session_and_unmanaged_inbox_do_not_emit_team_event(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    bus = AgentGovInMemoryMessageBus(
        _settings(tmp_path),
        transport=httpx.MockTransport(handler),
    )

    async def exercise() -> None:
        await bus.queue_push("agentscope:inbox:unmanaged", {})
        token = CURRENT_RUNTIME_CONTEXT.set(_context())
        try:
            await bus.queue_push("agentscope:inbox:leader-session", {})
        finally:
            CURRENT_RUNTIME_CONTEXT.reset(token)

    asyncio.run(exercise())
    assert requests == []


def test_team_generation_advances_only_when_target_session_actually_drains(
    tmp_path: Path,
) -> None:
    generation = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal generation
        generation += 1
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "run_id": body["run_id"],
                "event_id": body["event_id"],
                "generation": generation,
            },
        )

    bus = AgentGovInMemoryMessageBus(
        _settings(tmp_path),
        transport=httpx.MockTransport(handler),
    )
    root = _context()
    worker = _context(
        session_id="worker-session",
        role="worker",
        runtime_agent_id="worker-agent",
        team_generation=2,
    )

    async def push_from_root(payload: dict[str, object]) -> None:
        token = CURRENT_RUNTIME_CONTEXT.set(root)
        try:
            await bus.queue_push("agentscope:inbox:worker-session", payload)
        finally:
            CURRENT_RUNTIME_CONTEXT.reset(token)

    async def drain_as_worker() -> list[tuple[str, dict]]:
        token = CURRENT_RUNTIME_CONTEXT.set(worker)
        try:
            return await bus.queue_drain("agentscope:inbox:worker-session")
        finally:
            CURRENT_RUNTIME_CONTEXT.reset(token)

    async def exercise() -> tuple[list[tuple[str, dict]], list[tuple[str, dict]]]:
        await push_from_root({"kind": "first"})
        first = await drain_as_worker()
        await push_from_root({"kind": "second"})

        # AgentScope 的 end-of-run pending check 会 drain 后原样 requeue；
        # 这不是实际 InboxMiddleware 消费，generation 不得前移。
        peeked = await bus.queue_drain("agentscope:inbox:worker-session")
        for _entry_id, payload in peeked:
            await bus.queue_push("agentscope:inbox:worker-session", payload)
        assert bus.processed_team_generation(worker) == 1
        second = await drain_as_worker()
        return first, second

    first, second = asyncio.run(exercise())
    assert first[0][1] == {"kind": "first"}
    assert second[0][1] == {"kind": "second"}
    assert bus.processed_team_generation(worker) == 2


def test_runtime_boot_notification_fences_active_run_and_is_idempotent(
    tmp_path: Path,
) -> None:
    store, run = _control_store(tmp_path)
    settings = replace(
        _settings(tmp_path),
        agentgov_api_base_url="http://agentgov-control.test",
    )
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(
        create_internal_runtime_router(
            store=store,
            shared_secret=settings.shared_secret,
        ),
    )
    transport = httpx.ASGITransport(app=app)

    async def announce() -> None:
        coordinator = RuntimeBootCoordinator(
            settings,
            transport=transport,
            boot_id="runtime-boot-test",
        )
        await asyncio.wait_for(
            coordinator.announce_until_acknowledged(),
            timeout=1,
        )
        assert coordinator.acknowledged is True

    asyncio.run(announce())
    fenced = store.get_run(run.run_id)
    assert fenced.metadata["runtime_boot_id"] == "runtime-boot-test"
    assert fenced.metadata["recovery_required"] is True
    assert fenced.trace_status == "incomplete"
    assert store.reconcile_after_runtime_boot("runtime-boot-test") == []


def test_internal_validation_never_echoes_untrusted_body(tmp_path: Path) -> None:
    store, _run = _control_store(tmp_path)
    settings = _settings(tmp_path)
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(
        create_internal_runtime_router(
            store=store,
            shared_secret=settings.shared_secret,
        ),
    )
    body = b'{"boot_id":"provider-test-secret","unexpected":true}'
    path = "/internal/runtime-boots"

    with TestClient(app) as client:
        response = client.post(
            path,
            content=body,
            headers={
                **signed_headers(settings.shared_secret, "POST", path, body),
                "Content-Type": "application/json",
            },
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "Invalid Runtime boot announcement"
    assert "provider-test-secret" not in response.text


def test_storage_batch_acknowledges_processed_not_latest_team_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = replace(
        _settings(tmp_path),
        agentgov_api_base_url="http://agentgov-control.test",
        receipt_retry_backoff_seconds=0.001,
    )
    settings.prepare_writable_directories()
    generation = 0
    receipts: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal generation
        payload = json.loads(request.content)
        if request.url.path == "/internal/runtime-team-inbox":
            generation += 1
            return httpx.Response(
                200,
                json={
                    "run_id": payload["run_id"],
                    "event_id": payload["event_id"],
                    "generation": generation,
                },
            )
        receipts.append(payload)
        return httpx.Response(200, json={"run_id": "run-1", "status": "running"})

    async def committed_state(*_args: object, **_kwargs: object) -> None:
        return None

    async def team_session(*_args: object, **_kwargs: object) -> object:
        return SimpleNamespace(team_id="team-1")

    monkeypatch.setattr(AsyncSQLAlchemyStorage, "update_session_state", committed_state)
    monkeypatch.setattr(AsyncSQLAlchemyStorage, "get_session", team_session)
    transport = httpx.MockTransport(handler)
    bus = AgentGovInMemoryMessageBus(settings, transport=transport)
    root = _context(team_generation=0)
    worker = _context(
        session_id="worker-session",
        role="worker",
        runtime_agent_id="worker-agent",
        team_generation=0,
    )

    async def deliver(payload: dict[str, object], *, drain: bool) -> None:
        token = CURRENT_RUNTIME_CONTEXT.set(worker)
        try:
            await bus.queue_push("agentscope:inbox:leader-session", payload)
        finally:
            CURRENT_RUNTIME_CONTEXT.reset(token)
        if drain:
            await drain_root()

    async def drain_root() -> None:
        token = CURRENT_RUNTIME_CONTEXT.set(root)
        try:
            await bus.queue_drain("agentscope:inbox:leader-session")
        finally:
            CURRENT_RUNTIME_CONTEXT.reset(token)

    async def persist_reply(storage: ProvisionedAsyncSQLAlchemyStorage, reply_id: str) -> None:
        bind_reply_context(root, reply_id)
        await storage.upsert_message(
            RUNTIME_USER_ID,
            "leader-session",
            AssistantMsg(
                "leader",
                reply_id,
                id=reply_id,
                finished_reason=ReplyFinishedReason.COMPLETED,
            ),
        )
        await storage.update_session_state(
            RUNTIME_USER_ID,
            "leader-agent",
            "leader-session",
            AgentState(),
        )

    async def exercise() -> None:
        storage = ProvisionedAsyncSQLAlchemyStorage(
            settings,
            receipt_transport=transport,
            message_bus=bus,
        )
        async with storage:
            await deliver({"kind": "processed"}, drain=True)
            await deliver({"kind": "late"}, drain=False)
            await persist_reply(storage, "reply-one")
            await drain_root()
            await persist_reply(storage, "reply-two")

    asyncio.run(exercise())
    markers = [item for item in receipts if item["type"] == "SESSION_PERSISTED"]
    assert [item["payload"]["team_generation"] for item in markers] == [1, 2]


def test_child_session_is_bound_before_worker_can_be_woken(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    settings = _settings(tmp_path)

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=_context().model_dump(mode="json"))
        return httpx.Response(
            200,
            json=_context(
                session_id="worker-session",
                runtime_agent_id="worker-agent",
                role="worker",
            ).model_dump(mode="json"),
        )

    class _Storage:
        async def get_team(self, user_id: str, team_id: str):
            return SimpleNamespace(session_id="leader-session")

        async def get_session(self, user_id: str, agent_id: str, session_id: str):
            return SimpleNamespace(agent_id="worker-agent")

    result = asyncio.run(
        register_team_child_session(
            _Storage(),
            settings,
            user_id="agentgov-runtime",
            child_session_id="worker-session",
            team_id="team-1",
            transport=httpx.MockTransport(handler),
        ),
    )

    assert result is not None and result.session_id == "worker-session"
    assert [request.url.path for request in requests] == [
        "/internal/runtime-context/leader-session",
        "/internal/runtime-child-sessions",
    ]
    body = json.loads(requests[1].content)
    assert body == {
        "child_runtime_agent_id": "worker-agent",
        "child_session_id": "worker-session",
        "parent_session_id": "leader-session",
        "run_id": "run-1",
        "team_id": "team-1",
    }


_RECEIPT_IDS = itertools.count()


def _control_store(tmp_path: Path) -> tuple[RuntimeRunStore, object]:
    store = RuntimeRunStore(make_session_factory(tmp_path / "control.db"))
    store.bind_agent_version(
        agent_id="business-agent",
        agent_version_id="version-1",
        digest="a" * 64,
        runtime_agent_id="leader-agent",
    )
    store.bind_session(
        session_id="leader-session",
        agent_id="business-agent",
        agent_version_id="version-1",
        runtime_agent_id="leader-agent",
        digest="a" * 64,
    )
    run = store.begin_run(
        session_id="leader-session",
        runtime_agent_id="leader-agent",
        input_value={"role": "user", "content": []},
        alert_id=None,
        case_id=None,
        metadata={},
    )
    store.mark_trigger_started(run.run_id)
    return store, run


def _receipt(
    run: object,
    event_type: str,
    *,
    session_id: str,
    reply_id: str | None,
    payload: dict[str, object],
) -> RuntimeReceipt:
    suffix = next(_RECEIPT_IDS)
    return RuntimeReceipt(
        receipt_id=f"team-receipt-{suffix}",
        event_id=f"team-event-{suffix}",
        session_id=session_id,
        run_id=run.run_id,
        reply_id=reply_id,
        trace_id=run.trace_id,
        type=event_type,
        payload=payload,
    )


def _root_reply(store: RuntimeRunStore, run: object, reply_id: str) -> None:
    store.apply_receipt(
        _receipt(run, "REPLY_START", session_id="leader-session", reply_id=reply_id, payload={}),
    )
    store.apply_receipt(
        _receipt(
            run,
            "REPLY_END",
            session_id="leader-session",
            reply_id=reply_id,
            payload={"finished_reason": "completed"},
        ),
    )
    store.apply_receipt(
        _receipt(
            run,
            "MESSAGE_PERSISTED",
            session_id="leader-session",
            reply_id=reply_id,
            payload={"message_persisted": True, "finished_reason": "completed"},
        ),
    )


def test_team_quiescence_waits_for_worker_and_second_root_batch(tmp_path: Path) -> None:
    """AgentCreate→worker TeamSay→leader wakeup 的两个 root batch 才闭合。"""

    store, run = _control_store(tmp_path)
    _root_reply(store, run, "leader-initial")
    child = store.bind_team_child(
        RuntimeChildSessionRegistration(
            run_id=run.run_id,
            parent_session_id="leader-session",
            child_session_id="worker-session",
            child_runtime_agent_id="worker-agent",
            team_id="team-1",
        ),
    )
    assert child.role == "worker"
    assert child.root_session_id == "leader-session"
    assert child.runtime_agent_id == "worker-agent"

    outbound = RuntimeTeamInboxDelivery(
        event_id="delivery-to-worker",
        run_id=run.run_id,
        source_session_id="leader-session",
        target_session_id="worker-session",
    )
    first = store.record_team_inbox_delivery(outbound)
    duplicate = store.record_team_inbox_delivery(outbound)
    assert first.generation == duplicate.generation == 1
    with pytest.raises(RuntimeStateConflict, match="reused"):
        store.record_team_inbox_delivery(
            outbound.model_copy(update={"target_session_id": "leader-session"}),
        )

    initial_marker = store.apply_receipt(
        _receipt(
            run,
            "SESSION_PERSISTED",
            session_id="leader-session",
            reply_id=None,
            payload={
                "reply_ids": ["leader-initial"],
                "message_count": 1,
                "team_generation": 0,
            },
        ),
    )
    assert initial_marker.status is RunStatus.FINALIZING
    assert initial_marker.pending_child_session_ids == ["worker-session"]
    worker_context = store.runtime_context("worker-session")
    assert worker_context.team_generation == 1
    assert worker_context.run_id == run.run_id

    # Worker reply lifecycle is auditable but must never become a top-level reply.
    for event_type, payload in (
        ("REPLY_START", {}),
        ("REPLY_END", {"finished_reason": "completed"}),
        ("MESSAGE_PERSISTED", {"message_persisted": True, "finished_reason": "completed"}),
    ):
        observed = store.apply_receipt(
            _receipt(
                run,
                event_type,
                session_id="worker-session",
                reply_id="worker-reply",
                payload=payload,
            ),
        )
        assert observed.status is RunStatus.FINALIZING
        assert observed.reply_ids == ["leader-initial"]

    report = store.record_team_inbox_delivery(
        RuntimeTeamInboxDelivery(
            event_id="delivery-to-leader",
            run_id=run.run_id,
            source_session_id="worker-session",
            target_session_id="leader-session",
        ),
    )
    assert report.generation == 2
    worker_done = store.apply_receipt(
        _receipt(
            run,
            "SESSION_PERSISTED",
            session_id="worker-session",
            reply_id=None,
            payload={
                "reply_ids": ["worker-reply"],
                "message_count": 1,
                "team_generation": 1,
            },
        ),
    )
    assert worker_done.status is RunStatus.FINALIZING
    assert worker_done.pending_child_session_ids == []
    assert worker_done.root_persisted_team_generation == 0
    assert worker_done.team_generation == 2

    _root_reply(store, run, "leader-followup")
    terminal = store.apply_receipt(
        _receipt(
            run,
            "SESSION_PERSISTED",
            session_id="leader-session",
            reply_id=None,
            payload={
                "reply_ids": ["leader-followup"],
                "message_count": 1,
                "team_generation": 2,
            },
        ),
    )
    assert terminal.status is RunStatus.SUCCEEDED
    assert terminal.reply_ids == ["leader-initial", "leader-followup"]
    assert terminal.persistence_batch_reply_ids == ["leader-initial", "leader-followup"]
    assert terminal.root_persisted_team_generation == terminal.team_generation == 2
    assert store.get_session("leader-session").active_run_id is None
    assert store.get_session("worker-session").active_run_id is None


def test_public_agentscope_team_chain_reaches_top_level_quiescence(tmp_path: Path) -> None:
    """真实走公共 Team tools/storage/bus，而非直接调用 metadata helper。"""

    settings = replace(
        _settings(tmp_path),
        credential_type="agentgov_team_script",
        credential_id="team-script-credential",
        agentgov_api_base_url="http://agentgov-control.test",
    )
    settings.prepare_writable_directories()
    control_store = RuntimeRunStore(make_session_factory(tmp_path / "control-e2e.db"))
    control_app = FastAPI()
    register_error_handlers(control_app)
    control_app.include_router(
        create_internal_runtime_router(
            store=control_store,
            shared_secret=settings.shared_secret,
        ),
    )
    control_transport = httpx.ASGITransport(app=control_app)
    trace_provider = TracerProvider(id_generator=AgentGovTraceIdGenerator())
    trace_registry = AgentGovRunTraceRegistry(
        trace_provider.get_tracer("agentgov-team-e2e"),
    )
    bus = AgentGovInMemoryMessageBus(
        settings,
        transport=control_transport,
    )
    storage = ProvisionedAsyncSQLAlchemyStorage(
        settings,
        receipt_transport=control_transport,
        trace_registry=trace_registry,
        message_bus=bus,
    )

    async def middlewares(
        _user_id: str,
        _agent_id: str,
        _session_id: str,
        _workspace: object,
    ) -> list[object]:
        return [
            AgentGovTraceContextMiddleware(
                settings,
                transport=control_transport,
                trace_registry=trace_registry,
            ),
            AgentGovReceiptMiddleware(
                settings,
                transport=control_transport,
            ),
        ]

    runtime_app = create_agentscope_app(
        storage=storage,
        message_bus=bus,
        workspace_manager=LocalWorkspaceManager(
            basedir=str(tmp_path / "runtime-workspaces"),
        ),
        knowledge_base_manager=None,
        enable_index_worker=False,
        enable_channel_worker=False,
        enable_scheduler=False,
        channels=[],
        mcp_hubs=[],
        skill_hubs=[],
        extra_credentials=[_TeamScriptCredential],
        extra_agent_middlewares=middlewares,  # type: ignore[arg-type]
    )
    headers = {"X-User-ID": "agentgov-runtime"}
    _TeamScriptModel.actions = []

    with TestClient(runtime_app) as client:
        agent_response = client.post(
            "/agent/",
            headers=headers,
            json={
                "name": "leader",
                "system_prompt": "Delegate the task to one worker.",
            },
        )
        assert agent_response.status_code == 201, agent_response.text
        runtime_agent_id = agent_response.json()["agent_id"]
        session_response = client.post(
            "/sessions/",
            headers=headers,
            json={
                "agent_id": runtime_agent_id,
                "chat_model_config": {
                    "type": "agentgov_team_script",
                    "credential_id": settings.credential_id,
                    "model": "team-script",
                    "parameters": {},
                },
            },
        )
        assert session_response.status_code == 201, session_response.text
        root_session_id = session_response.json()["session_id"]

        control_store.bind_agent_version(
            agent_id="business-agent",
            agent_version_id="version-e2e",
            digest="b" * 64,
            runtime_agent_id=runtime_agent_id,
        )
        control_store.bind_session(
            session_id=root_session_id,
            agent_id="business-agent",
            agent_version_id="version-e2e",
            runtime_agent_id=runtime_agent_id,
            digest="b" * 64,
        )
        run = control_store.begin_run(
            session_id=root_session_id,
            runtime_agent_id=runtime_agent_id,
            input_value={"role": "user", "content": "Delegate this task."},
            alert_id=None,
            case_id=None,
            metadata={},
        )
        control_store.mark_trigger_started(run.run_id)
        chat = client.post(
            "/chat/",
            headers=headers,
            json={
                "agent_id": runtime_agent_id,
                "session_id": root_session_id,
                "input": {
                    "name": "user",
                    "role": "user",
                    "content": [{"type": "text", "text": "Delegate this task."}],
                },
            },
        )
        assert chat.status_code == 200, chat.text

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            terminal = control_store.get_run(run.run_id)
            if terminal.status in {
                RunStatus.SUCCEEDED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
                RunStatus.INTERRUPTED,
            }:
                break
            time.sleep(0.02)
        else:
            pytest.fail(
                "AgentScope public Team chain did not quiesce: "
                f"{control_store.get_run(run.run_id).model_dump(mode='json')}; "
                f"actions={_TeamScriptModel.actions}",
            )

        terminal = control_store.get_run(run.run_id)
        sessions = client.get(
            "/sessions/",
            headers=headers,
            params={"agent_id": runtime_agent_id},
        )
        assert sessions.status_code == 200, sessions.text
        root_view = sessions.json()["sessions"][0]
        assert root_view["team"] is not None
        worker = root_view["team"]["members"][0]
        worker_session_id = worker["session_id"]
        worker_messages = client.get(
            f"/sessions/{worker_session_id}/messages",
            headers=headers,
            params={"agent_id": worker["agent"]["id"]},
        )
        assert worker_messages.status_code == 200, worker_messages.text

    assert terminal.status is RunStatus.SUCCEEDED
    assert terminal.team_generation >= 2
    assert terminal.root_persisted_team_generation == terminal.team_generation
    assert terminal.pending_child_session_ids == []
    assert terminal.reply_ids == terminal.persisted_reply_ids
    assert set(terminal.reply_ids) == set(terminal.persistence_batch_reply_ids)
    assert {"TeamCreate", "AgentCreate", "TeamSay", "leader_final"}.issubset(
        _TeamScriptModel.actions,
    )
    assert worker_session_id != root_session_id
    worker_replies = [message for message in worker_messages.json()["messages"] if message["role"] == "assistant"]
    assert len(worker_replies) == 1
    assert worker_replies[0]["finished_reason"] == "completed"
    assert worker_replies[0]["id"] not in terminal.reply_ids
    assert control_store.get_session(root_session_id).active_run_id is None
    assert control_store.get_session(worker_session_id).active_run_id is None


def test_worker_hitl_is_persisted_against_child_and_resumes_same_root_run(
    tmp_path: Path,
) -> None:
    store, run = _control_store(tmp_path)
    _root_reply(store, run, "leader-initial")
    store.bind_team_child(
        RuntimeChildSessionRegistration(
            run_id=run.run_id,
            parent_session_id="leader-session",
            child_session_id="worker-session",
            child_runtime_agent_id="worker-agent",
            team_id="team-1",
        ),
    )
    store.record_team_inbox_delivery(
        RuntimeTeamInboxDelivery(
            event_id="worker-hitl-delivery",
            run_id=run.run_id,
            source_session_id="leader-session",
            target_session_id="worker-session",
        ),
    )
    store.apply_receipt(
        _receipt(
            run,
            "SESSION_PERSISTED",
            session_id="leader-session",
            reply_id=None,
            payload={
                "reply_ids": ["leader-initial"],
                "message_count": 1,
                "team_generation": 0,
            },
        ),
    )
    tool_call = {
        "type": "tool_call",
        "id": "worker-tool",
        "name": "Read",
        "input": '{"file_path":"worker.txt"}',
        "state": "asking",
        "suggested_rules": [],
    }
    waiting = store.apply_receipt(
        _receipt(
            run,
            "REQUIRE_USER_CONFIRM",
            session_id="worker-session",
            reply_id="worker-reply",
            payload={"tool_calls": [tool_call]},
        ),
    )

    assert waiting.status is RunStatus.WAITING_HUMAN
    assert waiting.reply_ids == ["leader-initial"]
    with store.Session() as db:
        action = db.get(
            RuntimePendingActionModel,
            f"{run.run_id}:worker-reply:worker-tool",
        )
        assert action is not None
        assert action.session_id == "worker-session"
        assert action.status == "pending"

    resumed = store.begin_run(
        session_id="leader-session",
        runtime_agent_id="leader-agent",
        input_value={
            "type": "USER_CONFIRM_RESULT",
            "reply_id": "worker-reply",
            "confirm_results": [
                {
                    "confirmed": True,
                    "tool_call": tool_call,
                },
            ],
        },
        alert_id=None,
        case_id=None,
        metadata={},
        expected_run_id=run.run_id,
    )
    assert resumed.run_id == run.run_id
    assert resumed.status is RunStatus.RUNNING
    assert resumed.reply_ids == ["leader-initial"]
    with store.Session() as db:
        action = db.get(
            RuntimePendingActionModel,
            f"{run.run_id}:worker-reply:worker-tool",
        )
        assert action is not None and action.status == "resolved"
