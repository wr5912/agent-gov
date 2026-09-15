from __future__ import annotations

from copy import deepcopy

import pytest
from app.runtime.integrations.runtime_langfuse import project_validation_trace
from app.runtime_gateway.contracts import (
    AgentRunResponse,
    RunStatus,
    RuntimeTraceActionExpectation,
    RuntimeTraceExpectations,
    RuntimeTraceTeamChildExpectation,
    RuntimeTraceToolExpectation,
)
from app.runtime_gateway.trace_validation import trace_has_complete_governed_run


def _run() -> AgentRunResponse:
    return AgentRunResponse(
        run_id="run-1",
        session_id="session-1",
        agent_id="agent-1",
        agent_version_id="f" * 40,
        runtime_agent_id="runtime-agent-1",
        harness_digest="a" * 64,
        status=RunStatus.SUCCEEDED,
        reply_ids=["reply-1"],
        persisted_reply_ids=["reply-1"],
        persistence_batch_reply_ids=["reply-1"],
        trace_id="1" * 32,
        terminal_reason="completed",
        created_at="2026-09-10T00:00:00Z",
        updated_at="2026-09-10T00:00:01Z",
        completed_at="2026-09-10T00:00:01Z",
    )


def _expectations() -> RuntimeTraceExpectations:
    run = _run()
    return RuntimeTraceExpectations(
        run_id=run.run_id,
        root_session_id=run.session_id,
        root_reply_ids=run.reply_ids,
    )


def _complete_trace() -> dict[str, object]:
    run = _run()
    ended = "2026-09-10T00:00:01Z"
    return {
        "id": run.trace_id,
        "observations": [
            {
                "id": "root-span",
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
                "id": "stage-span",
                "name": "agentgov.run.stage",
                "traceId": run.trace_id,
                "parentObservationId": "root-span",
                "endTime": ended,
                "attributes": {
                    "agentscope.agent.id": run.runtime_agent_id,
                    "agentscope.session.id": run.session_id,
                    "agentscope.agent.reply_id": "reply-1",
                },
            },
            {
                "id": "invoke-span",
                "name": "invoke_agent",
                "traceId": run.trace_id,
                "parentObservationId": "stage-span",
                "endTime": ended,
                "attributes": {
                    "gen_ai.conversation.id": run.session_id,
                    "agentscope.agent.reply_id": "reply-1",
                    "agentgov.content.input.length": 5,
                    "agentgov.content.input.sha256": "b" * 64,
                },
            },
            {
                "id": "chat-span",
                "name": "chat",
                "traceId": run.trace_id,
                "parentObservationId": "invoke-span",
                "endTime": ended,
                "attributes": {
                    "gen_ai.conversation.id": run.session_id,
                    "gen_ai.request.model": "model-1",
                    "gen_ai.provider.name": "provider-1",
                    "agentgov.content.output.length": 7,
                    "agentgov.content.output.sha256": "c" * 64,
                },
            },
        ],
    }


def _observations(trace: dict[str, object]) -> list[dict[str, object]]:
    return trace["observations"]  # type: ignore[return-value]


def _attributes(trace: dict[str, object], index: int) -> dict[str, object]:
    return _observations(trace)[index]["attributes"]  # type: ignore[return-value]


def _add_worker(trace: dict[str, object]) -> None:
    ended = "2026-09-10T00:00:01Z"
    _observations(trace).extend(
        [
            {
                "id": "worker-stage",
                "name": "agentgov.run.stage",
                "traceId": _run().trace_id,
                "parentObservationId": "root-span",
                "endTime": ended,
                "attributes": {
                    "agentscope.agent.id": "runtime-worker",
                    "agentscope.session.id": "worker-session",
                    "agentscope.agent.reply_id": "worker-reply",
                },
            },
            {
                "id": "worker-invoke",
                "name": "invoke_agent",
                "traceId": _run().trace_id,
                "parentObservationId": "worker-stage",
                "endTime": ended,
                "attributes": {
                    "gen_ai.conversation.id": "worker-session",
                    "agentscope.agent.reply_id": "worker-reply",
                },
            },
        ],
    )


