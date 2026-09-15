from __future__ import annotations

from datetime import datetime, timedelta

from app.runtime.integrations.runtime_langfuse import project_validation_trace
from app.runtime.json_types import JsonObject
from app.runtime.runtime_db import make_session_factory
from app.runtime_gateway.contracts import RuntimeReceipt
from app.runtime_gateway.store import RuntimeRunStore
from app.runtime_gateway.trace_reconciliation import reconcile_pending_traces


def _store(tmp_path) -> RuntimeRunStore:
    store = RuntimeRunStore(make_session_factory(tmp_path / "runtime.db"))
    store.bind_agent_version(
        agent_id="agent-a",
        agent_version_id="version-a",
        digest="a" * 64,
        runtime_agent_id="runtime-a",
    )
    return store


def _terminal_run(store: RuntimeRunStore, suffix: str, *, tool_state: str | None = None):
    session_id = f"session-{suffix}"
    reply_id = f"reply-{suffix}"
    store.bind_session(
        session_id=session_id,
        agent_id="agent-a",
        agent_version_id="version-a",
        runtime_agent_id="runtime-a",
        digest="a" * 64,
    )
    run = store.begin_run(
        session_id=session_id,
        runtime_agent_id="runtime-a",
        input_value={"role": "user", "content": []},
        entities={},
        metadata={},
    )
    store.mark_trigger_started(run.run_id)
    if tool_state is not None:
        store.apply_receipt(
            RuntimeReceipt(
                receipt_id=f"receipt-{suffix}-tool",
                event_id=f"event-{suffix}-tool",
                session_id=session_id,
                run_id=run.run_id,
                reply_id=reply_id,
                type="TOOL_RESULT_END",
                payload={"tool_call_id": "call-1", "state": tool_state},
                trace_id=run.trace_id,
            ),
        )
    for index, (event_type, payload) in enumerate(
        (
            ("REPLY_END", {"finished_reason": "completed"}),
            ("MESSAGE_PERSISTED", {"message_persisted": True, "finished_reason": "completed"}),
            (
                "SESSION_PERSISTED",
                {"reply_ids": [reply_id], "message_count": 1, "team_generation": 0},
            ),
        ),
    ):
        store.apply_receipt(
            RuntimeReceipt(
                receipt_id=f"receipt-{suffix}-{index}",
                event_id=f"event-{suffix}-{index}",
                session_id=session_id,
                run_id=run.run_id,
                reply_id=None if event_type == "SESSION_PERSISTED" else reply_id,
                type=event_type,
                payload=payload,
                trace_id=run.trace_id,
            ),
        )
    return store.get_run(run.run_id)


def _complete_trace(run) -> JsonObject:
    ended = "2026-09-10T00:00:01Z"
    root_id = f"root-{run.run_id}"
    stage_id = f"stage-{run.run_id}"
    invoke_id = f"invoke-{run.run_id}"
    return {
        "id": run.trace_id,
        "url": f"https://langfuse.example/{run.trace_id}",
        "observations": [
            {
                "id": root_id,
                "name": "agentgov.run",
                "traceId": run.trace_id,
                "parentObservationId": None,
                "endTime": ended,
                "attributes": {
                    "agentgov.run.id": run.run_id,
                    "agentgov.agent.id": run.agent_id,
                    "agentgov.agent.version_id": run.agent_version_id,
                    "agentgov.harness.digest": run.harness_digest,
                    "agentgov.runtime.version": "v1",
                    "agentscope.agent.id": run.runtime_agent_id,
                    "agentscope.runtime.version": "2.0.8",
                    "agentscope.session.id": run.session_id,
                    "agentgov.run.finished_reason": run.terminal_reason,
                },
            },
            {
                "id": stage_id,
                "name": "agentgov.run.stage",
                "traceId": run.trace_id,
                "parentObservationId": root_id,
                "endTime": ended,
                "attributes": {
                    "agentscope.agent.id": run.runtime_agent_id,
                    "agentscope.session.id": run.session_id,
                    "agentscope.agent.reply_id": run.reply_ids[0],
                },
            },
            {
                "id": invoke_id,
                "name": "invoke_agent",
                "traceId": run.trace_id,
                "parentObservationId": stage_id,
                "endTime": ended,
                "attributes": {
                    "gen_ai.conversation.id": run.session_id,
                    "agentscope.agent.reply_id": run.reply_ids[0],
                    "agentgov.content.input.length": 1,
                    "agentgov.content.input.sha256": "b" * 64,
                },
            },
            {
                "id": f"chat-{run.run_id}",
                "name": "chat",
                "traceId": run.trace_id,
                "parentObservationId": invoke_id,
                "endTime": ended,
                "attributes": {
                    "gen_ai.conversation.id": run.session_id,
                    "gen_ai.request.model": "model-1",
                    "gen_ai.provider.name": "provider-1",
                    "agentgov.content.output.length": 1,
                    "agentgov.content.output.sha256": "c" * 64,
                },
            },
        ],
    }


