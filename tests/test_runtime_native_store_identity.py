from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from app.runtime.runtime_db import make_session_factory
from app.runtime_gateway.contracts import RuntimeChildSessionRegistration, RuntimeReceipt
from app.runtime_gateway.models import AgentRunModel, RuntimeChatOperationModel, RuntimeSessionBindingModel
from app.runtime_gateway.native_chat_input import native_operation_key
from app.runtime_gateway.operation_identity import RuntimeChatOperationKind
from app.runtime_gateway.store import RuntimeObjectNotFound, RuntimeRunStore, RuntimeStateConflict

from runtime_hitl_test_utils import fingerprinted_hitl_payload


def _store(tmp_path) -> RuntimeRunStore:
    return RuntimeRunStore(make_session_factory(tmp_path / "runtime.sqlite3"))


def _bind(store: RuntimeRunStore, session_id: str) -> None:
    store.bind_agent_version(agent_id="agent-a", agent_version_id="version-a", digest="a" * 64, runtime_agent_id="runtime-a")
    store.bind_session(
        session_id=session_id,
        agent_id="agent-a",
        agent_version_id="version-a",
        runtime_agent_id="runtime-a",
        digest="a" * 64,
    )


def _admit(store: RuntimeRunStore, *, session_id: str = "session-a", input_value: object, metadata: dict[str, object] | None = None):
    return store.admit_run(
        session_id=session_id,
        runtime_agent_id="runtime-a",
        input_value=input_value,
        entities={},
        metadata=metadata or {},
    )


def _message(message_id: str | None, text: str = "hello") -> dict[str, object]:
    value: dict[str, object] = {"role": "user", "content": [{"type": "text", "text": text}]}
    if message_id is not None:
        value["id"] = message_id
    return value


def test_native_explicit_id_replays_durable_response_and_rejects_changed_body(tmp_path) -> None:
    store = _store(tmp_path)
    _bind(store, "session-a")
    first = _admit(store, input_value=_message("msg-a"), metadata={"origin": "ui", "nested": {"second": 2, "first": 1}})
    assert first.should_trigger_upstream
    assert first.operation_key == native_operation_key(
        runtime_agent_id="runtime-a", session_id="session-a", operation_kind=RuntimeChatOperationKind.INITIAL, input_ids=("msg-a",)
    )
    store.record_chat_operation_response(
        first.operation_key,
        run_id=first.run.run_id,
        response_status=202,
        response_body=b'{"status":"started"}',
        response_content_type="application/json",
        response_headers={"x-runtime": "exact"},
    )
    reopened = _store(tmp_path)
    replay = _admit(reopened, input_value=_message("msg-a"), metadata={"nested": {"first": 1, "second": 2}, "origin": "ui"})
    assert not replay.should_trigger_upstream
    assert replay.run.run_id == first.run.run_id
    assert replay.replay_response is not None and replay.replay_response.body == b'{"status":"started"}'
    assert replay.run.metadata == {"origin": "ui", "nested": {"first": 1, "second": 2}}
    resolved = reopened.run_for_input_identity(
        runtime_agent_id="runtime-a", session_id="session-a", operation_kind=RuntimeChatOperationKind.INITIAL, input_ids=("msg-a",)
    )
    assert resolved.run_id == first.run.run_id
    with reopened.Session() as db:
        run = db.get(AgentRunModel, first.run.run_id)
        operation = db.get(RuntimeChatOperationModel, first.operation_key)
        assert run is not None and run.legacy_client_operation_id is None and run.legacy_input_fingerprint is None
        assert operation is not None and operation.legacy_client_operation_id is None
    with pytest.raises(RuntimeStateConflict, match="immutable chat request"):
        _admit(reopened, input_value=_message("msg-a", "changed"), metadata={"origin": "ui", "nested": {"first": 1, "second": 2}})


