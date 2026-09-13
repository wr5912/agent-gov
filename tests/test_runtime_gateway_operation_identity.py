from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pytest
from app.runtime.json_types import JsonObject
from app.runtime.runtime_db import make_session_factory
from app.runtime_gateway.client import RuntimeJsonResponse
from app.runtime_gateway.contracts import (
    AgentRunResponse,
    ConfirmationScope,
    RunStatus,
    RuntimeChildSessionRegistration,
    RuntimeReceipt,
)
from app.runtime_gateway.models import AgentRunModel, RuntimeChatOperationModel
from app.runtime_gateway.operation_identity import initial_operation_key
from app.runtime_gateway.run_trigger import (
    RuntimeChatTriggerResult,
    admit_and_trigger_chat,
)
from app.runtime_gateway.store import (
    RuntimeInputRejected,
    RuntimeObjectNotFound,
    RuntimeRunStore,
    RuntimeStateConflict,
)
from sqlalchemy import text

from runtime_hitl_test_utils import fingerprinted_hitl_payload


def _store(tmp_path) -> RuntimeRunStore:
    return RuntimeRunStore(make_session_factory(tmp_path / "runtime.db"))


def _bind_version(
    store: RuntimeRunStore,
    *,
    version_id: str = "version-a",
    runtime_agent_id: str = "runtime-a",
    digest: str = "a" * 64,
) -> None:
    store.bind_agent_version(
        agent_id="agent-a",
        agent_version_id=version_id,
        digest=digest,
        runtime_agent_id=runtime_agent_id,
    )


def _bind_session(
    store: RuntimeRunStore,
    *,
    session_id: str = "session-a",
    version_id: str = "version-a",
    runtime_agent_id: str = "runtime-a",
    digest: str = "a" * 64,
) -> None:
    store.bind_session(
        session_id=session_id,
        agent_id="agent-a",
        agent_version_id=version_id,
        runtime_agent_id=runtime_agent_id,
        digest=digest,
    )


def _admit(
    store: RuntimeRunStore,
    *,
    operation_id: str,
    input_text: str = "hello",
    metadata: dict[str, object] | None = None,
):
    request_metadata = {"client_operation_id": "untrusted", "client": "ui"} if metadata is None else metadata
    return store.admit_run(
        session_id="session-a",
        runtime_agent_id="runtime-a",
        input_value={"role": "user", "content": [{"type": "text", "text": input_text}]},
        alert_id="alert-a",
        case_id="case-a",
        metadata=request_metadata,
        client_operation_id=operation_id,
    )


def test_initial_operation_replays_durable_response_after_store_reopen(tmp_path) -> None:
    store = _store(tmp_path)
    _bind_version(store)
    _bind_session(store)
    created = _admit(store, operation_id="operation-replay")
    assert created.should_trigger_upstream is True
    assert created.operation_key == initial_operation_key("operation-replay")
    store.record_chat_operation_response(
        created.operation_key,
        run_id=created.run.run_id,
        response_status=202,
        response_body=b'{"status":"submitted"}',
        response_content_type="application/json",
        response_headers={"x-runtime": "exact"},
    )

    reopened = _store(tmp_path)
    replay = _admit(reopened, operation_id="operation-replay")

    assert replay.should_trigger_upstream is False
    assert replay.run.run_id == created.run.run_id
    assert replay.replay_response is not None
    assert replay.replay_response.status_code == 202
    assert replay.replay_response.body == b'{"status":"submitted"}'
    assert replay.replay_response.headers == {"x-runtime": "exact"}
    assert replay.run.client_operation_id == "operation-replay"
    assert replay.run.metadata == {"client": "ui"}
    with pytest.raises(RuntimeStateConflict, match="immutable chat request"):
        _admit(reopened, operation_id="operation-replay", input_text="changed")


def test_initial_operation_fingerprint_canonicalizes_governed_metadata(tmp_path) -> None:
    store = _store(tmp_path)
    _bind_version(store)
    _bind_session(store)
    created = _admit(
        store,
        operation_id="operation-metadata",
        metadata={
            "nested": {"second": 2, "first": 1},
            "client": "ui",
            "runtime_boot_id": "untrusted-boot",
            "runtime_boot_version": "untrusted-version",
        },
    )

    reopened = _store(tmp_path)
    replay = _admit(
        reopened,
        operation_id="operation-metadata",
        metadata={
            "client_operation_id": "untrusted-operation",
            "client": "ui",
            "nested": {"first": 1, "second": 2},
            "runtime_boot_id": "different-untrusted-boot",
            "runtime_boot_version": "different-untrusted-version",
        },
    )

    assert replay.should_trigger_upstream is False
    assert replay.run.run_id == created.run.run_id
    assert replay.run.metadata == {
        "client": "ui",
        "nested": {"first": 1, "second": 2},
    }
    with pytest.raises(RuntimeStateConflict, match="immutable chat request"):
        _admit(
            reopened,
            operation_id="operation-metadata",
            metadata={"client": "another-client", "nested": {"first": 1, "second": 2}},
        )