def _team_expectations() -> RuntimeTraceExpectations:
    return _expectations().model_copy(
        update={
            "team_children": [
                RuntimeTraceTeamChildExpectation(
                    session_id="worker-session",
                    runtime_agent_id="runtime-worker",
                ),
            ],
        },
    )


def _add_resume_invoke(trace: dict[str, object], *, kind: str) -> None:
    request_key = {
        "human": "agentscope.agent.hitl_pending_tool_call_ids",
        "external": "agentscope.agent.external_execution_pending_tool_call_ids",
    }[kind]
    incoming_type = {
        "human": "USER_CONFIRM_RESULT",
        "external": "EXTERNAL_EXECUTION_RESULT",
    }[kind]
    _attributes(trace, 2)[request_key] = ["call-1"]
    _observations(trace).extend(
        [
            {
                "id": "resume-stage",
                "name": "agentgov.run.stage",
                "traceId": _run().trace_id,
                "parentObservationId": "root-span",
                "endTime": "2026-09-10T00:00:01Z",
                "attributes": {
                    "agentscope.agent.id": _run().runtime_agent_id,
                    "agentscope.session.id": _run().session_id,
                    "agentscope.agent.reply_id": "reply-1",
                },
            },
            {
                "id": "resume-invoke",
                "name": "invoke_agent",
                "traceId": _run().trace_id,
                "parentObservationId": "resume-stage",
                "endTime": "2026-09-10T00:00:01Z",
                "attributes": {
                    "gen_ai.conversation.id": _run().session_id,
                    "agentscope.agent.reply_id": "reply-1",
                    "agentscope.agent.incoming_event_type": incoming_type,
                },
            },
        ],
    )


def test_accepts_one_ended_governed_root_with_required_semantics() -> None:
    assert trace_has_complete_governed_run(_complete_trace(), _run(), _expectations())


def test_rejects_mismatched_or_self_reported_expectations() -> None:
    mismatched = _expectations().model_copy(update={"root_reply_ids": ["reply-other"]})
    assert not trace_has_complete_governed_run(_complete_trace(), _run(), mismatched)
    incomplete = _expectations().model_copy(update={"control_integrity_complete": False})
    assert not trace_has_complete_governed_run(_complete_trace(), _run(), incomplete)


def test_rejects_multiple_roots_or_synthetic_parent() -> None:
    duplicate = _complete_trace()
    _observations(duplicate).append(deepcopy(_observations(duplicate)[0]))
    assert not trace_has_complete_governed_run(duplicate, _run(), _expectations())  # type: ignore[arg-type]

    synthetic = _complete_trace()
    _observations(synthetic)[0]["parentObservationId"] = "synthetic-parent"
    assert not trace_has_complete_governed_run(synthetic, _run(), _expectations())  # type: ignore[arg-type]


def test_rejects_missing_relation_model_or_fingerprint() -> None:
    for observation_index, attribute in (
        (0, "agentgov.agent.version_id"),
        (1, "agentscope.agent.reply_id"),
        (3, "gen_ai.provider.name"),
        (2, "agentgov.content.input.sha256"),
    ):
        trace = _complete_trace()
        del _attributes(trace, observation_index)[attribute]
        assert not trace_has_complete_governed_run(trace, _run(), _expectations())  # type: ignore[arg-type]


def test_rejects_unended_cross_trace_or_disconnected_graph() -> None:
    unended = _complete_trace()
    _observations(unended)[3]["endTime"] = None
    assert not trace_has_complete_governed_run(unended, _run(), _expectations())  # type: ignore[arg-type]

    crossed = _complete_trace()
    _observations(crossed)[2]["traceId"] = "2" * 32
    assert not trace_has_complete_governed_run(crossed, _run(), _expectations())  # type: ignore[arg-type]

    orphan = _complete_trace()
    _observations(orphan)[3]["parentObservationId"] = "missing-span"
    assert not trace_has_complete_governed_run(orphan, _run(), _expectations())  # type: ignore[arg-type]

    cycle = _complete_trace()
    _observations(cycle)[2]["parentObservationId"] = "chat-span"
    _observations(cycle)[3]["parentObservationId"] = "invoke-span"
    assert not trace_has_complete_governed_run(cycle, _run(), _expectations())  # type: ignore[arg-type]


