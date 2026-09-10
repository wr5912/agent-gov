from __future__ import annotations

import itertools
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from app.runtime.runtime_db import make_session_factory
from app.runtime_gateway.contracts import ConfirmationScope, RunStatus, RuntimeReceipt
from app.runtime_gateway.models import RuntimePendingActionModel
from app.runtime_gateway.store import RuntimeInputRejected, RuntimeRunStore, RuntimeStateConflict


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
        idempotency_key=None,
    )
    return store


def _message(text: str = "hello") -> dict[str, object]:
    return {
        "name": "user",
        "role": "user",
        "content": [{"type": "text", "text": text}],
    }


_IDS = itertools.count()


def _receipt(
    run,
    event_type: str,
    *,
    reply_id: str | None = "reply-a",
    payload: dict[str, object] | None = None,
    trace_id: str | None = None,
) -> RuntimeReceipt:
    suffix = next(_IDS)
    return RuntimeReceipt(
        receipt_id=f"receipt-{suffix}",
        event_id=f"event-{suffix}",
        session_id=run.session_id,
        run_id=run.run_id,
        reply_id=reply_id,
        type=event_type,
        payload=payload or {},
        trace_id=trace_id or run.trace_id,
    )


def _begin(store: RuntimeRunStore):
    return store.begin_run(
        session_id="session-a",
        runtime_agent_id="runtime-a",
        input_value=_message(),
        alert_id=None,
        case_id=None,
        metadata={},
    )


def _session_persisted(run, *reply_ids: str) -> RuntimeReceipt:
    return _receipt(
        run,
        "SESSION_PERSISTED",
        reply_id=None,
        payload={
            "reply_ids": list(reply_ids),
            "message_count": len(reply_ids),
            "team_generation": run.team_generation,
        },
    )


def test_reply_end_waits_for_canonical_message_before_releasing_fence(tmp_path) -> None:
    store = _store(tmp_path)
    run = _begin(store)
    assert len(run.trace_id or "") == 32
    store.mark_trigger_started(run.run_id)
    store.apply_receipt(_receipt(run, "REPLY_START"))
    end = _receipt(run, "REPLY_END", payload={"finished_reason": "completed", "error": None})
    assert store.apply_receipt(end).status is RunStatus.FINALIZING

    with pytest.raises(RuntimeStateConflict, match="already has active run"):
        _begin(store)

    persisted = _receipt(
        run,
        "MESSAGE_PERSISTED",
        payload={
            "message_persisted": True,
            "finished_reason": "completed",
            "error": None,
            "trace_complete": False,
        },
    )
    assert store.apply_receipt(persisted).status is RunStatus.FINALIZING
    terminal = store.apply_receipt(_session_persisted(run, "reply-a"))
    assert terminal.status is RunStatus.SUCCEEDED
    assert terminal.trace_status == "pending"
    assert store.apply_receipt(persisted).status is RunStatus.SUCCEEDED
    assert _begin(store).run_id != run.run_id


def test_persisted_message_must_match_observed_reply_end(tmp_path) -> None:
    store = _store(tmp_path)
    run = _begin(store)
    store.mark_trigger_started(run.run_id)
    store.apply_receipt(_receipt(run, "REPLY_START"))
    store.apply_receipt(_receipt(run, "REPLY_END", payload={"finished_reason": "completed"}))

    bad = _receipt(
        run,
        "MESSAGE_PERSISTED",
        payload={"message_persisted": True, "finished_reason": "error"},
    )
    with pytest.raises(RuntimeStateConflict, match="does not match"):
        store.apply_receipt(bad)
    assert store.get_run(run.run_id).status is RunStatus.FINALIZING

    good = bad.model_copy(
        update={
            "payload": {"message_persisted": True, "finished_reason": "completed"},
        },
    )
    assert store.apply_receipt(good).status is RunStatus.FINALIZING
    assert store.apply_receipt(_session_persisted(run, "reply-a")).status is RunStatus.SUCCEEDED