@pytest.mark.parametrize(
    ("input_value", "metadata"),
    [
        ({"role": "user", "content": [float("nan")]}, {}),
        ({"role": "user", "content": []}, {"score": float("inf")}),
    ],
)
def test_initial_operation_rejects_nonfinite_json_identity(
    tmp_path,
    input_value: object,
    metadata: JsonObject,
) -> None:
    store = _store(tmp_path)
    _bind_version(store)
    _bind_session(store)

    with pytest.raises(RuntimeInputRejected, match="canonical JSON"):
        store.admit_run(
            session_id="session-a",
            runtime_agent_id="runtime-a",
            input_value=input_value,
            alert_id=None,
            case_id=None,
            metadata=metadata,
            client_operation_id="operation-nonfinite",
        )

    with store.Session() as db:
        assert db.query(AgentRunModel).count() == 0
        assert db.query(RuntimeChatOperationModel).count() == 0


def test_concurrent_initial_operation_has_one_admission_and_exact_identity(tmp_path) -> None:
    store = _store(tmp_path)
    _bind_version(store)
    _bind_session(store)

    with ThreadPoolExecutor(max_workers=2) as pool:
        admissions = list(pool.map(lambda _index: _admit(store, operation_id="operation-concurrent"), range(2)))

    assert {item.run.run_id for item in admissions} == {admissions[0].run.run_id}
    assert sorted(item.should_trigger_upstream for item in admissions) == [False, True]
    assert admissions[0].run.client_operation_id == "operation-concurrent"
    assert "client_operation_id" not in admissions[0].run.metadata

    _bind_version(
        store,
        version_id="version-b",
        runtime_agent_id="runtime-b",
        digest="b" * 64,
    )
    _bind_session(
        store,
        session_id="session-b",
        version_id="version-b",
        runtime_agent_id="runtime-b",
        digest="b" * 64,
    )
    with pytest.raises(RuntimeStateConflict, match="another immutable chat request"):
        store.admit_run(
            session_id="session-b",
            runtime_agent_id="runtime-b",
            input_value={"role": "user", "content": [{"type": "text", "text": "hello"}]},
            alert_id="alert-a",
            case_id="case-a",
            metadata={},
            client_operation_id="operation-concurrent",
        )


def test_hitl_continuation_uses_expected_run_without_rebinding_initial_operation(tmp_path) -> None:
    store = _store(tmp_path)
    _bind_version(store)
    _bind_session(store)
    run = _admit(store, operation_id="initial-operation").run
    store.mark_trigger_started(run.run_id)
    tool_call = {
        "type": "tool_call",
        "id": "tool-a",
        "name": "Read",
        "input": "{}",
        "state": "asking",
    }
    store.apply_receipt(
        RuntimeReceipt(
            receipt_id="receipt-hitl",
            event_id="event-hitl",
            session_id="session-a",
            run_id=run.run_id,
            reply_id="reply-hitl",
            type="REQUIRE_USER_CONFIRM",
            payload=fingerprinted_hitl_payload([tool_call]),
            trace_id=run.trace_id,
        ),
    )

    resumed = store.admit_run(
        session_id="session-a",
        runtime_agent_id="runtime-a",
        input_value={
            "type": "USER_CONFIRM_RESULT",
            "reply_id": "reply-hitl",
            "confirm_results": [{"confirmed": True, "tool_call": tool_call}],
        },
        alert_id=None,
        case_id=None,
        metadata={},
        client_operation_id=f"detached:session-a:{run.run_id}",
        expected_run_id=run.run_id,
    )

    assert resumed.should_trigger_upstream is True
    assert resumed.run.run_id == run.run_id
    assert resumed.run.client_operation_id == "initial-operation"
    resolved = store.run_for_client_operation(
        session_id="session-a",
        client_operation_id=f"detached:session-a:{run.run_id}",
    )
    assert resolved.run_id == run.run_id
    assert resolved.client_operation_id == "initial-operation"