def test_native_id_is_scoped_to_request_session_and_unkeyed_input_has_no_lookup(tmp_path) -> None:
    store = _store(tmp_path)
    _bind(store, "session-a")
    _bind(store, "session-b")
    _bind(store, "session-c")
    _bind(store, "session-d")
    first = _admit(store, session_id="session-a", input_value=_message("same-id"))
    second = _admit(store, session_id="session-b", input_value=_message("same-id"))
    assert first.run.run_id != second.run.run_id
    assert first.operation_key != second.operation_key
    unkeyed = _admit(store, session_id="session-c", input_value=_message(None))
    assert unkeyed.should_trigger_upstream and unkeyed.operation_key.startswith("native-unkeyed:")
    empty_input = _admit(store, session_id="session-d", input_value=None)
    assert empty_input.should_trigger_upstream and empty_input.operation_key.startswith("native-unkeyed:")
    with pytest.raises(RuntimeObjectNotFound):
        store.run_for_input_identity(runtime_agent_id="runtime-a", session_id="session-a", operation_kind=RuntimeChatOperationKind.INITIAL, input_ids=())
    with pytest.raises(RuntimeObjectNotFound):
        store.run_for_input_identity(
            runtime_agent_id="runtime-a", session_id="session-b", operation_kind=RuntimeChatOperationKind.INITIAL, input_ids=("foreign",)
        )


def test_native_explicit_id_concurrent_admission_creates_one_run(tmp_path) -> None:
    store = _store(tmp_path)
    _bind(store, "session-a")
    with ThreadPoolExecutor(max_workers=2) as pool:
        admissions = list(pool.map(lambda _index: _admit(store, input_value=_message("concurrent")), range(2)))
    assert len({item.run.run_id for item in admissions}) == 1
    assert sorted(item.should_trigger_upstream for item in admissions) == [False, True]


def test_native_hitl_reply_and_tool_identity_selects_run_without_expected_run_id(tmp_path) -> None:
    store = _store(tmp_path)
    _bind(store, "session-a")
    first = _admit(store, input_value=_message("initial"))
    store.mark_trigger_started(first.run.run_id)
    tool_call = {"type": "tool_call", "id": "tool-a", "name": "Read", "input": "{}", "state": "asking"}
    store.apply_receipt(
        RuntimeReceipt(
            receipt_id="receipt-a",
            event_id="event-a",
            session_id="session-a",
            run_id=first.run.run_id,
            reply_id="reply-a",
            type="REQUIRE_USER_CONFIRM",
            payload=fingerprinted_hitl_payload([tool_call]),
            trace_id=first.run.trace_id,
        )
    )
    event = {"id": "decision-a", "type": "USER_CONFIRM_RESULT", "reply_id": "reply-a", "confirm_results": [{"confirmed": True, "tool_call": tool_call}]}
    resumed = _admit(store, input_value=event)
    assert resumed.should_trigger_upstream and resumed.run.run_id == first.run.run_id
    assert resumed.operation_key != first.operation_key
    store.record_chat_operation_response(
        resumed.operation_key,
        run_id=first.run.run_id,
        response_status=202,
        response_body=json.dumps({"status": "started"}).encode(),
        response_content_type="application/json",
        response_headers={},
    )
    replay = _admit(_store(tmp_path), input_value=event)
    assert not replay.should_trigger_upstream and replay.run.run_id == first.run.run_id
    assert replay.replay_response is not None and replay.replay_response.status_code == 202
    with pytest.raises(RuntimeStateConflict, match="immutable continuation request"):
        _admit(_store(tmp_path), input_value={**event, "confirm_results": [{"confirmed": False, "tool_call": tool_call}]})
    store.fail_trigger(first.run.run_id, error={"type": "test_terminal"})
    newer = _admit(store, input_value=_message("new-turn"))
    assert newer.run.run_id != first.run.run_id
    completed_replay = _admit(_store(tmp_path), input_value=event)
    assert not completed_replay.should_trigger_upstream and completed_replay.run.run_id == first.run.run_id