def test_canonical_error_message_closes_setup_failure_without_middleware_reply_end(tmp_path) -> None:
    store = _store(tmp_path)
    run = _begin(store)
    store.mark_trigger_started(run.run_id)
    persisted = store.apply_receipt(
        _receipt(
            run,
            "MESSAGE_PERSISTED",
            payload={
                "message_persisted": True,
                "finished_reason": "error",
                "error": {
                    "type": "setup",
                    "message": "assembly failed provider-test-secret",
                },
            },
        )
    )
    assert persisted.status is RunStatus.RUNNING
    terminal = store.apply_receipt(_session_persisted(run, "reply-a"))
    assert terminal.status is RunStatus.INTERRUPTED
    assert terminal.terminal_reason == "observation_incomplete"
    assert terminal.trace_status == "incomplete"
    assert terminal.error == {"type": "setup"}
    assert "provider-test-secret" not in str(terminal.model_dump(mode="json"))


def test_user_confirmation_reuses_run_and_rejects_kind_tamper_and_rules(tmp_path) -> None:
    store = _store(tmp_path)
    run = _begin(store)
    store.mark_trigger_started(run.run_id)
    store.apply_receipt(_receipt(run, "REPLY_START"))
    tool_call = {
        "type": "tool_call",
        "id": "tool-a",
        "name": "Write",
        "input": '{"path":"out.txt","text":"ok"}',
        "state": "asking",
        "suggested_rules": [],
    }
    waiting = store.apply_receipt(_receipt(run, "REQUIRE_USER_CONFIRM", payload={"tool_calls": [tool_call]}))
    assert waiting.status is RunStatus.WAITING_HUMAN

    external = {
        "type": "EXTERNAL_EXECUTION_RESULT",
        "reply_id": "reply-a",
        "execution_results": [{"type": "tool_result", "id": "tool-a", "name": "Write", "output": "ok", "state": "success"}],
    }
    with pytest.raises(RuntimeStateConflict, match="type does not match"):
        store.begin_run(
            session_id="session-a",
            runtime_agent_id="runtime-a",
            input_value=external,
            alert_id=None,
            case_id=None,
            metadata={},
            expected_run_id=run.run_id,
        )

    def confirmation(*, input_value: str = '{ "text": "ok", "path": "out.txt" }', rules=None):
        return {
            "type": "USER_CONFIRM_RESULT",
            "reply_id": "reply-a",
            "confirm_results": [
                {
                    "confirmed": True,
                    "tool_call": {**tool_call, "input": input_value},
                    "rules": rules,
                }
            ],
        }

    with pytest.raises(RuntimeInputRejected, match="cannot be submitted"):
        store.begin_run(
            session_id="session-a",
            runtime_agent_id="runtime-a",
            input_value=confirmation(rules=[{"behavior": "allow"}]),
            alert_id=None,
            case_id=None,
            metadata={},
            expected_run_id=run.run_id,
        )
    with pytest.raises(RuntimeStateConflict, match="cannot be modified"):
        store.begin_run(
            session_id="session-a",
            runtime_agent_id="runtime-a",
            input_value=confirmation(input_value='{"path":"other"}'),
            alert_id=None,
            case_id=None,
            metadata={},
            expected_run_id=run.run_id,
        )

    resumed = store.begin_run(
        session_id="session-a",
        runtime_agent_id="runtime-a",
        input_value=confirmation(),
        alert_id=None,
        case_id=None,
        metadata={},
        expected_run_id=run.run_id,
    )
    assert resumed.run_id == run.run_id
    assert resumed.trace_id == run.trace_id
    assert resumed.status is RunStatus.RUNNING