class _ContinuationClient:
    def __init__(self, *, response_session_id: str) -> None:
        self.response_session_id = response_session_id
        self.requests: list[dict[str, object]] = []

    async def request_json(self, method: str, path: str, **kwargs) -> RuntimeJsonResponse:
        assert (method, path) == ("POST", "/chat/")
        self.requests.append(kwargs["json"])
        return RuntimeJsonResponse(
            status_code=202,
            headers={"content-type": "application/json", "x-runtime": "continuation"},
            body={"status": "started", "session_id": self.response_session_id},
        )


@dataclass(frozen=True)
class _ChildHitlScenario:
    store: RuntimeRunStore
    run: AgentRunResponse
    input_value: JsonObject
    client: _ContinuationClient
    initial_operation_key: str


def _child_hitl_scenario(tmp_path) -> _ChildHitlScenario:
    store = _store(tmp_path)
    _bind_version(store)
    _bind_session(store)
    initial = _admit(store, operation_id="shared-turn-operation")
    assert initial.operation_key is not None
    store.record_chat_operation_response(
        initial.operation_key,
        run_id=initial.run.run_id,
        response_status=202,
        response_body=b'{"status":"started","session_id":"session-a"}',
        response_content_type="application/json",
        response_headers={"x-runtime": "initial"},
    )
    run = store.get_run(initial.run.run_id)
    store.bind_team_child(
        RuntimeChildSessionRegistration(
            run_id=run.run_id,
            parent_session_id="session-a",
            child_session_id="worker-session",
            child_runtime_agent_id="worker-agent",
            team_id="team-a",
        ),
    )
    tool_call: JsonObject = {
        "type": "tool_call",
        "id": "worker-tool",
        "name": "Read",
        "input": '{"file_path":"worker.txt"}',
        "state": "asking",
    }
    store.apply_receipt(
        RuntimeReceipt(
            receipt_id="receipt-worker-hitl",
            event_id="event-worker-hitl",
            session_id="worker-session",
            run_id=run.run_id,
            reply_id="worker-reply",
            type="REQUIRE_USER_CONFIRM",
            payload=fingerprinted_hitl_payload([tool_call]),
            trace_id=run.trace_id,
        ),
    )
    input_value: JsonObject = {
        "type": "USER_CONFIRM_RESULT",
        "reply_id": "worker-reply",
        "confirm_results": [{"confirmed": True, "tool_call": tool_call}],
    }
    client = _ContinuationClient(response_session_id="worker-session")
    return _ChildHitlScenario(
        store=store,
        run=run,
        input_value=input_value,
        client=client,
        initial_operation_key=initial.operation_key,
    )


async def _trigger_child_hitl(
    scenario: _ChildHitlScenario,
    current_store: RuntimeRunStore,
    *,
    scope: ConfirmationScope = ConfirmationScope.ONCE,
    value: object | None = None,
) -> RuntimeChatTriggerResult:
    return await admit_and_trigger_chat(
        client=scenario.client,  # type: ignore[arg-type]
        store=current_store,
        session_id="session-a",
        runtime_agent_id="runtime-a",
        input_value=scenario.input_value if value is None else value,
        alert_id="alert-a",
        case_id="case-a",
        metadata={"client": "ui"},
        client_operation_id="shared-turn-operation",
        confirmation_scope=scope,
        expected_run_id=scenario.run.run_id,
    )


def _assert_child_hitl_ledgers(
    scenario: _ChildHitlScenario,
    reopened: RuntimeRunStore,
    first: RuntimeChatTriggerResult,
) -> None:
    with reopened.Session() as db:
        operations = list(db.query(RuntimeChatOperationModel).all())
    assert {item.operation_kind for item in operations} == {
        "initial",
        "user_confirmation",
    }
    continuation = next(item for item in operations if item.operation_kind == "user_confirmation")
    assert continuation.run_id == scenario.run.run_id
    assert continuation.root_session_id == "session-a"
    assert continuation.action_session_id == "worker-session"
    assert continuation.reply_id == "worker-reply"
    assert continuation.tool_call_ids_json == ["worker-tool"]
    assert continuation.confirmation_scope == "once"
    assert continuation.response_body == first.body
    initial = reopened.chat_operation_for_key(scenario.initial_operation_key)
    assert initial.response_headers_json == {"x-runtime": "initial"}


