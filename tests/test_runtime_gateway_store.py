from __future__ import annotations

import itertools
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from app.runtime.runtime_db import make_session_factory
from app.runtime_gateway.contracts import ConfirmationScope, RunStatus, RuntimeReceipt, governed_run_permission_rules
from app.runtime_gateway.models import RuntimePendingActionModel, RuntimeReceiptModel
from app.runtime_gateway.store import RuntimeInputRejected, RuntimeRunStore, RuntimeStateConflict
from sqlalchemy import func, select

from runtime_hitl_test_utils import fingerprinted_hitl_payload


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
    waiting = store.apply_receipt(
        _receipt(
            run,
            "REQUIRE_USER_CONFIRM",
            payload=fingerprinted_hitl_payload([tool_call]),
        ),
    )
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
            client_operation_id="continuation-kind-tamper",
            expected_run_id=run.run_id,
        )

    def confirmation(*, input_value: str = '{"path":"out.txt","text":"ok"}', rules=None):
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
            client_operation_id="continuation-client-rules",
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
            client_operation_id="continuation-tool-tamper",
            expected_run_id=run.run_id,
        )

    resumed = store.begin_run(
        session_id="session-a",
        runtime_agent_id="runtime-a",
        input_value=confirmation(),
        alert_id=None,
        case_id=None,
        metadata={},
        client_operation_id="continuation-approved",
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
                "source": "workspace_policy.ask_tools",
            },
        ],
    }
    store.apply_receipt(
        _receipt(
            run,
            "REQUIRE_USER_CONFIRM",
            payload=fingerprinted_hitl_payload([tool_call]),
        ),
    )
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
        client_operation_id="continuation-run-scope",
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
    store.apply_receipt(
        _receipt(
            run,
            "REQUIRE_USER_CONFIRM",
            payload=fingerprinted_hitl_payload([tool_call]),
        ),
    )

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
            client_operation_id="continuation-run-scope-race",
            confirmation_scope=ConfirmationScope.RUN,
            expected_run_id=run.run_id,
        )

    with pytest.raises(RuntimeStateConflict, match="requires AgentScope suggested"):
        approve(deepcopy(tool_call))

    for rule_content in (None, "", "*", "**", "./**", "/**/*"):
        broad_call = deepcopy(tool_call)
        broad_call["suggested_rules"] = [
            {
                "tool_name": "Read",
                "rule_content": rule_content,
                "behavior": "allow",
                "source": "workspace_policy.ask_tools",
            },
        ]
        with store.Session.begin() as db:
            action = db.get(RuntimePendingActionModel, f"{run.run_id}:reply-a:tool-run")
            assert action is not None
            action.tool_call_json = fingerprinted_hitl_payload(
                [broad_call],
            )["tool_calls"][0]
        with pytest.raises(RuntimeStateConflict, match="bounded AgentScope"):
            approve(deepcopy(broad_call))

    for tool_name, rule_content in (
        ("Bash", "*a*"),
        ("mcp__security__lookup", "incident-123"),
        ("Glob", "reports/**"),
        ("Read", "**/secret.txt"),
        ("Write", "../outputs/**"),
        ("Unknown", "reports/**"),
    ):
        unsafe_rule_call = {
            **deepcopy(tool_call),
            "name": tool_name,
            "suggested_rules": [
                {
                    "tool_name": tool_name,
                    "rule_content": rule_content,
                    "behavior": "allow",
                    "source": "workspace_policy.ask_tools",
                },
            ],
        }
        with pytest.raises(ValueError, match="bounded AgentScope"):
            governed_run_permission_rules(unsafe_rule_call, run.run_id)

    untrusted_source = deepcopy(tool_call)
    untrusted_source["suggested_rules"] = [
        {
            "tool_name": "Read",
            "rule_content": "reports/**",
            "behavior": "allow",
            "source": "suggested",
        },
    ]
    with pytest.raises(ValueError, match="governed suggestion source"):
        governed_run_permission_rules(untrusted_source, run.run_id)

    malicious_call = deepcopy(tool_call)
    malicious_call["suggested_rules"] = [
        {
            "tool_name": "Write",
            "rule_content": "**",
            "behavior": "allow",
            "source": "workspace_policy.ask_tools",
        }
    ]
    persisted_call = deepcopy(tool_call)
    persisted_call["suggested_rules"] = [
        {
            "tool_name": "Read",
            "rule_content": "reports/**",
            "behavior": "allow",
            "source": "workspace_policy.ask_tools",
        }
    ]
    with store.Session.begin() as db:
        action = db.get(RuntimePendingActionModel, f"{run.run_id}:reply-a:tool-run")
        assert action is not None
        action.tool_call_json = fingerprinted_hitl_payload(
            [persisted_call],
        )["tool_calls"][0]

    def attempt(call: dict[str, object]) -> str:
        try:
            return approve(deepcopy(call)).run_id
        except RuntimeStateConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(attempt, (malicious_call, persisted_call)))
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
            payload=fingerprinted_hitl_payload(
                [
                    {
                        "type": "tool_call",
                        "id": "external-a",
                        "name": "browser",
                        "input": "{}",
                        "state": "pending",
                    },
                ],
            ),
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
            client_operation_id="continuation-external",
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
    store.apply_receipt(
        _receipt(
            run,
            "REQUIRE_USER_CONFIRM",
            payload=fingerprinted_hitl_payload(
                calls,
                default_state="asking",
            ),
        ),
    )
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
            client_operation_id="continuation-stale-run",
            expected_run_id="run-stale",
        )

    resumed = store.begin_run(
        session_id="session-a",
        runtime_agent_id="runtime-a",
        input_value=decision,
        alert_id=None,
        case_id=None,
        metadata={},
        client_operation_id="continuation-partial-batch",
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
                payload=fingerprinted_hitl_payload(
                    [
                        {
                            "id": "external-a",
                            "name": "browser",
                            "input": "{}",
                        },
                    ],
                    default_state="pending",
                ),
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


def test_cancel_request_is_atomic_and_repeat_does_not_reset_quiescence(tmp_path) -> None:
    store = _store(tmp_path)
    run = _begin(store)
    store.mark_trigger_started(run.run_id)

    first = store.mark_cancel_requested(run.run_id)
    assert first.metadata["cancellation_requested"] is True
    assert first.metadata["recovery_required"] is True
    assert first.metadata["recovery_quiescent_observations"] == 0
    assert store.note_recovery_quiescent(run.run_id) == 1

    repeated = store.mark_cancel_requested(run.run_id)
    assert repeated.metadata["recovery_quiescent_observations"] == 1
    assert [item.run_id for item in store.recovery_required_runs()] == [run.run_id]


def test_client_metadata_cannot_preseed_recovery_control_facts(tmp_path) -> None:
    store = _store(tmp_path)
    run = store.begin_run(
        session_id="session-a",
        runtime_agent_id="runtime-a",
        input_value=_message(),
        alert_id=None,
        case_id=None,
        metadata={
            "business_label": "preserved",
            "cancellation_requested": True,
            "recovery_required": True,
            "recovery_quiescent_observations": 99,
            "runtime_interrupted_session_ids": ["session-a"],
        },
    )

    assert run.metadata == {"business_label": "preserved"}
    requested = store.mark_cancel_requested(run.run_id)
    assert requested.metadata["cancellation_requested"] is True
    assert requested.metadata["recovery_quiescent_observations"] == 0


def test_pre_reply_interrupted_receipt_waits_for_quiescence_and_late_receipt_is_ack_only(tmp_path) -> None:
    store = _store(tmp_path)
    run = _begin(store)
    store.mark_trigger_started(run.run_id)
    store.mark_cancel_requested(run.run_id)
    interrupted = _receipt(
        run,
        "RUN_INTERRUPTED",
        reply_id=None,
        payload={},
    )

    recovering = store.apply_receipt(interrupted)
    assert recovering.status is RunStatus.RUNNING
    assert recovering.trace_status == "pending"
    assert recovering.metadata["recovery_required"] is True
    assert recovering.metadata["runtime_interrupted_session_ids"] == [run.session_id]
    assert store.active_run_for_session(run.session_id) is not None

    assert store.apply_receipt(interrupted).status is RunStatus.RUNNING
    assert (
        store.settle_after_quiescent_observation(
            run.run_id,
            error="all bound Sessions are idle",
        )
        is None
    )
    terminal = store.settle_after_quiescent_observation(
        run.run_id,
        error="all bound Sessions are idle",
    )
    assert terminal is not None
    assert terminal.status is RunStatus.CANCELLED
    assert terminal.terminal_reason == "interrupted"
    assert terminal.trace_status == "pending"
    assert terminal.error == {"type": "runtime_interrupted"}
    assert store.active_run_for_session(run.session_id) is None
    expectations = store.trace_expectations(run.run_id)
    assert expectations.interrupted_before_reply is True
    assert expectations.control_integrity_complete is True

    late = interrupted.model_copy(
        update={
            "receipt_id": "late-interrupted-receipt",
            "event_id": "late-interrupted-event",
        },
    )
    assert store.apply_receipt(late).status is RunStatus.CANCELLED
    assert store.mark_cancel_requested(run.run_id).status is RunStatus.CANCELLED
    with store.Session() as db:
        receipt_count = db.scalar(
            select(func.count())
            .select_from(RuntimeReceiptModel)
            .where(
                RuntimeReceiptModel.run_id == run.run_id,
            ),
        )
    assert receipt_count == 1


def test_interrupted_receipt_after_reply_start_keeps_fence_for_idle_recovery(tmp_path) -> None:
    store = _store(tmp_path)
    run = _begin(store)
    store.mark_trigger_started(run.run_id)
    store.apply_receipt(_receipt(run, "REPLY_START"))
    interrupted = store.apply_receipt(
        _receipt(
            run,
            "RUN_INTERRUPTED",
            reply_id=None,
            payload={},
        ),
    )

    assert interrupted.status is RunStatus.RUNNING
    assert interrupted.trace_status == "pending"
    assert interrupted.metadata["recovery_required"] is True
    assert interrupted.metadata["runtime_interrupted_session_ids"] == [run.session_id]
    assert store.active_run_for_session(run.session_id) is not None
    assert [item.run_id for item in store.recovery_required_runs()] == [run.run_id]


def test_canonical_interruption_cannot_be_overwritten_by_late_success_receipts(tmp_path) -> None:
    store = _store(tmp_path)
    run = _begin(store)
    store.mark_trigger_started(run.run_id)
    store.apply_receipt(_receipt(run, "REPLY_START"))
    store.mark_cancel_requested(run.run_id)
    store.apply_receipt(
        _receipt(
            run,
            "RUN_INTERRUPTED",
            reply_id=None,
            payload={},
        ),
    )

    store.apply_receipt(
        _receipt(
            run,
            "REPLY_END",
            payload={"finished_reason": "completed"},
        ),
    )
    store.apply_receipt(
        _receipt(
            run,
            "MESSAGE_PERSISTED",
            payload={
                "message_persisted": True,
                "finished_reason": "completed",
            },
        ),
    )
    late_batch = store.apply_receipt(_session_persisted(run, "reply-a"))

    assert late_batch.status is RunStatus.FINALIZING
    assert late_batch.metadata["recovery_required"] is True
    assert store.active_run_for_session(run.session_id) is not None
    assert (
        store.settle_after_quiescent_observation(
            run.run_id,
            error="first idle",
        )
        is None
    )
    terminal = store.settle_after_quiescent_observation(
        run.run_id,
        error="second idle",
    )
    assert terminal is not None
    assert terminal.status is RunStatus.CANCELLED
    assert terminal.terminal_reason == "interrupted"
    assert terminal.trace_status == "pending"


def test_runtime_originated_interruption_settles_interrupted_after_quiescence(tmp_path) -> None:
    store = _store(tmp_path)
    run = _begin(store)
    store.mark_trigger_started(run.run_id)
    store.apply_receipt(
        _receipt(
            run,
            "RUN_INTERRUPTED",
            reply_id=None,
            payload={},
        ),
    )
    assert (
        store.settle_after_quiescent_observation(
            run.run_id,
            error="all bound Sessions are idle",
        )
        is None
    )
    terminal = store.settle_after_quiescent_observation(
        run.run_id,
        error="all bound Sessions are idle",
    )

    assert terminal is not None
    assert terminal.status is RunStatus.INTERRUPTED
    assert terminal.terminal_reason == "interrupted"
    assert terminal.trace_status == "pending"


def test_delayed_interrupted_receipt_upgrades_fallback_evidence_without_reopening_run(tmp_path) -> None:
    store = _store(tmp_path)
    run = _begin(store)
    store.mark_trigger_started(run.run_id)
    store.mark_cancel_requested(run.run_id)
    assert (
        store.settle_after_quiescent_observation(
            run.run_id,
            error="receipt not yet available",
        )
        is None
    )
    fallback = store.settle_after_quiescent_observation(
        run.run_id,
        error="receipt not yet available",
    )
    assert fallback is not None
    assert fallback.status is RunStatus.CANCELLED
    assert fallback.terminal_reason == "observation_incomplete"
    assert fallback.trace_status == "incomplete"

    upgraded = store.apply_receipt(
        _receipt(
            run,
            "RUN_INTERRUPTED",
            reply_id=None,
            payload={},
        ),
    )

    assert upgraded.status is RunStatus.CANCELLED
    assert upgraded.terminal_reason == "interrupted"
    assert upgraded.trace_status == "pending"
    assert store.active_run_for_session(run.session_id) is None


def test_receipt_idempotency_identity_cannot_be_rebound(tmp_path) -> None:
    store = _store(tmp_path)
    run = _begin(store)
    store.mark_trigger_started(run.run_id)
    receipt = _receipt(run, "REPLY_START")
    store.apply_receipt(receipt)

    with pytest.raises(RuntimeStateConflict, match="idempotency identity"):
        store.apply_receipt(
            receipt.model_copy(update={"reply_id": "different-reply"}),
        )


def test_interrupted_receipt_rejects_content_and_trace_rebinding(tmp_path) -> None:
    store = _store(tmp_path)
    run = _begin(store)
    store.mark_trigger_started(run.run_id)

    with pytest.raises(RuntimeStateConflict, match="must not carry"):
        store.apply_receipt(
            _receipt(
                run,
                "RUN_INTERRUPTED",
                reply_id="reply-not-allowed",
                payload={"message": "not-allowed"},
            ),
        )
    with pytest.raises(RuntimeStateConflict, match="another trace"):
        store.apply_receipt(
            _receipt(
                run,
                "RUN_INTERRUPTED",
                reply_id=None,
                trace_id="b" * 32,
            ),
        )
    valid = _receipt(
        run,
        "RUN_INTERRUPTED",
        reply_id=None,
    )
    store.apply_receipt(valid)
    with pytest.raises(RuntimeStateConflict, match="idempotency identity"):
        store.apply_receipt(
            valid.model_copy(update={"trace_id": "b" * 32}),
        )


def test_cancel_recovery_settles_cancelled_and_expires_hitl_fence(tmp_path) -> None:
    store = _store(tmp_path)
    run = _begin(store)
    store.mark_trigger_started(run.run_id)
    store.apply_receipt(_receipt(run, "REPLY_START"))
    store.apply_receipt(
        _receipt(
            run,
            "REQUIRE_USER_CONFIRM",
            payload=fingerprinted_hitl_payload(
                [
                    {
                        "type": "tool_call",
                        "id": "tool-cancel",
                        "name": "Read",
                        "input": '{"file_path":"cancel.txt"}',
                    },
                ],
                default_state="asking",
            ),
        ),
    )
    store.mark_cancel_requested(run.run_id)

    first_idle = store.settle_after_quiescent_observation(
        run.run_id,
        error="all bound Sessions are now idle",
    )
    terminal = store.settle_after_quiescent_observation(
        run.run_id,
        error="all bound Sessions are now idle",
    )

    assert first_idle is None
    assert terminal is not None
    assert terminal.status is RunStatus.CANCELLED
    assert terminal.terminal_reason == "observation_incomplete"
    assert terminal.trace_status == "incomplete"
    assert store.active_run_for_session(run.session_id) is None
    assert store.pending_actions_for_run(run.run_id) == []
    assert store.fail_recovery(run.run_id, error="repeat").status is RunStatus.CANCELLED