def test_allow_for_run_uses_only_persisted_suggestions_and_expires_at_terminal(tmp_path) -> None:
    store = _store(tmp_path)
    run = _begin(store)
    store.mark_trigger_started(run.run_id)
    store.apply_receipt(_receipt(run, "REPLY_START"))
    tool_call = {
        "type": "tool_call",
        "id": "tool-run",
        "name": "Read",
        "input": '{"file_path":"reports/a.txt"}',
        "state": "asking",
        "suggested_rules": [
            {
                "tool_name": "Read",
                "rule_content": "reports/**",
                "behavior": "allow",
                "source": "suggested",
            },
        ],
    }
    store.apply_receipt(_receipt(run, "REQUIRE_USER_CONFIRM", payload={"tool_calls": [tool_call]}))
    decision = {
        "type": "USER_CONFIRM_RESULT",
        "reply_id": "reply-a",
        "confirm_results": [{"confirmed": True, "tool_call": deepcopy(tool_call)}],
    }

    resumed = store.begin_run(
        session_id="session-a",
        runtime_agent_id="runtime-a",
        input_value=decision,
        alert_id=None,
        case_id=None,
        metadata={},
        confirmation_scope=ConfirmationScope.RUN,
        expected_run_id=run.run_id,
    )

    expected_rule = {
        "tool_name": "Read",
        "rule_content": "reports/**",
        "behavior": "allow",
        "source": f"agentgov-run:{run.run_id}",
    }
    assert decision["confirm_results"][0]["rules"] == [expected_rule]
    with store.Session() as db:
        action = db.get(RuntimePendingActionModel, f"{run.run_id}:reply-a:tool-run")
        assert action is not None
        assert action.run_rules_json == [expected_rule]
        assert action.run_rules_granted_at is not None
        assert action.run_rules_expired_at is None

    terminal = store.fail_trigger(resumed.run_id, error={"type": "test", "message": "stop"})
    assert terminal.status is RunStatus.FAILED
    with store.Session() as db:
        action = db.get(RuntimePendingActionModel, f"{run.run_id}:reply-a:tool-run")
        assert action is not None and action.run_rules_expired_at is not None


def test_allow_for_run_fails_closed_without_safe_suggestions_and_under_concurrent_replay(tmp_path) -> None:
    store = _store(tmp_path)
    run = _begin(store)
    store.mark_trigger_started(run.run_id)
    store.apply_receipt(_receipt(run, "REPLY_START"))
    tool_call = {
        "type": "tool_call",
        "id": "tool-run",
        "name": "Read",
        "input": '{"file_path":"reports/a.txt"}',
        "state": "asking",
        "suggested_rules": [],
    }
    store.apply_receipt(_receipt(run, "REQUIRE_USER_CONFIRM", payload={"tool_calls": [tool_call]}))

    def approve(call: dict[str, object]):
        return store.begin_run(
            session_id="session-a",
            runtime_agent_id="runtime-a",
            input_value={
                "type": "USER_CONFIRM_RESULT",
                "reply_id": "reply-a",
                "confirm_results": [{"confirmed": True, "tool_call": call}],
            },
            alert_id=None,
            case_id=None,
            metadata={},
            confirmation_scope=ConfirmationScope.RUN,
            expected_run_id=run.run_id,
        )

    with pytest.raises(RuntimeStateConflict, match="requires AgentScope suggested"):
        approve(deepcopy(tool_call))

    malicious_call = deepcopy(tool_call)
    malicious_call["suggested_rules"] = [
        {
            "tool_name": "Write",
            "rule_content": "**",
            "behavior": "allow",
            "source": "suggested",
        }
    ]
    persisted_call = deepcopy(tool_call)
    persisted_call["suggested_rules"] = [
        {
            "tool_name": "Read",
            "rule_content": "reports/**",
            "behavior": "allow",
            "source": "suggested",
        }
    ]
    with store.Session.begin() as db:
        action = db.get(RuntimePendingActionModel, f"{run.run_id}:reply-a:tool-run")
        assert action is not None
        action.tool_call_json = persisted_call

    def attempt(_: int) -> str:
        try:
            return approve(deepcopy(malicious_call)).run_id
        except RuntimeStateConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(attempt, range(2)))
    assert outcomes.count(run.run_id) == 1
    assert outcomes.count("conflict") == 1
    with store.Session() as db:
        action = db.get(RuntimePendingActionModel, f"{run.run_id}:reply-a:tool-run")
        assert action is not None
        assert action.run_rules_json == [
            {
                "tool_name": "Read",
                "rule_content": "reports/**",
                "behavior": "allow",
                "source": f"agentgov-run:{run.run_id}",
            }
        ]