def _assert_child_hitl_rebinding_fails(
    scenario: _ChildHitlScenario,
    reopened: RuntimeRunStore,
) -> None:
    tool_call = scenario.input_value["confirm_results"][0]["tool_call"]
    assert isinstance(tool_call, dict)
    changed_tool = {
        **scenario.input_value,
        "confirm_results": [
            {
                "confirmed": True,
                "tool_call": {**tool_call, "input": '{"file_path":"other.txt"}'},
            },
        ],
    }
    with pytest.raises(RuntimeStateConflict, match="immutable continuation request"):
        asyncio.run(_trigger_child_hitl(scenario, reopened, value=changed_tool))
    with pytest.raises(RuntimeStateConflict, match="immutable continuation request"):
        asyncio.run(
            _trigger_child_hitl(scenario, reopened, scope=ConfirmationScope.RUN),
        )
    with pytest.raises(RuntimeStateConflict, match="expected_run_id"):
        reopened.admit_run(
            session_id="session-a",
            runtime_agent_id="runtime-a",
            input_value=scenario.input_value,
            alert_id="alert-a",
            case_id="case-a",
            metadata={"client": "ui"},
            client_operation_id="shared-turn-operation",
            expected_run_id="run-other",
        )


def test_child_hitl_operation_replays_its_own_response_and_rejects_rebinding(
    tmp_path,
) -> None:
    scenario = _child_hitl_scenario(tmp_path)

    first = asyncio.run(_trigger_child_hitl(scenario, scenario.store))
    reopened = _store(tmp_path)
    replay = asyncio.run(_trigger_child_hitl(scenario, reopened))

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.body == first.body
    assert replay.status_code == first.status_code == 202
    assert replay.headers == first.headers
    assert scenario.client.requests == [
        {
            "agent_id": "runtime-a",
            "session_id": "session-a",
            "input": scenario.input_value,
        },
    ]
    assert b'"session_id":"session-a"' in replay.body
    assert b'"worker_session_id":"worker-session"' in replay.body
    _assert_child_hitl_ledgers(scenario, reopened, first)
    _assert_child_hitl_rebinding_fails(scenario, reopened)


def test_completed_continuation_replays_while_a_newer_run_is_active(tmp_path) -> None:
    scenario = _child_hitl_scenario(tmp_path)
    first = asyncio.run(_trigger_child_hitl(scenario, scenario.store))
    scenario.store.fail_trigger(
        scenario.run.run_id,
        error={"type": "test_terminal"},
    )
    newer = _admit(scenario.store, operation_id="newer-initial-operation")
    assert newer.run.run_id != scenario.run.run_id

    replay = asyncio.run(_trigger_child_hitl(scenario, scenario.store))

    assert replay.replayed is True
    assert replay.run.run_id == scenario.run.run_id
    assert replay.body == first.body
    assert len(scenario.client.requests) == 1


@pytest.mark.parametrize("nonfinite_in_input", [False, True])
def test_hitl_operation_rejects_nonfinite_canonical_identity(
    tmp_path,
    nonfinite_in_input: bool,
) -> None:
    scenario = _child_hitl_scenario(tmp_path)
    input_value = dict(scenario.input_value)
    metadata: JsonObject = {"client": "ui"}
    if nonfinite_in_input:
        input_value["nonfinite"] = float("nan")
    else:
        metadata["nonfinite"] = float("inf")

    with pytest.raises(RuntimeInputRejected, match="canonical JSON"):
        scenario.store.admit_run(
            session_id="session-a",
            runtime_agent_id="runtime-a",
            input_value=input_value,
            alert_id="alert-a",
            case_id="case-a",
            metadata=metadata,
            client_operation_id="continuation-nonfinite",
            expected_run_id=scenario.run.run_id,
        )

    with scenario.store.Session() as db:
        assert db.query(RuntimeChatOperationModel).filter_by(operation_kind="user_confirmation").count() == 0


def test_same_reply_and_kind_use_distinct_action_operation_keys(tmp_path) -> None:
    store = _store(tmp_path)
    _bind_version(store)
    _bind_session(store)
    run = _admit(store, operation_id="initial-multi-action").run
    store.mark_trigger_started(run.run_id)
    calls = [
        {
            "type": "tool_call",
            "id": tool_id,
            "name": "Read",
            "input": f'{{"file_path":"{tool_id}.txt"}}',
            "state": "asking",
        }
        for tool_id in ("tool-a", "tool-b")
    ]
    store.apply_receipt(
        RuntimeReceipt(
            receipt_id="receipt-multi-action",
            event_id="event-multi-action",
            session_id="session-a",
            run_id=run.run_id,
            reply_id="reply-shared",
            type="REQUIRE_USER_CONFIRM",
            payload=fingerprinted_hitl_payload(calls),
            trace_id=run.trace_id,
        ),
    )

    operation_keys: list[str] = []
    for index, tool_call in enumerate(calls):
        if index:
            store.apply_receipt(
                RuntimeReceipt(
                    receipt_id="receipt-multi-action-second",
                    event_id="event-multi-action-second",
                    session_id="session-a",
                    run_id=run.run_id,
                    reply_id="reply-shared",
                    type="REQUIRE_USER_CONFIRM",
                    payload=fingerprinted_hitl_payload([tool_call]),
                    trace_id=run.trace_id,
                ),
            )
        admission = store.admit_run(
            session_id="session-a",
            runtime_agent_id="runtime-a",
            input_value={
                "type": "USER_CONFIRM_RESULT",
                "reply_id": "reply-shared",
                "confirm_results": [{"confirmed": False, "tool_call": tool_call}],
            },
            alert_id=None,
            case_id=None,
            metadata={},
            client_operation_id="shared-continuation-id",
            expected_run_id=run.run_id,
        )
        assert admission.operation_key is not None
        operation_keys.append(admission.operation_key)

    assert len(set(operation_keys)) == 2
    assert [store.chat_operation_for_key(key).tool_call_ids_json for key in operation_keys] == [["tool-a"], ["tool-b"]]


