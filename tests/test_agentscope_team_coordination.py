from __future__ import annotations

import asyncio
import itertools
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest
from agentscope_runtime.context_registry import RuntimeContext
from agentscope_runtime.receipt_middleware import CURRENT_RUNTIME_CONTEXT
from agentscope_runtime.settings import RuntimeSettings
from agentscope_runtime.signing import signed_headers
from agentscope_runtime.team_coordination import (
    AgentGovInMemoryMessageBus,
    RuntimeBootCoordinationMiddleware,
    RuntimeBootCoordinator,
)
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
from app.version import APP_VERSION
from fastapi import FastAPI
from fastapi.testclient import TestClient

from runtime_hitl_test_utils import fingerprinted_hitl_payload
from runtime_loopback import serve_loopback


def _settings(tmp_path: Path) -> RuntimeSettings:
    return RuntimeSettings(
        shared_secret="s" * 32,
        provider_api_key="provider-key",
        data_dir=tmp_path / "data",
        business_agents_root=tmp_path / "business",
        candidates_root=tmp_path / "candidates",
        workspaces_root=tmp_path / "workspaces",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'data' / 'agentscope.db'}",
        runtime_version=APP_VERSION,
    )


def _control_app(store: RuntimeRunStore, secret: str) -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(
        create_internal_runtime_router(store=store, shared_secret=secret),
    )
    return app


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


def test_cross_session_inbox_records_fence_before_queueing(tmp_path: Path) -> None:
    store, run = _control_store(tmp_path)
    store.bind_team_child(
        RuntimeChildSessionRegistration(
            run_id=run.run_id,
            parent_session_id="leader-session",
            child_session_id="worker-session",
            child_runtime_agent_id="worker-agent",
            team_id="team-1",
        )
    )

    with serve_loopback(_control_app(store, _settings(tmp_path).shared_secret)) as control_url:
        bus = AgentGovInMemoryMessageBus(
            replace(_settings(tmp_path), agentgov_api_base_url=control_url),
        )

        async def exercise() -> list[tuple[str, dict]]:
            token = CURRENT_RUNTIME_CONTEXT.set(_context(run_id=run.run_id))
            try:
                await bus.queue_push(
                    "agentscope:inbox:worker-session",
                    {"secret": "must-not-leak"},
                )
            finally:
                CURRENT_RUNTIME_CONTEXT.reset(token)
            return await bus.queue_drain("agentscope:inbox:worker-session")

        queued = asyncio.run(exercise())
    assert len(queued) == 1
    assert queued[0][1] == {"secret": "must-not-leak"}
    assert store.get_run(run.run_id).team_generation == 1
    assert "must-not-leak" not in store.trace_expectations(run.run_id).model_dump_json()


def test_same_session_and_unmanaged_inbox_do_not_emit_team_event(tmp_path: Path) -> None:
    store, run = _control_store(tmp_path)

    with serve_loopback(_control_app(store, _settings(tmp_path).shared_secret)) as control_url:
        bus = AgentGovInMemoryMessageBus(
            replace(_settings(tmp_path), agentgov_api_base_url=control_url),
        )

        async def exercise() -> None:
            await bus.queue_push("agentscope:inbox:unmanaged", {})
            token = CURRENT_RUNTIME_CONTEXT.set(_context(run_id=run.run_id))
            try:
                await bus.queue_push("agentscope:inbox:leader-session", {})
            finally:
                CURRENT_RUNTIME_CONTEXT.reset(token)

        asyncio.run(exercise())
    assert store.get_run(run.run_id).team_generation == 0