def test_native_child_hitl_uses_worker_binding_and_keeps_root_run_identity(tmp_path) -> None:
    store = _store(tmp_path)
    _bind(store, "root-session")
    first = _admit(store, session_id="root-session", input_value=_message("root-message"))
    store.mark_trigger_started(first.run.run_id)
    store.bind_team_child(
        RuntimeChildSessionRegistration(
            run_id=first.run.run_id,
            parent_session_id="root-session",
            child_session_id="worker-session",
            child_runtime_agent_id="worker-agent",
            team_id="team-a",
        )
    )
    tool_call = {"type": "tool_call", "id": "worker-tool", "name": "Read", "input": "{}", "state": "asking"}
    store.apply_receipt(
        RuntimeReceipt(
            receipt_id="worker-receipt",
            event_id="worker-event",
            session_id="worker-session",
            run_id=first.run.run_id,
            reply_id="worker-reply",
            type="REQUIRE_USER_CONFIRM",
            payload=fingerprinted_hitl_payload([tool_call]),
            trace_id=first.run.trace_id,
        )
    )
    event = {
        "id": "worker-decision",
        "type": "USER_CONFIRM_RESULT",
        "reply_id": "worker-reply",
        "confirm_results": [{"confirmed": True, "tool_call": tool_call}],
    }
    resumed = store.admit_run(
        session_id="worker-session",
        runtime_agent_id="worker-agent",
        input_value=event,
        entities={},
        metadata={},
    )
    assert resumed.run.run_id == first.run.run_id
    assert resumed.operation_key == native_operation_key(
        runtime_agent_id="worker-agent",
        session_id="worker-session",
        operation_kind=RuntimeChatOperationKind.USER_CONFIRMATION,
        input_ids=("worker-decision",),
    )
    operation = store.chat_operation_for_key(resumed.operation_key)
    assert operation.root_session_id == "root-session"
    assert operation.action_session_id == "worker-session"
    assert operation.runtime_agent_id == "worker-agent"
    assert (
        store.run_for_input_identity(
            runtime_agent_id="worker-agent",
            session_id="worker-session",
            operation_kind=RuntimeChatOperationKind.USER_CONFIRMATION,
            input_ids=("worker-decision",),
        ).run_id
        == first.run.run_id
    )


@pytest.mark.parametrize(
    ("action_session_id", "request_session_id", "request_runtime_agent_id"),
    [
        ("worker-session", "root-session", "runtime-a"),
        ("root-session", "worker-session", "worker-agent"),
    ],
)
def test_native_hitl_rejects_root_worker_session_substitution(
    tmp_path,
    action_session_id: str,
    request_session_id: str,
    request_runtime_agent_id: str,
) -> None:
    store = _store(tmp_path)
    _bind(store, "root-session")
    first = _admit(
        store,
        session_id="root-session",
        input_value=_message("root-worker-substitution"),
    )
    store.mark_trigger_started(first.run.run_id)
    store.bind_team_child(
        RuntimeChildSessionRegistration(
            run_id=first.run.run_id,
            parent_session_id="root-session",
            child_session_id="worker-session",
            child_runtime_agent_id="worker-agent",
            team_id="team-a",
        ),
    )
    tool_call = {
        "type": "tool_call",
        "id": "target-tool",
        "name": "Read",
        "input": "{}",
        "state": "asking",
    }
    store.apply_receipt(
        RuntimeReceipt(
            receipt_id="target-receipt",
            event_id="target-event",
            session_id=action_session_id,
            run_id=first.run.run_id,
            reply_id="target-reply",
            type="REQUIRE_USER_CONFIRM",
            payload=fingerprinted_hitl_payload([tool_call]),
            trace_id=first.run.trace_id,
        ),
    )
    event = {
        "id": "wrong-session-decision",
        "type": "USER_CONFIRM_RESULT",
        "reply_id": "target-reply",
        "confirm_results": [{"confirmed": True, "tool_call": tool_call}],
    }

    with pytest.raises(RuntimeStateConflict, match="exact pending action Session"):
        store.admit_run(
            session_id=request_session_id,
            runtime_agent_id=request_runtime_agent_id,
            input_value=event,
            entities={},
            metadata={},
        )

    with store.Session() as db:
        operations = db.query(RuntimeChatOperationModel).all()
        assert len(operations) == 1
        assert operations[0].operation_kind == RuntimeChatOperationKind.INITIAL.value