def test_root_stage_reply_set_is_exact_but_worker_reply_is_not_a_root_reply() -> None:
    trace = _complete_trace()
    _add_worker(trace)
    assert trace_has_complete_governed_run(trace, _run(), _team_expectations())  # type: ignore[arg-type]

    _attributes(trace, 4)["agentscope.session.id"] = _run().session_id
    assert not trace_has_complete_governed_run(trace, _run(), _team_expectations())  # type: ignore[arg-type]


@pytest.mark.parametrize("missing_index", [4, 5], ids=["stage", "invoke"])
def test_every_durable_team_child_requires_stage_and_invoke(missing_index: int) -> None:
    trace = _complete_trace()
    _add_worker(trace)
    del _observations(trace)[missing_index]
    assert not trace_has_complete_governed_run(trace, _run(), _team_expectations())  # type: ignore[arg-type]


def _add_tool_span(trace: dict[str, object], *, span_id: str = "tool-span") -> dict[str, object]:
    span: dict[str, object] = {
        "id": span_id,
        "name": "execute_tool",
        "traceId": _run().trace_id,
        "parentObservationId": "invoke-span",
        "endTime": "2026-09-10T00:00:01Z",
        "attributes": {
            "gen_ai.conversation.id": _run().session_id,
            "gen_ai.tool.call.id": "call-1",
        },
    }
    _observations(trace).append(span)
    return span


def _tool_expectations(state: str = "success") -> RuntimeTraceExpectations:
    return _expectations().model_copy(
        update={
            "tool_results": [
                RuntimeTraceToolExpectation(
                    session_id=_run().session_id,
                    reply_id="reply-1",
                    tool_call_id="call-1",
                    state=state,
                    source="tool_result_receipt",
                ),
            ],
        },
    )


@pytest.mark.parametrize("state", ["success", "error", "interrupted", "running"])
def test_non_denied_tool_receipt_requires_one_closed_span_linked_by_parent_invoke(state: str) -> None:
    trace = _complete_trace()
    _add_tool_span(trace)

    assert trace_has_complete_governed_run(trace, _run(), _tool_expectations(state))  # type: ignore[arg-type]


@pytest.mark.parametrize("external_marker", ["true", 1, 0, [], {}])
def test_ordinary_tool_receipt_rejects_malformed_external_execution_marker(external_marker: object) -> None:
    trace = _complete_trace()
    span = _add_tool_span(trace)
    attributes = span["attributes"]
    assert isinstance(attributes, dict)
    attributes["agentscope.agent.is_external_execution"] = external_marker

    assert not trace_has_complete_governed_run(trace, _run(), _tool_expectations())  # type: ignore[arg-type]


def test_ordinary_tool_receipt_accepts_explicit_false_external_execution_marker() -> None:
    trace = _complete_trace()
    span = _add_tool_span(trace)
    attributes = span["attributes"]
    assert isinstance(attributes, dict)
    attributes["agentscope.agent.is_external_execution"] = False

    assert trace_has_complete_governed_run(trace, _run(), _tool_expectations())  # type: ignore[arg-type]