def test_team_generation_advances_only_when_target_session_actually_drains(
    tmp_path: Path,
) -> None:
    store, run = _control_store(tmp_path)
    store.bind_team_child(
        RuntimeChildSessionRegistration(
            run_id=run.run_id,
            parent_session_id="leader-session",
            child_session_id="worker-session",
            child_runtime_agent_id="worker-agent",
            team_id="team-1",
        )
    )

    root = _context(run_id=run.run_id)
    worker = _context(
        run_id=run.run_id,
        session_id="worker-session",
        role="worker",
        runtime_agent_id="worker-agent",
        team_generation=2,
    )

    with serve_loopback(_control_app(store, _settings(tmp_path).shared_secret)) as control_url:
        bus = AgentGovInMemoryMessageBus(
            replace(_settings(tmp_path), agentgov_api_base_url=control_url),
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
    assert store.get_run(run.run_id).team_generation == 2


def test_runtime_boot_notification_fences_active_run_and_is_idempotent(
    tmp_path: Path,
) -> None:
    store, run = _control_store(tmp_path)
    with serve_loopback(_control_app(store, _settings(tmp_path).shared_secret)) as control_url:
        settings = replace(
            _settings(tmp_path),
            agentgov_api_base_url=control_url,
        )

        async def announce() -> None:
            coordinator = RuntimeBootCoordinator(
                settings,
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
    assert fenced.metadata["runtime_boot_version"] == APP_VERSION
    assert fenced.metadata["recovery_required"] is True
    assert fenced.trace_status == "incomplete"
    assert store.reconcile_after_runtime_boot("runtime-boot-test", APP_VERSION) == []
    with pytest.raises(RuntimeStateConflict, match="cannot be rebound"):
        store.reconcile_after_runtime_boot("runtime-boot-test", "0.0.0-stale")


@pytest.mark.parametrize(
    "body",
    [
        b'{"boot_id":"runtime-boot-old"}',
        b'{"boot_id":"runtime-boot-empty","runtime_version":""}',
        b'{"boot_id":"runtime-boot-blank","runtime_version":" "}',
        b'{"boot_id":"runtime-boot-stale","runtime_version":"0.0.0-stale"}',
    ],
    ids=("old-contract", "empty-version", "blank-version", "mismatched-version"),
)
def test_runtime_boot_rejects_unbound_version_without_recovery(
    tmp_path: Path,
    body: bytes,
) -> None:
    store, run = _control_store(tmp_path)
    settings = _settings(tmp_path)
    path = "/internal/runtime-boots"

    with TestClient(_control_app(store, settings.shared_secret)) as client:
        response = client.post(
            path,
            content=body,
            headers={
                **signed_headers(settings.shared_secret, "POST", path, body),
                "Content-Type": "application/json",
            },
        )

    assert response.status_code == 409
    assert "runtime_boot_id" not in store.get_run(run.run_id).metadata


def test_mismatched_boot_ack_keeps_runtime_chat_gate_closed(tmp_path: Path) -> None:
    boot_id = "runtime-boot-mismatched-ack"
    second_announcement_seen = Event()
    announcement_attempts = itertools.count()
    control = FastAPI()

    @control.post("/internal/runtime-boots")
    async def acknowledge_wrong_version() -> dict[str, object]:
        if next(announcement_attempts) == 1:
            second_announcement_seen.set()
        return {
            "boot_id": boot_id,
            "runtime_version": "0.0.0-stale",
            "recovery_run_ids": [],
        }

    with serve_loopback(control) as control_url:
        settings = replace(
            _settings(tmp_path),
            agentgov_api_base_url=control_url,
            receipt_retry_backoff_seconds=0.01,
        )
        coordinator = RuntimeBootCoordinator(settings, boot_id=boot_id)
        runtime = FastAPI()
        runtime.add_middleware(
            RuntimeBootCoordinationMiddleware,
            coordinator=coordinator,
        )

        @runtime.post("/chat/")
        async def chat() -> dict[str, str]:
            return {"status": "started"}

        with TestClient(runtime) as client:
            assert second_announcement_seen.wait(timeout=1)
            response = client.post("/chat/", json={})

    assert response.status_code == 503
    assert response.json()["error_code"] == "RUNTIME_BOOT_PENDING"
    assert coordinator.acknowledged is False


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


def test_team_interruption_receipts_are_exact_per_session_and_settle_once(
    tmp_path: Path,
) -> None:
    store, run = _control_store(tmp_path)
    store.bind_team_child(
        RuntimeChildSessionRegistration(
            run_id=run.run_id,
            parent_session_id="leader-session",
            child_session_id="worker-session",
            child_runtime_agent_id="worker-agent",
            team_id="team-1",
        ),
    )
    store.mark_cancel_requested(run.run_id)
    root_receipt = _receipt(
        run,
        "RUN_INTERRUPTED",
        session_id="leader-session",
        reply_id=None,
        payload={},
    )
    child_receipt = _receipt(
        run,
        "RUN_INTERRUPTED",
        session_id="worker-session",
        reply_id=None,
        payload={},
    )

    store.apply_receipt(root_receipt)
    store.apply_receipt(child_receipt)
    duplicate = store.apply_receipt(child_receipt)

    assert duplicate.status is RunStatus.RUNNING
    assert duplicate.metadata["runtime_interrupted_session_ids"] == [
        "leader-session",
        "worker-session",
    ]
    assert (
        store.settle_after_quiescent_observation(
            run.run_id,
            error="first idle observation",
        )
        is None
    )
    terminal = store.settle_after_quiescent_observation(
        run.run_id,
        error="second idle observation",
    )
    assert terminal is not None
    assert terminal.status is RunStatus.CANCELLED
    assert terminal.terminal_reason == "interrupted"
    assert terminal.trace_status == "pending"
    expectations = store.trace_expectations(run.run_id)
    assert expectations.control_integrity_complete is True
    assert expectations.interrupted_before_reply is True
    assert [child.session_id for child in expectations.team_children] == [
        "worker-session",
    ]
    assert store.get_session("leader-session").active_run_id is None
    assert store.get_session("worker-session").active_run_id is None


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
            payload=fingerprinted_hitl_payload([tool_call]),
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
        client_operation_id="worker-continuation",
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