def test_native_hitl_rejects_legacy_operation_replay_from_wrong_session(tmp_path) -> None:
    store = _store(tmp_path)
    _bind(store, "root-session")
    first = _admit(store, session_id="root-session", input_value=_message("legacy-worker-operation"))
    store.mark_trigger_started(first.run.run_id)
    store.bind_team_child(
        RuntimeChildSessionRegistration(
            run_id=first.run.run_id,
            parent_session_id="root-session",
            child_session_id="worker-session",
            child_runtime_agent_id="worker-agent",
            team_id="team-a",
        ),
    )
    tool_call = {
        "type": "tool_call",
        "id": "legacy-worker-tool",
        "name": "Read",
        "input": "{}",
        "state": "asking",
    }
    store.apply_receipt(
        RuntimeReceipt(
            receipt_id="legacy-worker-receipt",
            event_id="legacy-worker-event",
            session_id="worker-session",
            run_id=first.run.run_id,
            reply_id="legacy-worker-reply",
            type="REQUIRE_USER_CONFIRM",
            payload=fingerprinted_hitl_payload([tool_call]),
            trace_id=first.run.trace_id,
        ),
    )
    event = {
        "id": "legacy-worker-decision",
        "type": "USER_CONFIRM_RESULT",
        "reply_id": "legacy-worker-reply",
        "confirm_results": [{"confirmed": True, "tool_call": tool_call}],
    }
    admitted = store.admit_run(
        session_id="worker-session",
        runtime_agent_id="worker-agent",
        input_value=event,
        entities={},
        metadata={},
    )
    root_key = native_operation_key(
        runtime_agent_id="runtime-a",
        session_id="root-session",
        operation_kind=RuntimeChatOperationKind.USER_CONFIRMATION,
        input_ids=("legacy-worker-decision",),
    )
    with store.Session.begin() as db:
        operation = db.get(RuntimeChatOperationModel, admitted.operation_key)
        assert operation is not None
        operation.operation_key = root_key
        operation.runtime_agent_id = "runtime-a"

    with pytest.raises(RuntimeStateConflict, match="pending action Session"):
        store.admit_run(
            session_id="root-session",
            runtime_agent_id="runtime-a",
            input_value=event,
            entities={},
            metadata={},
        )


def test_native_lookup_rejects_corrupt_operation_to_run_binding(tmp_path) -> None:
    store = _store(tmp_path)
    _bind(store, "session-a")
    first = _admit(store, input_value=_message("lookup-a"))
    with store.Session.begin() as db:
        operation = db.get(RuntimeChatOperationModel, first.operation_key)
        assert operation is not None
        operation.root_session_id = "foreign-session"
    with pytest.raises(RuntimeStateConflict, match="operation does not match"):
        store.run_for_input_identity(
            runtime_agent_id="runtime-a",
            session_id="session-a",
            operation_kind=RuntimeChatOperationKind.INITIAL,
            input_ids=("lookup-a",),
        )
    with pytest.raises(RuntimeStateConflict, match="immutable chat request"):
        _admit(store, input_value=_message("lookup-a"))


@pytest.mark.parametrize("location", ["input", "metadata"])
def test_native_store_rejects_nonfinite_json_without_persisting_run(tmp_path, location: str) -> None:
    store = _store(tmp_path)
    _bind(store, "session-a")
    input_value = _message("bad-json")
    metadata: dict[str, object] = {}
    if location == "input":
        input_value["nonfinite"] = float("nan")
    else:
        metadata["nonfinite"] = float("inf")
    with pytest.raises(Exception, match="canonical JSON"):
        _admit(store, input_value=input_value, metadata=metadata)
    with store.Session() as db:
        assert db.query(AgentRunModel).count() == 0
        assert db.query(RuntimeChatOperationModel).count() == 0


def test_native_run_pending_actions_project_root_and_worker_without_tool_arguments(tmp_path) -> None:
    store = _store(tmp_path)
    _bind(store, "root-session")
    first = _admit(store, session_id="root-session", input_value=_message("pending-actions"))
    store.mark_trigger_started(first.run.run_id)
    store.bind_team_child(
        RuntimeChildSessionRegistration(
            run_id=first.run.run_id,
            parent_session_id="root-session",
            child_session_id="worker-session",
            child_runtime_agent_id="worker-agent",
            team_id="team-a",
        )
    )
    for suffix, session_id in (("root", "root-session"), ("worker", "worker-session")):
        tool_call = {"type": "tool_call", "id": f"tool-{suffix}", "name": "Read", "input": "{}", "state": "asking"}
        store.apply_receipt(
            RuntimeReceipt(
                receipt_id=f"receipt-{suffix}",
                event_id=f"event-{suffix}",
                session_id=session_id,
                run_id=first.run.run_id,
                reply_id=f"reply-{suffix}",
                type="REQUIRE_USER_CONFIRM",
                payload=fingerprinted_hitl_payload([tool_call]),
                trace_id=first.run.trace_id,
            )
        )
    actions = store.pending_actions_for_run(first.run.run_id)
    assert {item.session_id for item in actions} == {"root-session", "worker-session"}
    assert {item.session_id: item.runtime_agent_id for item in actions} == {
        "root-session": "runtime-a",
        "worker-session": "worker-agent",
    }
    payloads = [item.model_dump(mode="json") for item in actions]
    assert all("tool_call" not in payload for payload in payloads)
    assert all(len(payload["tool_call_sha256"]) == 64 for payload in payloads)
    store.fail_trigger(first.run.run_id, error={"type": "test_terminal"})
    assert store.pending_actions_for_run(first.run.run_id) == []