def test_recovery_quiescent_reset_breaks_the_consecutive_idle_sequence(tmp_path) -> None:
    store = _store(tmp_path)
    _bind_version(store)
    _bind_session(store)
    run = _admit(store, operation_id="recovery-sequence").run
    store.reconcile_after_restart()

    assert store.note_recovery_quiescent(run.run_id) == 1
    assert store.reset_recovery_quiescent(run.run_id).metadata["recovery_quiescent_observations"] == 0
    assert store.note_recovery_quiescent(run.run_id) == 1


def test_exact_operation_lookup_rejects_missing_and_corrupt_duplicate_rows(tmp_path) -> None:
    store = _store(tmp_path)
    _bind_version(store)
    _bind_session(store)
    first = _admit(store, operation_id="operation-lookup").run

    assert (
        store.run_for_client_operation(
            session_id="session-a",
            client_operation_id="operation-lookup",
        ).run_id
        == first.run_id
    )
    with pytest.raises(RuntimeObjectNotFound):
        store.run_for_client_operation(
            session_id="session-a",
            client_operation_id="operation-missing",
        )

    store.fail_trigger(first.run_id, error={"type": "test"})
    with store.Session.begin() as db:
        db.execute(text("DROP INDEX ux_agent_runs_client_operation"))
        db.add(
            AgentRunModel(
                run_id="run-corrupt-duplicate",
                session_id="session-a",
                agent_id="agent-a",
                agent_version_id="version-a",
                runtime_agent_id="runtime-a",
                harness_digest="a" * 64,
                client_operation_id="operation-lookup",
                input_fingerprint="f" * 64,
                status=RunStatus.FAILED.value,
                metadata_json={},
            ),
        )
    with pytest.raises(RuntimeStateConflict, match="multiple AgentGov runs"):
        store.run_for_client_operation(
            session_id="session-a",
            client_operation_id="operation-lookup",
        )


def test_pending_actions_project_root_and_worker_then_expire_at_terminal(tmp_path) -> None:
    store = _store(tmp_path)
    _bind_version(store)
    _bind_session(store)
    run = _admit(store, operation_id="operation-pending-actions").run
    store.mark_trigger_started(run.run_id)
    store.bind_team_child(
        RuntimeChildSessionRegistration(
            run_id=run.run_id,
            parent_session_id="session-a",
            child_session_id="worker-session",
            child_runtime_agent_id="worker-agent",
            team_id="team-1",
        ),
    )

    for suffix, session_id in (("root", "session-a"), ("worker", "worker-session")):
        store.apply_receipt(
            RuntimeReceipt(
                receipt_id=f"receipt-{suffix}",
                event_id=f"event-{suffix}",
                session_id=session_id,
                run_id=run.run_id,
                reply_id=f"reply-{suffix}",
                type="REQUIRE_USER_CONFIRM",
                payload=fingerprinted_hitl_payload(
                    [
                        {
                            "type": "tool_call",
                            "id": f"tool-{suffix}",
                            "name": "Read",
                            "input": f'{{"file_path":"{suffix}.txt"}}',
                            "state": "asking",
                            "metadata": {"api_key": "must-not-project"},
                        },
                    ],
                ),
                trace_id=run.trace_id,
            ),
        )

    actions = store.pending_actions_for_run(run.run_id)
    assert {item.session_id for item in actions} == {"session-a", "worker-session"}
    action_payloads = [item.model_dump(mode="json") for item in actions]
    assert all("tool_call" not in item for item in action_payloads)
    assert all(len(item["tool_call_sha256"]) == 64 for item in action_payloads)
    assert "must-not-project" not in str(action_payloads)
    store.fail_trigger(run.run_id, error={"type": "test"})
    assert store.pending_actions_for_run(run.run_id) == []
