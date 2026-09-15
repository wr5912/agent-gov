from __future__ import annotations

import itertools

import pytest
from app.runtime.runtime_db import make_session_factory
from app.runtime_gateway.contracts import RuntimeChildSessionRegistration, RuntimeReceipt
from app.runtime_gateway.store import RuntimeRunStore, RuntimeStateConflict
from pydantic import ValidationError

_IDENTITIES = itertools.count()


def _store(tmp_path) -> RuntimeRunStore:
    store = RuntimeRunStore(make_session_factory(tmp_path / "runtime.db"))
    store.bind_agent_version(
        agent_id="agent-a",
        agent_version_id="version-a",
        digest="a" * 64,
        runtime_agent_id="runtime-a",
    )
    store.bind_session(
        session_id="session-a",
        agent_id="agent-a",
        agent_version_id="version-a",
        runtime_agent_id="runtime-a",
        digest="a" * 64,
    )
    return store


def _begin(store: RuntimeRunStore):
    return store.begin_run(
        session_id="session-a",
        runtime_agent_id="runtime-a",
        input_value={"role": "user", "content": [{"type": "text", "text": "identity boundary"}]},
        entities={},
        metadata={},
    )


def _receipt(run, *, session_id: str = "session-a", trace_id: str | None = None) -> RuntimeReceipt:
    identity = next(_IDENTITIES)
    return RuntimeReceipt(
        receipt_id=f"receipt-identity-{identity}",
        event_id=f"event-identity-{identity}",
        session_id=session_id,
        run_id=run.run_id,
        reply_id="reply-a",
        type="REPLY_START",
        payload={},
        trace_id=trace_id or run.trace_id,
    )


@pytest.mark.parametrize("missing", ("run_id", "trace_id"))
def test_runtime_receipt_requires_exact_run_and_trace_identity(missing: str) -> None:
    payload = {
        "receipt_id": "receipt-required-identity",
        "event_id": "event-required-identity",
        "session_id": "session-a",
        "run_id": "run-a",
        "reply_id": "reply-a",
        "type": "REPLY_START",
        "payload": {},
        "trace_id": "a" * 32,
    }
    payload.pop(missing)

    with pytest.raises(ValidationError, match=missing):
        RuntimeReceipt.model_validate(payload)


def test_delayed_receipt_from_terminal_run_cannot_enter_reused_session_fence(tmp_path) -> None:
    store = _store(tmp_path)
    old_run = _begin(store)
    store.mark_trigger_started(old_run.run_id)
    store.fail_trigger(old_run.run_id, error={"type": "transport"})
    new_run = _begin(store)

    with pytest.raises(RuntimeStateConflict, match="does not own this session fence"):
        store.apply_receipt(_receipt(old_run))

    assert store.get_run(new_run.run_id).status.value == "queued"
    assert store.get_run(old_run.run_id).status.value == "failed"


def test_child_receipt_requires_root_run_and_trace_identity(tmp_path) -> None:
    store = _store(tmp_path)
    run = _begin(store)
    store.mark_trigger_started(run.run_id)
    store.bind_team_child(
        RuntimeChildSessionRegistration(
            run_id=run.run_id,
            parent_session_id="session-a",
            child_session_id="worker-session",
            child_runtime_agent_id="worker-agent",
            team_id="team-a",
        ),
    )

    wrong_run = _receipt(run, session_id="worker-session").model_copy(
        update={"run_id": "run-from-another-team"},
    )
    with pytest.raises(RuntimeStateConflict, match="does not own this session fence"):
        store.apply_receipt(wrong_run)

    with pytest.raises(RuntimeStateConflict, match="another trace"):
        store.apply_receipt(
            _receipt(
                run,
                session_id="worker-session",
                trace_id="b" * 32,
            ),
        )