def test_denied_tool_receipt_requires_no_execute_span() -> None:
    expectations = _tool_expectations("denied")
    trace = _complete_trace()
    assert trace_has_complete_governed_run(trace, _run(), expectations)  # type: ignore[arg-type]

    _add_tool_span(trace)
    assert not trace_has_complete_governed_run(trace, _run(), expectations)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("target", "attribute", "wrong_value"),
    [
        ("tool", "gen_ai.conversation.id", "wrong-session"),
        ("tool", "agentscope.agent.reply_id", "wrong-reply"),
        ("tool", "gen_ai.tool.call.id", "wrong-call"),
        ("invoke", "gen_ai.conversation.id", "wrong-session"),
        ("invoke", "agentscope.agent.reply_id", "wrong-reply"),
    ],
)
def test_tool_receipt_rejects_wrong_span_or_parent_identity(
    target: str,
    attribute: str,
    wrong_value: str,
) -> None:
    trace = _complete_trace()
    _add_tool_span(trace)
    index = 4 if target == "tool" else 2
    _attributes(trace, index)[attribute] = wrong_value

    assert not trace_has_complete_governed_run(trace, _run(), _tool_expectations())  # type: ignore[arg-type]


def test_tool_receipt_rejects_missing_parent_invoke_duplicate_and_extra_span() -> None:
    missing_parent = _complete_trace()
    span = _add_tool_span(missing_parent)
    span["parentObservationId"] = "stage-span"
    assert not trace_has_complete_governed_run(missing_parent, _run(), _tool_expectations())  # type: ignore[arg-type]

    duplicate = _complete_trace()
    _add_tool_span(duplicate)
    _add_tool_span(duplicate, span_id="duplicate-tool-span")
    assert not trace_has_complete_governed_run(duplicate, _run(), _tool_expectations())  # type: ignore[arg-type]

    extra = _complete_trace()
    _add_tool_span(extra)
    extra_span = _add_tool_span(extra, span_id="extra-tool-span")
    extra_attributes = extra_span["attributes"]
    assert isinstance(extra_attributes, dict)
    extra_attributes["gen_ai.tool.call.id"] = "unexpected-call"
    assert not trace_has_complete_governed_run(extra, _run(), _tool_expectations())  # type: ignore[arg-type]


def test_tool_receipt_rejects_duplicate_durable_expectations() -> None:
    trace = _complete_trace()
    _add_tool_span(trace)
    expectation = _tool_expectations().tool_results[0]
    expectations = _expectations().model_copy(
        update={"tool_results": [expectation, expectation]},
    )

    assert not trace_has_complete_governed_run(trace, _run(), expectations)  # type: ignore[arg-type]


def test_resolved_human_action_requires_request_and_decision_spans() -> None:
    trace = _complete_trace()
    _add_resume_invoke(trace, kind="human")
    expectations = _expectations().model_copy(
        update={
            "actions": [
                RuntimeTraceActionExpectation(
                    session_id=_run().session_id,
                    reply_id="reply-1",
                    tool_call_id="call-1",
                    kind="human",
                    status="resolved",
                ),
            ],
        },
    )
    assert trace_has_complete_governed_run(trace, _run(), expectations)  # type: ignore[arg-type]
    del _attributes(trace, 2)["agentscope.agent.hitl_pending_tool_call_ids"]
    assert not trace_has_complete_governed_run(trace, _run(), expectations)  # type: ignore[arg-type]
    _attributes(trace, 2)["agentscope.agent.hitl_pending_tool_call_ids"] = '["call-1"]'
    del _attributes(trace, 5)["agentscope.agent.incoming_event_type"]
    assert not trace_has_complete_governed_run(trace, _run(), expectations)  # type: ignore[arg-type]