def test_external_result_requires_exact_identity_and_terminal_state(tmp_path) -> None:
    store = _store(tmp_path)
    run = _begin(store)
    store.mark_trigger_started(run.run_id)
    store.apply_receipt(_receipt(run, "REPLY_START"))
    store.apply_receipt(
        _receipt(
            run,
            "REQUIRE_EXTERNAL_EXECUTION",
            payload={"tool_calls": [{"type": "tool_call", "id": "external-a", "name": "browser", "input": "{}", "state": "pending"}]},
        )
    )

    def resume(state: str, name: str = "browser"):
        return store.begin_run(
            session_id="session-a",
            runtime_agent_id="runtime-a",
            input_value={
                "type": "EXTERNAL_EXECUTION_RESULT",
                "reply_id": "reply-a",
                "execution_results": [{"type": "tool_result", "id": "external-a", "name": name, "output": "done", "state": state}],
            },
            alert_id=None,
            case_id=None,
            metadata={},
            expected_run_id=run.run_id,
        )

    with pytest.raises(RuntimeStateConflict, match="terminal tool state"):
        resume("running")
    with pytest.raises(RuntimeStateConflict, match="tool name"):
        resume("success", name="other")
    assert resume("success").run_id == run.run_id


def test_hitl_allows_exact_partial_batch_and_rejects_stale_run_or_mixed_kind(tmp_path) -> None:
    store = _store(tmp_path)
    run = _begin(store)
    store.mark_trigger_started(run.run_id)
    calls = [{"type": "tool_call", "id": tool_id, "name": "Read", "input": f'{{"file_path":"{tool_id}.txt"}}'} for tool_id in ("tool-a", "tool-b")]
    store.apply_receipt(_receipt(run, "REQUIRE_USER_CONFIRM", payload={"tool_calls": calls}))
    decision = {
        "type": "USER_CONFIRM_RESULT",
        "reply_id": "reply-a",
        "confirm_results": [{"confirmed": False, "tool_call": deepcopy(calls[0])}],
    }

    with pytest.raises(RuntimeStateConflict, match="expected_run_id"):
        store.begin_run(
            session_id="session-a",
            runtime_agent_id="runtime-a",
            input_value=deepcopy(decision),
            alert_id=None,
            case_id=None,
            metadata={},
            expected_run_id="run-stale",
        )

    resumed = store.begin_run(
        session_id="session-a",
        runtime_agent_id="runtime-a",
        input_value=decision,
        alert_id=None,
        case_id=None,
        metadata={},
        expected_run_id=run.run_id,
    )
    assert resumed.run_id == run.run_id
    with store.Session() as db:
        first = db.get(RuntimePendingActionModel, f"{run.run_id}:reply-a:tool-a")
        second = db.get(RuntimePendingActionModel, f"{run.run_id}:reply-a:tool-b")
        assert first is not None and first.status == "resolved"
        assert second is not None and second.status == "pending"

    with pytest.raises(RuntimeStateConflict, match="Mixed human/external"):
        store.apply_receipt(
            _receipt(
                run,
                "REQUIRE_EXTERNAL_EXECUTION",
                payload={"tool_calls": [{"id": "external-a", "name": "browser", "input": "{}"}]},
            ),
        )


def test_multi_reply_batch_waits_for_marker_and_every_message_in_any_receipt_order(tmp_path) -> None:
    store = _store(tmp_path)
    run = _begin(store)
    store.mark_trigger_started(run.run_id)
    store.apply_receipt(_receipt(run, "REPLY_START", reply_id="reply-a"))
    store.apply_receipt(
        _receipt(run, "REPLY_END", reply_id="reply-a", payload={"finished_reason": "completed"}),
    )
    # 同一个 AgentScope ChatService run 在 Session lock 内继续消费 inbox。
    assert store.apply_receipt(_receipt(run, "REPLY_START", reply_id="reply-b")).status is RunStatus.RUNNING
    store.apply_receipt(
        _receipt(run, "REPLY_END", reply_id="reply-b", payload={"finished_reason": "completed"}),
    )

    assert store.apply_receipt(_session_persisted(run, "reply-a", "reply-b")).status is RunStatus.FINALIZING
    assert (
        store.apply_receipt(
            _receipt(
                run,
                "MESSAGE_PERSISTED",
                reply_id="reply-b",
                payload={"message_persisted": True, "finished_reason": "completed"},
            ),
        ).status
        is RunStatus.FINALIZING
    )
    terminal = store.apply_receipt(
        _receipt(
            run,
            "MESSAGE_PERSISTED",
            reply_id="reply-a",
            payload={"message_persisted": True, "finished_reason": "completed"},
        ),
    )
    assert terminal.status is RunStatus.SUCCEEDED
    assert terminal.reply_ids == ["reply-a", "reply-b"]
    assert terminal.persisted_reply_ids == ["reply-b", "reply-a"]
    assert terminal.persistence_batch_reply_ids == ["reply-a", "reply-b"]