def test_pending_action_projection_rejects_worker_binding_outside_governed_run(tmp_path) -> None:
    store = _store(tmp_path)
    _bind(store, "root-session")
    first = _admit(store, session_id="root-session", input_value=_message("pending-worker-binding"))
    store.mark_trigger_started(first.run.run_id)
    store.bind_team_child(
        RuntimeChildSessionRegistration(
            run_id=first.run.run_id,
            parent_session_id="root-session",
            child_session_id="worker-session",
            child_runtime_agent_id="worker-agent",
            team_id="team-a",
        )
    )
    tool_call = {"type": "tool_call", "id": "worker-tool", "name": "Read", "input": "{}", "state": "asking"}
    store.apply_receipt(
        RuntimeReceipt(
            receipt_id="worker-receipt",
            event_id="worker-event",
            session_id="worker-session",
            run_id=first.run.run_id,
            reply_id="worker-reply",
            type="REQUIRE_USER_CONFIRM",
            payload=fingerprinted_hitl_payload([tool_call]),
            trace_id=first.run.trace_id,
        )
    )
    with store.Session.begin() as db:
        binding = db.get(RuntimeSessionBindingModel, "worker-session")
        assert binding is not None
        binding.root_session_id = "foreign-root"

    with pytest.raises(RuntimeStateConflict, match="Session binding"):
        store.pending_actions_for_run(first.run.run_id)


def test_native_run_recovery_idle_sequence_resets_after_nonidle_observation(tmp_path) -> None:
    store = _store(tmp_path)
    _bind(store, "session-a")
    first = _admit(store, input_value=_message("recovery"))
    store.reconcile_after_restart()
    assert store.note_recovery_quiescent(first.run.run_id) == 1
    assert store.reset_recovery_quiescent(first.run.run_id).metadata["recovery_quiescent_observations"] == 0
    assert store.note_recovery_quiescent(first.run.run_id) == 1


def test_native_same_reply_accepts_distinct_explicit_decision_ids_for_distinct_actions(tmp_path) -> None:
    store = _store(tmp_path)
    _bind(store, "session-a")
    first = _admit(store, input_value=_message("initial-multi-action"))
    store.mark_trigger_started(first.run.run_id)
    calls = [{"type": "tool_call", "id": tool_id, "name": "Read", "input": "{}", "state": "asking"} for tool_id in ("tool-a", "tool-b")]
    store.apply_receipt(
        RuntimeReceipt(
            receipt_id="receipt-multi",
            event_id="event-multi",
            session_id="session-a",
            run_id=first.run.run_id,
            reply_id="reply-shared",
            type="REQUIRE_USER_CONFIRM",
            payload=fingerprinted_hitl_payload(calls),
            trace_id=first.run.trace_id,
        )
    )
    keys: list[str] = []
    for index, tool_call in enumerate(calls):
        if index:
            store.apply_receipt(
                RuntimeReceipt(
                    receipt_id="receipt-multi-second",
                    event_id="event-multi-second",
                    session_id="session-a",
                    run_id=first.run.run_id,
                    reply_id="reply-shared",
                    type="REQUIRE_USER_CONFIRM",
                    payload=fingerprinted_hitl_payload([tool_call]),
                    trace_id=first.run.trace_id,
                )
            )
        admission = _admit(
            store,
            input_value={
                "id": f"decision-{index}",
                "type": "USER_CONFIRM_RESULT",
                "reply_id": "reply-shared",
                "confirm_results": [{"confirmed": False, "tool_call": tool_call}],
            },
        )
        assert admission.run.run_id == first.run.run_id
        keys.append(admission.operation_key)
    assert len(set(keys)) == 2
    assert [store.chat_operation_for_key(key).tool_call_ids_json for key in keys] == [["tool-a"], ["tool-b"]]