def test_resolved_external_action_requires_resume_and_synthetic_tool_span() -> None:
    trace = _complete_trace()
    _add_resume_invoke(trace, kind="external")
    _observations(trace).append(
        {
            "id": "external-tool-span",
            "name": "execute_tool",
            "traceId": _run().trace_id,
            "parentObservationId": "resume-invoke",
            "endTime": "2026-09-10T00:00:01Z",
            "attributes": {
                "gen_ai.conversation.id": _run().session_id,
                "gen_ai.tool.call.id": "call-1",
                "agentscope.agent.is_external_execution": True,
            },
        },
    )
    expectations = _expectations().model_copy(
        update={
            "actions": [
                RuntimeTraceActionExpectation(
                    session_id=_run().session_id,
                    reply_id="reply-1",
                    tool_call_id="call-1",
                    kind="external",
                    status="resolved",
                ),
            ],
            "tool_results": [
                RuntimeTraceToolExpectation(
                    session_id=_run().session_id,
                    reply_id="reply-1",
                    tool_call_id="call-1",
                    state=None,
                    source="external_action",
                ),
            ],
        },
    )
    assert trace_has_complete_governed_run(trace, _run(), expectations)  # type: ignore[arg-type]
    _attributes(trace, 6)["agentscope.agent.is_external_execution"] = False
    assert not trace_has_complete_governed_run(trace, _run(), expectations)  # type: ignore[arg-type]
    _attributes(trace, 6)["agentscope.agent.is_external_execution"] = True
    del _observations(trace)[6]
    assert not trace_has_complete_governed_run(trace, _run(), expectations)  # type: ignore[arg-type]


def _langfuse_otel_trace() -> dict[str, object]:
    """合成 Langfuse OTLP 返回形状，不使用真实运行内容或属性值。"""

    trace = _complete_trace()
    for observation in _observations(trace):
        attributes = observation.pop("attributes")
        assert isinstance(attributes, dict)
        for key, value in attributes.items():
            if key.startswith("agentgov.content.") and key.endswith(".length"):
                attributes[key] = str(value)
        observation["metadata"] = {
            "attributes": attributes,
            "resourceAttributes": {"service.name": "test-runtime"},
            "scope": {"name": "test-instrumentation"},
        }
    return trace


def _nested_attributes(trace: dict[str, object], index: int) -> dict[str, object]:
    metadata = _observations(trace)[index]["metadata"]
    assert isinstance(metadata, dict) and isinstance(metadata["attributes"], dict)
    return metadata["attributes"]


def test_accepts_langfuse_nested_otel_attributes_and_decimal_lengths() -> None:
    assert trace_has_complete_governed_run(_langfuse_otel_trace(), _run(), _expectations())  # type: ignore[arg-type]


@pytest.mark.parametrize("container", ["direct", "attributes", "metadata"])
@pytest.mark.parametrize("conflict", [False, True])
def test_duplicate_attribute_sources_must_agree_without_precedence(container, conflict) -> None:
    trace = _langfuse_otel_trace()
    root = _observations(trace)[0]
    value = "other-agent" if conflict else _run().agent_id
    if container == "direct":
        root["agentgov.agent.id"] = value
    elif container == "attributes":
        root["attributes"] = {"agentgov.agent.id": value}
    else:
        metadata = root["metadata"]
        assert isinstance(metadata, dict)
        metadata["agentgov.agent.id"] = value
    assert trace_has_complete_governed_run(trace, _run(), _expectations()) is not conflict  # type: ignore[arg-type]


def test_conflicting_fingerprint_cannot_hide_behind_another_valid_span() -> None:
    trace = _langfuse_otel_trace()
    invoke = _nested_attributes(trace, 2)
    _nested_attributes(trace, 3).update(
        {
            "agentgov.content.input.length": invoke["agentgov.content.input.length"],
            "agentgov.content.input.sha256": invoke["agentgov.content.input.sha256"],
        },
    )
    _observations(trace)[2]["attributes"] = {"agentgov.content.input.length": "99"}
    assert not trace_has_complete_governed_run(trace, _run(), _expectations())  # type: ignore[arg-type]


@pytest.mark.parametrize(("nested", "direct"), [(1, True), (7, "7")])
def test_duplicate_attribute_types_must_agree_before_length_parsing(nested, direct) -> None:
    trace = _langfuse_otel_trace()
    _nested_attributes(trace, 3)["agentgov.content.output.length"] = nested
    _observations(trace)[3]["attributes"] = {"agentgov.content.output.length": direct}
    assert not trace_has_complete_governed_run(trace, _run(), _expectations())  # type: ignore[arg-type]


