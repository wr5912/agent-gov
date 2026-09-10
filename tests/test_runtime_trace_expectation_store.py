from __future__ import annotations

import itertools

from app.runtime.runtime_db import make_session_factory
from app.runtime_gateway.contracts import (
    AgentRunResponse,
    RuntimeChildSessionRegistration,
    RuntimeReceipt,
    RuntimeTeamInboxDelivery,
)
from app.runtime_gateway.models import RuntimeSessionBindingModel
from app.runtime_gateway.store import RuntimeRunStore

_IDS = itertools.count()


def _receipt(
    run,
    event_type: str,
    *,
    session_id: str,
    reply_id: str | None,
    payload: dict[str, object],
) -> RuntimeReceipt:
    suffix = next(_IDS)
    return RuntimeReceipt(
        receipt_id=f"receipt-{suffix}",
        event_id=f"event-{suffix}",
        session_id=session_id,
        run_id=run.run_id,
        reply_id=reply_id,
        type=event_type,
        payload=payload,
        trace_id=run.trace_id,
    )


def _store(tmp_path) -> RuntimeRunStore:
    store = RuntimeRunStore(make_session_factory(tmp_path / "runtime.db"))
    store.bind_agent_version(
        agent_id="agent-a",
        agent_version_id="version-a",
        digest="a" * 64,
        runtime_agent_id="leader-runtime",
    )
    store.bind_session(
        session_id="root-session",
        agent_id="agent-a",
        agent_version_id="version-a",
        runtime_agent_id="leader-runtime",
        digest="a" * 64,
    )
    return store


def _run_with_worker_facts(store: RuntimeRunStore) -> AgentRunResponse:
    run = store.begin_run(
        session_id="root-session",
        runtime_agent_id="leader-runtime",
        input_value={"role": "user", "content": []},
        alert_id=None,
        case_id=None,
        metadata={},
    )
    store.mark_trigger_started(run.run_id)
    store.apply_receipt(
        _receipt(
            run,
            "REPLY_START",
            session_id="root-session",
            reply_id="root-reply",
            payload={},
        ),
    )
    store.bind_team_child(
        RuntimeChildSessionRegistration(
            run_id=run.run_id,
            parent_session_id="root-session",
            child_session_id="worker-session",
            child_runtime_agent_id="worker-runtime",
            team_id="team-1",
        ),
    )
    store.record_team_inbox_delivery(
        RuntimeTeamInboxDelivery(
            event_id="delivery-1",
            run_id=run.run_id,
            source_session_id="root-session",
            target_session_id="worker-session",
        ),
    )
    store.apply_receipt(
        _receipt(
            run,
            "TOOL_RESULT_END",
            session_id="worker-session",
            reply_id="worker-reply",
            payload={"tool_call_id": "local-call", "state": "success"},
        ),
    )
    return run


def _record_external_result(store: RuntimeRunStore, run: AgentRunResponse) -> None:
    external_call = {
        "type": "tool_call",
        "id": "external-call",
        "name": "BrowserAction",
        "input": "{}",
        "state": "asking",
    }
    store.apply_receipt(
        _receipt(
            run,
            "REQUIRE_EXTERNAL_EXECUTION",
            session_id="worker-session",
            reply_id="worker-reply",
            payload={"tool_calls": [external_call]},
        ),
    )
    store.begin_run(
        session_id="root-session",
        runtime_agent_id="leader-runtime",
        input_value={
            "type": "EXTERNAL_EXECUTION_RESULT",
            "reply_id": "worker-reply",
            "execution_results": [
                {
                    "type": "tool_result",
                    "id": "external-call",
                    "name": "BrowserAction",
                    "output": "redacted by trace exporter",
                    "state": "success",
                },
            ],
        },
        alert_id=None,
        case_id=None,
        metadata={},
        expected_run_id=run.run_id,
    )
    store.apply_receipt(
        _receipt(
            run,
            "TOOL_RESULT_END",
            session_id="worker-session",
            reply_id="worker-reply",
            payload={"tool_call_id": "external-call", "state": "success"},
        ),
    )


def test_trace_expectations_are_derived_from_receipts_actions_and_team_ledger(tmp_path) -> None:
    store = _store(tmp_path)
    run = _run_with_worker_facts(store)
    _record_external_result(store, run)
    expectations = store.trace_expectations(run.run_id)
    assert expectations.control_integrity_complete is True
    assert expectations.root_reply_ids == ["root-reply"]
    assert [item.model_dump() for item in expectations.team_children] == [
        {"session_id": "worker-session", "runtime_agent_id": "worker-runtime"},
    ]
    assert [item.model_dump() for item in expectations.tool_results] == [
        {
            "session_id": "worker-session",
            "reply_id": "worker-reply",
            "tool_call_id": "external-call",
            "state": None,
            "source": "external_action",
        },
        {
            "session_id": "worker-session",
            "reply_id": "worker-reply",
            "tool_call_id": "local-call",
            "state": "success",
            "source": "tool_result_receipt",
        },
    ]
    assert [item.model_dump() for item in expectations.actions] == [
        {
            "session_id": "worker-session",
            "reply_id": "worker-reply",
            "tool_call_id": "external-call",
            "kind": "external",
            "status": "resolved",
        },
    ]

    with store.Session.begin() as db:
        child = db.get(RuntimeSessionBindingModel, "worker-session")
        assert child is not None
        child.team_id = None
    assert store.trace_expectations(run.run_id).control_integrity_complete is False