def _completed_at(run) -> datetime:
    assert run.completed_at is not None
    return datetime.fromisoformat(run.completed_at.replace("Z", "+00:00"))


def test_background_reconciliation_completes_without_trace_api_read(tmp_path) -> None:
    store = _store(tmp_path)
    run = _terminal_run(store, "complete")
    report = reconcile_pending_traces(
        store=store,
        trace_fetcher=lambda _: _complete_trace(run),
        now=_completed_at(run) + timedelta(seconds=1),
    )
    assert report.scanned == report.completed == 1
    assert report.incomplete == report.pending == report.failures == 0
    assert store.get_run(run.run_id).trace_status == "complete"


def test_one_fetch_failure_stays_pending_and_does_not_block_later_run(tmp_path) -> None:
    store = _store(tmp_path)
    failed = _terminal_run(store, "failed-fetch")
    valid = _terminal_run(store, "valid-after-failure")

    def fetch(trace_id: str) -> JsonObject:
        if trace_id == failed.trace_id:
            raise TimeoutError("Langfuse unavailable")
        return _complete_trace(valid)

    report = reconcile_pending_traces(
        store=store,
        trace_fetcher=fetch,
        now=max(_completed_at(failed), _completed_at(valid)) + timedelta(seconds=61),
    )
    assert report.scanned == 2
    assert report.completed == report.failures == report.pending == 1
    assert store.get_run(failed.run_id).trace_status == "pending"
    assert store.get_run(valid.run_id).trace_status == "complete"


def test_semantically_incomplete_trace_uses_terminal_time_deadline(tmp_path) -> None:
    store = _store(tmp_path)
    run = _terminal_run(store, "invalid")
    invalid_trace: JsonObject = {"id": run.trace_id, "observations": []}
    before_deadline = reconcile_pending_traces(
        store=store,
        trace_fetcher=lambda _: invalid_trace,
        now=_completed_at(run) + timedelta(seconds=59),
    )
    assert before_deadline.pending == 1
    assert store.get_run(run.run_id).trace_status == "pending"

    after_deadline = reconcile_pending_traces(
        store=store,
        trace_fetcher=lambda _: invalid_trace,
        now=_completed_at(run) + timedelta(seconds=60),
    )
    assert after_deadline.incomplete == 1
    assert store.get_run(run.run_id).trace_status == "incomplete"


def test_reconciliation_uses_durable_tool_state_and_real_langfuse_operation_shape(tmp_path) -> None:
    store = _store(tmp_path)
    executed = _terminal_run(store, "tool-error", tool_state="error")
    denied = _terminal_run(store, "tool-denied", tool_state="denied")
    executed_trace = _complete_trace(executed)
    observations = executed_trace["observations"]
    assert isinstance(observations, list)
    observations.append(
        {
            "id": f"tool-{executed.run_id}",
            "name": None,
            "traceId": executed.trace_id,
            "parentObservationId": f"invoke-{executed.run_id}",
            "endTime": "2026-09-10T00:00:01Z",
            "metadata": {
                "attributes": {
                    "gen_ai.operation.name": "execute_tool",
                    "gen_ai.conversation.id": executed.session_id,
                    "gen_ai.tool.call.id": "call-1",
                }
            },
        },
    )
    traces = {
        executed.trace_id: project_validation_trace(executed_trace),
        denied.trace_id: project_validation_trace(_complete_trace(denied)),
    }

    report = reconcile_pending_traces(
        store=store,
        trace_fetcher=lambda trace_id: traces[trace_id],
        now=max(_completed_at(executed), _completed_at(denied)) + timedelta(seconds=1),
    )

    assert report.scanned == report.completed == 2
    assert report.incomplete == report.pending == report.failures == 0
    assert store.get_run(executed.run_id).trace_status == "complete"
    assert store.get_run(denied.run_id).trace_status == "complete"