@pytest.mark.parametrize("container", ["resourceAttributes", "scope", "arbitraryNested"])
def test_metadata_non_span_containers_cannot_supply_required_identity(container) -> None:
    trace = _langfuse_otel_trace()
    identity = _nested_attributes(trace, 0).pop("agentgov.agent.id")
    metadata = _observations(trace)[0]["metadata"]
    assert isinstance(metadata, dict)
    metadata[container] = {"agentgov.agent.id": identity}
    assert not trace_has_complete_governed_run(trace, _run(), _expectations())  # type: ignore[arg-type]


def test_resource_and_scope_attributes_do_not_override_span_identity() -> None:
    trace = _langfuse_otel_trace()
    metadata = _observations(trace)[0]["metadata"]
    assert isinstance(metadata, dict)
    for container in ("resourceAttributes", "scope"):
        metadata[container] = {"agentgov.agent.id": "different-resource-owner"}
    assert trace_has_complete_governed_run(trace, _run(), _expectations())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("index", "key", "value"),
    [(0, "endTime", None), (0, "parentObservationId", "foreign-parent"), (3, "parentObservationId", "missing-span"), (2, "traceId", "2" * 32)],
)
def test_nested_otel_shape_keeps_ended_trace_and_graph_checks(index, key, value) -> None:
    trace = _langfuse_otel_trace()
    _observations(trace)[index][key] = value
    assert not trace_has_complete_governed_run(trace, _run(), _expectations())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("index", "key", "value"),
    [
        (0, "agentgov.agent.version_id", "other-version"),
        (1, "agentscope.agent.reply_id", "other-reply"),
        (2, "gen_ai.conversation.id", "other-session"),
        (3, "gen_ai.provider.name", ""),
        (3, "agentgov.content.output.sha256", "z" * 64),
        (3, "agentgov.content.output.sha256", "A" * 64),
    ],
)
def test_nested_otel_shape_keeps_identity_model_and_digest_checks(index, key, value) -> None:
    trace = _langfuse_otel_trace()
    _nested_attributes(trace, index)[key] = value
    assert not trace_has_complete_governed_run(trace, _run(), _expectations())  # type: ignore[arg-type]


@pytest.mark.parametrize("length", [0, "0", 7, "7", (1 << 63) - 1, str((1 << 63) - 1)])
def test_content_length_accepts_only_bounded_nonnegative_integer_values(length) -> None:
    trace = _langfuse_otel_trace()
    _nested_attributes(trace, 3)["agentgov.content.output.length"] = length
    assert trace_has_complete_governed_run(trace, _run(), _expectations())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "length",
    [True, False, -1, 7.0, None, "", " ", " 7", "7 ", "+7", "-1", "7.0", "1e3", "00", "０", "١", 1 << 63, str(1 << 63), "9" * 5000],
)
def test_content_length_rejects_untyped_or_malformed_values(length) -> None:
    trace = _langfuse_otel_trace()
    _nested_attributes(trace, 3)["agentgov.content.output.length"] = length
    assert not trace_has_complete_governed_run(trace, _run(), _expectations())  # type: ignore[arg-type]


def test_langfuse_positive_projection_retains_the_complete_validation_contract() -> None:
    projected = project_validation_trace(_langfuse_otel_trace())

    assert trace_has_complete_governed_run(projected, _run(), _expectations())


def test_langfuse_projection_recovers_real_execute_tool_shape_without_reply_or_state() -> None:
    trace = _langfuse_otel_trace()
    _observations(trace).append(
        {
            "id": "tool-span",
            "name": None,
            "traceId": _run().trace_id,
            "parentObservationId": "invoke-span",
            "endTime": "2026-09-10T00:00:01Z",
            "metadata": {
                "attributes": {
                    "gen_ai.operation.name": "execute_tool",
                    "gen_ai.conversation.id": _run().session_id,
                    "gen_ai.tool.call.id": "call-1",
                }
            },
        },
    )

    projected = project_validation_trace(trace)

    assert trace_has_complete_governed_run(projected, _run(), _tool_expectations("error"))
