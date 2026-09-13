from __future__ import annotations

import pytest
from app.runtime.runtime_db import make_session_factory
from app.runtime_gateway.contracts import (
    RuntimeChildSessionRegistration,
    RuntimeTeamInboxDelivery,
)
from app.runtime_gateway.store import RuntimeRunStore, RuntimeStateConflict


def test_cancellation_freezes_team_topology_but_keeps_idempotent_replays(tmp_path) -> None:
    store = RuntimeRunStore(make_session_factory(tmp_path / "runtime.db"))
    store.bind_agent_version(
        agent_id="agent-a",
        agent_version_id="version-a",
        digest="a" * 64,
        runtime_agent_id="runtime-a",
    )
    store.bind_session(
        session_id="leader-session",
        agent_id="agent-a",
        agent_version_id="version-a",
        runtime_agent_id="runtime-a",
        digest="a" * 64,
    )
    run = store.begin_run(
        session_id="leader-session",
        runtime_agent_id="runtime-a",
        input_value={"role": "user", "content": [{"type": "text", "text": "start"}]},
        alert_id=None,
        case_id=None,
        metadata={},
    )
    store.mark_trigger_started(run.run_id)

    existing_child = RuntimeChildSessionRegistration(
        run_id=run.run_id,
        parent_session_id="leader-session",
        child_session_id="worker-existing",
        child_runtime_agent_id="runtime-worker-existing",
        team_id="team-a",
    )
    store.bind_team_child(existing_child)
    delivery = RuntimeTeamInboxDelivery(
        event_id="delivery-before-cancel",
        run_id=run.run_id,
        source_session_id="leader-session",
        target_session_id="worker-existing",
    )
    first_ack = store.record_team_inbox_delivery(delivery)

    store.mark_cancel_requested(run.run_id)

    assert store.bind_team_child(existing_child).run_id == run.run_id
    assert store.record_team_inbox_delivery(delivery) == first_ack
    with pytest.raises(RuntimeStateConflict, match="cannot bind after cancellation"):
        store.bind_team_child(
            RuntimeChildSessionRegistration(
                run_id=run.run_id,
                parent_session_id="leader-session",
                child_session_id="worker-late",
                child_runtime_agent_id="runtime-worker-late",
                team_id="team-a",
            ),
        )
    with pytest.raises(RuntimeStateConflict, match="cannot expand a run"):
        store.record_team_inbox_delivery(
            delivery.model_copy(update={"event_id": "delivery-after-cancel"}),
        )

    assert {item.session_id for item in store.active_session_bindings(run.run_id)} == {
        "leader-session",
        "worker-existing",
    }