def test_late_old_run_receipt_cannot_bind_to_new_session_fence(tmp_path) -> None:
    store = _store(tmp_path)
    old = _begin(store)
    store.mark_trigger_started(old.run_id)
    store.apply_receipt(_receipt(old, "REPLY_START"))
    store.apply_receipt(_receipt(old, "REPLY_END", payload={"finished_reason": "completed"}))
    store.apply_receipt(
        _receipt(old, "MESSAGE_PERSISTED", payload={"message_persisted": True, "finished_reason": "completed"}),
    )
    store.apply_receipt(_session_persisted(old, "reply-a"))

    new = _begin(store)
    late = _receipt(
        old,
        "MESSAGE_PERSISTED",
        reply_id="reply-late",
        payload={"message_persisted": True, "finished_reason": "completed"},
    )
    with pytest.raises(RuntimeStateConflict, match="does not own this session fence"):
        store.apply_receipt(late)
    current = store.get_run(new.run_id)
    assert current.status is RunStatus.QUEUED
    assert current.reply_ids == []
    assert current.persisted_reply_ids == []


def test_trace_identity_is_immutable_and_only_terminal_trace_can_complete(tmp_path) -> None:
    store = _store(tmp_path)
    run = _begin(store)
    store.mark_trigger_started(run.run_id)
    with pytest.raises(RuntimeStateConflict, match="another trace"):
        store.apply_receipt(_receipt(run, "REPLY_START", trace_id="b" * 32))
    with pytest.raises(RuntimeStateConflict, match="terminal run"):
        store.mark_trace_observed(run.run_id)

    store.apply_receipt(_receipt(run, "REPLY_START"))
    store.apply_receipt(_receipt(run, "REPLY_END", payload={"finished_reason": "completed"}))
    store.apply_receipt(
        _receipt(
            run,
            "MESSAGE_PERSISTED",
            payload={"message_persisted": True, "finished_reason": "completed"},
        )
    )
    store.apply_receipt(_session_persisted(run, "reply-a"))
    observed = store.mark_trace_observed(run.run_id, trace_url="https://langfuse.example/trace")
    assert observed.trace_status == "complete"
    assert observed.trace_url == "https://langfuse.example/trace"


def test_runtime_trace_complete_claim_never_bypasses_langfuse_reconciliation(tmp_path) -> None:
    store = _store(tmp_path)
    run = _begin(store)
    store.mark_trigger_started(run.run_id)
    store.apply_receipt(_receipt(run, "REPLY_START"))
    store.apply_receipt(_receipt(run, "REPLY_END", payload={"finished_reason": "completed"}))
    store.apply_receipt(
        _receipt(
            run,
            "MESSAGE_PERSISTED",
            payload={
                "message_persisted": True,
                "finished_reason": "completed",
                "trace_complete": True,
            },
        ),
    )
    terminal = store.apply_receipt(_session_persisted(run, "reply-a"))
    assert terminal.status is RunStatus.SUCCEEDED
    assert terminal.trace_status == "pending"


def test_restart_reconciliation_fails_closed_and_keeps_session_fence(tmp_path) -> None:
    store = _store(tmp_path)
    run = _begin(store)
    store.mark_trigger_started(run.run_id)
    assert store.reconcile_after_restart() == [run.run_id]
    recovered = store.get_run(run.run_id)
    assert recovered.status is RunStatus.RUNNING
    assert recovered.terminal_reason is None
    assert recovered.trace_status == "incomplete"
    assert recovered.metadata["recovery_required"] is True
    with pytest.raises(RuntimeStateConflict, match="active run"):
        _begin(store)
