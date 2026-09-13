"""Langfuse 派生 Trace 与 AgentGov durable facts 的严格对账。"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from app.runtime.json_types import JsonObject

from .contracts import (
    AgentRunResponse,
    RunStatus,
    RuntimeTraceActionExpectation,
    RuntimeTraceExpectations,
    RuntimeTraceTeamChildExpectation,
    RuntimeTraceToolExpectation,
)

_HEX_64 = re.compile(r"[0-9a-f]{64}")
_DECIMAL_LENGTH = re.compile(r"(?:0|[1-9][0-9]{0,18})")
_MAX_OTEL_INT64 = (1 << 63) - 1
_ATTRIBUTE_PREFIXES = ("agentgov.", "agentscope.", "gen_ai.")
_CONFLICTING_ATTRIBUTE = object()
_PARENT_KEYS = (
    "parent_observation_id",
    "parentObservationId",
    "parent_span_id",
    "parentSpanId",
    "parent_id",
    "parentId",
)
_ACTION_REQUEST_ATTRIBUTE = {
    "human": "agentscope.agent.hitl_pending_tool_call_ids",
    "external": "agentscope.agent.external_execution_pending_tool_call_ids",
}
_ACTION_RESULT_TYPE = {
    "human": "USER_CONFIRM_RESULT",
    "external": "EXTERNAL_EXECUTION_RESULT",
}


@dataclass(frozen=True)
class _ObservationGraph:
    by_id: Mapping[str, JsonObject]


def trace_has_complete_governed_run(
    trace: JsonObject,
    run: AgentRunResponse,
    expectations: RuntimeTraceExpectations,
) -> bool:
    """只有 Trace 与控制面持久事实全量一致时才可解锁自动改进。"""

    if not _run_and_expectations_match(trace, run, expectations):
        return False
    observations = _observations(trace, run.trace_id)
    if observations is None:
        return False
    roots = [value for value in observations if value.get("name") == "agentgov.run"]
    if len(roots) != 1:
        return False
    root = roots[0]
    graph = _observation_graph(root, observations)
    if graph is None or not _root_attributes_match(root, run):
        return False
    if not _stages_match(observations, expectations):
        return False
    if not _team_children_match(observations, graph, expectations.team_children):
        return False
    if not _tool_results_match(observations, graph, expectations.tool_results):
        return False
    if not _actions_match(observations, expectations.actions):
        return False
    if not _root_invoke_exists(observations, expectations.root_session_id):
        return False
    if expectations.interrupted_before_reply:
        return True
    if not _attributes_present_anywhere(observations, ("gen_ai.request.model", "gen_ai.provider.name")):
        return False
    names = {str(value.get("name")) for value in observations}
    return "chat" in names and _fingerprint_present(observations, "input") and _fingerprint_present(observations, "output")


def _run_and_expectations_match(
    trace: JsonObject,
    run: AgentRunResponse,
    expectations: RuntimeTraceExpectations,
) -> bool:
    if trace.get("fetch_status") == "failed" or not run.trace_id or not run.terminal_reason:
        return False
    if trace.get("id", trace.get("trace_id")) != run.trace_id:
        return False
    if expectations.interrupted_before_reply and (
        run.status not in {RunStatus.CANCELLED, RunStatus.INTERRUPTED} or run.terminal_reason != "interrupted" or bool(run.reply_ids)
    ):
        return False
    return (
        expectations.control_integrity_complete
        and expectations.run_id == run.run_id
        and expectations.root_session_id == run.session_id
        and expectations.root_reply_ids == run.reply_ids
        and len(set(expectations.root_reply_ids)) == len(expectations.root_reply_ids)
    )


def _observations(trace: JsonObject, trace_id: str | None) -> list[JsonObject] | None:
    raw = trace.get("observations")
    if not isinstance(raw, list) or not raw or not trace_id:
        return None
    observations: list[JsonObject] = []
    for value in raw:
        if not isinstance(value, dict):
            return None
        if not _observation_ended(value) or not _trace_matches(value, trace_id) or _has_conflicting_attributes(value):
            return None
        observations.append(value)
    return observations


def _root_attributes_match(root: JsonObject, run: AgentRunResponse) -> bool:
    expected = {
        "agentgov.run.id": run.run_id,
        "agentgov.agent.id": run.agent_id,
        "agentgov.agent.version_id": run.agent_version_id,
        "agentgov.harness.digest": run.harness_digest,
        "agentscope.agent.id": run.runtime_agent_id,
        "agentscope.session.id": run.session_id,
        "agentgov.run.finished_reason": run.terminal_reason,
    }
    return all(_attribute(root, key) == value for key, value in expected.items()) and _nonempty_attributes(
        root,
        ("agentgov.runtime.version", "agentscope.runtime.version"),
    )


def _stages_match(observations: list[JsonObject], expectations: RuntimeTraceExpectations) -> bool:
    stages = [value for value in observations if value.get("name") == "agentgov.run.stage"]
    allowed_sessions = {expectations.root_session_id, *(child.session_id for child in expectations.team_children)}
    if any(_direct_session_id(stage) not in allowed_sessions for stage in stages):
        return False
    root_stages = [stage for stage in stages if _direct_session_id(stage) == expectations.root_session_id]
    if expectations.interrupted_before_reply:
        return len(root_stages) == 1 and _attribute(root_stages[0], "agentgov.run.stage") == "initial"
    raw_reply_ids = [_attribute(stage, "agentscope.agent.reply_id") for stage in root_stages]
    if any(not _valid_reply_id(value) for value in raw_reply_ids):
        return False
    return {str(value) for value in raw_reply_ids} == set(expectations.root_reply_ids)


def _team_children_match(
    observations: list[JsonObject],
    graph: _ObservationGraph,
    children: list[RuntimeTraceTeamChildExpectation],
) -> bool:
    for child in children:
        stages = [
            value
            for value in observations
            if value.get("name") == "agentgov.run.stage"
            and _direct_session_id(value) == child.session_id
            and (child.runtime_agent_id is None or _attribute(value, "agentscope.agent.id") == child.runtime_agent_id)
        ]
        if not stages:
            return False
        stage_ids = {str(stage["id"]) for stage in stages}
        invokes = [
            value
            for value in observations
            if value.get("name") == "invoke_agent"
            and _direct_session_id(value) == child.session_id
            and any(_is_descendant_of(value, stage_id, graph) for stage_id in stage_ids)
        ]
        if not invokes:
            return False
    return True


def _tool_results_match(
    observations: list[JsonObject],
    graph: _ObservationGraph,
    expected_tools: list[RuntimeTraceToolExpectation],
) -> bool:
    tool_spans = [value for value in observations if value.get("name") == "execute_tool"]
    return all(_tool_result_has_span(expectation, tool_spans, graph) for expectation in expected_tools)


def _tool_result_has_span(
    expectation: RuntimeTraceToolExpectation,
    tool_spans: list[JsonObject],
    graph: _ObservationGraph,
) -> bool:
    for span in tool_spans:
        if _direct_session_id(span) != expectation.session_id:
            continue
        if _attribute(span, "gen_ai.tool.call.id") != expectation.tool_call_id:
            continue
        direct_reply = _attribute(span, "agentscope.agent.reply_id")
        observed_reply = direct_reply or _ancestor_attribute(span, "agentscope.agent.reply_id", graph)
        if observed_reply != expectation.reply_id:
            continue
        if expectation.source == "tool_result_receipt" and direct_reply != expectation.reply_id:
            continue
        if expectation.source == "external_action" and _attribute(span, "agentscope.agent.is_external_execution") is not True:
            continue
        if expectation.state is not None and _attribute(span, "agentscope.tool.result.state") != expectation.state:
            continue
        return True
    return False


def _actions_match(
    observations: list[JsonObject],
    actions: list[RuntimeTraceActionExpectation],
) -> bool:
    invokes = [value for value in observations if value.get("name") == "invoke_agent"]
    for action in actions:
        request_key = _ACTION_REQUEST_ATTRIBUTE[action.kind]
        request_matches = [
            span
            for span in invokes
            if _direct_session_id(span) == action.session_id
            and _attribute(span, "agentscope.agent.reply_id") == action.reply_id
            and action.tool_call_id in _string_values(_attribute(span, request_key))
        ]
        if not request_matches:
            return False
        if action.status != "resolved":
            continue
        expected_type = _ACTION_RESULT_TYPE[action.kind]
        if not any(
            _direct_session_id(span) == action.session_id
            and _attribute(span, "agentscope.agent.reply_id") == action.reply_id
            and _attribute(span, "agentscope.agent.incoming_event_type") == expected_type
            for span in invokes
        ):
            return False
    return True


def _root_invoke_exists(observations: list[JsonObject], session_id: str) -> bool:
    return any(value.get("name") == "invoke_agent" and _direct_session_id(value) == session_id for value in observations)


def _string_values(value: object) -> set[str]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {value} if value else set()
        return _string_values(decoded)
    if isinstance(value, (list, tuple)):
        return {item for item in value if isinstance(item, str) and item}
    return set()


def _trace_matches(observation: JsonObject, trace_id: str) -> bool:
    observed = observation.get("trace_id", observation.get("traceId"))
    return observed is None or observed == trace_id


def _observation_ended(observation: JsonObject) -> bool:
    return bool(observation.get("end_time") or observation.get("endTime"))


def _explicit_root(observation: JsonObject) -> bool:
    present = [key for key in _PARENT_KEYS if key in observation]
    return bool(present) and all(observation.get(key) in {None, ""} for key in present)


def _observation_graph(root: JsonObject, observations: list[JsonObject]) -> _ObservationGraph | None:
    identifiers = [observation.get("id") for observation in observations]
    if any(not isinstance(identifier, str) or not identifier for identifier in identifiers):
        return None
    if len(set(identifiers)) != len(identifiers) or not _explicit_root(root):
        return None
    root_id = root.get("id")
    if not isinstance(root_id, str):
        return None
    indexed = {str(observation["id"]): observation for observation in observations}
    graph = _ObservationGraph(indexed)
    if not all(observation is root or _descends_from_root(observation, root_id=root_id, graph=graph) for observation in observations):
        return None
    return graph


def _descends_from_root(
    observation: JsonObject,
    *,
    root_id: str,
    graph: _ObservationGraph,
) -> bool:
    visited: set[str] = set()
    current = observation
    while True:
        parent_id = _parent_identifier(current)
        if parent_id is None:
            return False
        if parent_id == root_id:
            return True
        if parent_id in visited:
            return False
        visited.add(parent_id)
        parent = graph.by_id.get(parent_id)
        if parent is None:
            return False
        current = parent


def _parent_identifier(observation: JsonObject) -> str | None:
    identifiers = {str(observation[key]) for key in _PARENT_KEYS if key in observation and isinstance(observation[key], str) and observation[key]}
    return identifiers.pop() if len(identifiers) == 1 else None


def _is_descendant_of(
    observation: JsonObject,
    ancestor_id: str,
    graph: _ObservationGraph,
) -> bool:
    current = observation
    visited: set[str] = set()
    while True:
        parent_id = _parent_identifier(current)
        if parent_id is None or parent_id in visited:
            return False
        if parent_id == ancestor_id:
            return True
        visited.add(parent_id)
        parent = graph.by_id.get(parent_id)
        if parent is None:
            return False
        current = parent


def _ancestor_attribute(
    observation: JsonObject,
    key: str,
    graph: _ObservationGraph,
) -> object:
    current = observation
    visited: set[str] = set()
    while True:
        value = _attribute(current, key)
        if value is not None:
            return value
        parent_id = _parent_identifier(current)
        if parent_id is None or parent_id in visited:
            return None
        visited.add(parent_id)
        parent = graph.by_id.get(parent_id)
        if parent is None:
            return None
        current = parent


def _direct_session_id(observation: JsonObject) -> object:
    return _attribute(observation, "agentscope.session.id") or _attribute(observation, "gen_ai.conversation.id")


def _attribute_sources(observation: JsonObject) -> tuple[JsonObject, ...]:
    sources = [observation]
    attributes, metadata = observation.get("attributes"), observation.get("metadata")
    if isinstance(attributes, dict):
        sources.append(attributes)
    if isinstance(metadata, dict):
        sources.append(metadata)
        # Langfuse 将原始 OTel span 属性放在这个固定容器；resourceAttributes
        # 和 scope 不属于 span 身份，不能递归搜索或作为缺失字段的替代来源。
        nested = metadata.get("attributes")
        if isinstance(nested, dict):
            sources.append(nested)
    return tuple(sources)


def _attribute(observation: JsonObject, key: str) -> object:
    values = [source[key] for source in _attribute_sources(observation) if key in source]
    if not values:
        return None
    first = values[0]
    if any(type(value) is not type(first) or value != first for value in values[1:]):
        return _CONFLICTING_ATTRIBUTE
    return first


def _has_conflicting_attributes(observation: JsonObject) -> bool:
    keys = {key for source in _attribute_sources(observation) for key in source if key.startswith(_ATTRIBUTE_PREFIXES)}
    return any(_attribute(observation, key) is _CONFLICTING_ATTRIBUTE for key in keys)


def _nonempty_attributes(observation: JsonObject, keys: Iterable[str]) -> bool:
    return all(isinstance(_attribute(observation, key), str) and bool(str(_attribute(observation, key)).strip()) for key in keys)


def _attributes_present_anywhere(observations: list[JsonObject], keys: Iterable[str]) -> bool:
    return all(
        any(isinstance(_attribute(observation, key), str) and bool(str(_attribute(observation, key)).strip()) for observation in observations) for key in keys
    )


def _valid_reply_id(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value != "pending"


def _content_length(value: object) -> int | None:
    # OTLP int64 经 Langfuse JSON 返回十进制字符串；只规范已声明的内容长度，
    # 不对身份、bool 或任意属性做全局类型猜测。先限制位数，避免恶意长串转换。
    if isinstance(value, str) and _DECIMAL_LENGTH.fullmatch(value):
        value = int(value)
    if type(value) is int and 0 <= value <= _MAX_OTEL_INT64:
        return value
    return None


def _fingerprint_present(observations: list[JsonObject], label: str) -> bool:
    length_key = f"agentgov.content.{label}.length"
    digest_key = f"agentgov.content.{label}.sha256"
    for observation in observations:
        length = _content_length(_attribute(observation, length_key))
        digest = _attribute(observation, digest_key)
        if length is not None and isinstance(digest, str) and _HEX_64.fullmatch(digest):
            return True
    return False
