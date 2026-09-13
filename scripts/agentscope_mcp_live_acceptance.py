"""真实 MCP 平台能力的公共 Session、canonical message 与 SSE 证据校验。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

import httpx
from app.runtime.json_types import JsonObject

from scripts.agentscope_live_acceptance_scenarios import (
    MCP_TECHNICAL_COMPLETION_TEXT,
    MCP_TECHNICAL_TOOL_NAMES,
    LiveAcceptanceError,
    McpExpectation,
    Scenario,
    parse_sse_events,
)

_MAX_TOOL_RESULT_BYTES = 1 * 1024 * 1024


@dataclass(frozen=True)
class McpWorkspaceEvidence:
    server_name: str
    tool_names: tuple[str, ...]
    unapproved_tool_names_absent: tuple[str, ...]


@dataclass(frozen=True)
class McpToolResultEvidence:
    tool_name: str
    utf8_length: int
    sha256: str


@dataclass(frozen=True)
class McpAcceptanceEvidence:
    workspace: McpWorkspaceEvidence
    resource_uris: tuple[str, ...]
    resource_templates: tuple[str, ...]
    resource_content_count: int
    resource_content_utf8_length: int
    resource_content_sha256: str
    tool_results: tuple[McpToolResultEvidence, ...]
    sse_tool_names: tuple[str, ...]

    def summary(self) -> JsonObject:
        """只投影名称、计数和摘要；不输出 MCP 参数或返回正文。"""

        return {
            "server_name": self.workspace.server_name,
            "workspace_tool_names": list(self.workspace.tool_names),
            "unapproved_tool_names_absent": list(self.workspace.unapproved_tool_names_absent),
            "resource_uris": list(self.resource_uris),
            "resource_templates": list(self.resource_templates),
            "resource_content_count": self.resource_content_count,
            "resource_content_utf8_length": self.resource_content_utf8_length,
            "resource_content_sha256": self.resource_content_sha256,
            "tool_results": [
                {
                    "tool_name": item.tool_name,
                    "utf8_length": item.utf8_length,
                    "sha256": item.sha256,
                    "status": "success",
                }
                for item in self.tool_results
            ],
            "sse_tool_names": list(self.sse_tool_names),
        }


def _required_expectation(scenario: Scenario) -> McpExpectation:
    expectation = scenario.mcp_expectation
    if scenario.capability != "mcp_readonly" or expectation is None:
        raise LiveAcceptanceError("MCP 验收必须绑定 mcp_readonly 场景契约")
    return expectation


def _prefixed_tool(server_name: str, raw_name: str) -> str:
    return f"mcp__{server_name}__{raw_name}"


async def validate_workspace_mcp(
    client: httpx.AsyncClient,
    scenario: Scenario,
    *,
    session_id: str,
    runtime_agent_id: str,
) -> McpWorkspaceEvidence:
    """通过 AgentGov 公共 Session route 验证 initialize 后的精确工具投影。"""

    response = await client.get(
        f"/api/runtime/sessions/{session_id}/workspace/mcp",
        params={"agent_id": runtime_agent_id},
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise LiveAcceptanceError("Session MCP route 未返回 JSON") from exc
    return validate_workspace_mcp_payload(payload, scenario)


async def validate_workspace_mcp_if_required(
    client: httpx.AsyncClient,
    scenario: Scenario,
    *,
    session_id: str,
    runtime_agent_id: str,
) -> McpWorkspaceEvidence | None:
    if scenario.capability != "mcp_readonly":
        return None
    return await validate_workspace_mcp(
        client,
        scenario,
        session_id=session_id,
        runtime_agent_id=runtime_agent_id,
    )


def validate_workspace_mcp_payload(payload: object, scenario: Scenario) -> McpWorkspaceEvidence:
    """校验公共 Session MCP route 的脱敏 JSON 投影。"""

    expectation = _required_expectation(scenario)
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise LiveAcceptanceError("Session MCP route 未返回唯一已发布 server")
    server = cast(dict[str, object], payload[0])
    if server.get("name") != expectation.server_name or server.get("is_healthy") is not True or server.get("error") is not None:
        raise LiveAcceptanceError("Session MCP route 未确认真实 server initialize 健康")
    raw_tools = server.get("tools")
    if not isinstance(raw_tools, list) or any(not isinstance(item, dict) for item in raw_tools):
        raise LiveAcceptanceError("Session MCP route 工具列表无效")
    tool_names = tuple(sorted(str(item.get("name") or "") for item in raw_tools))
    expected = tuple(sorted(_prefixed_tool(expectation.server_name, item) for item in expectation.allowed_tool_names))
    unapproved = tuple(sorted(_prefixed_tool(expectation.server_name, item) for item in expectation.unapproved_tool_names))
    if tool_names != expected or any(item in tool_names for item in unapproved):
        raise LiveAcceptanceError("Session MCP route 未按 Harness 精确 tool allowlist 投影")
    return McpWorkspaceEvidence(expectation.server_name, tool_names, unapproved)


def _assistant_blocks(messages: object, reply_ids: tuple[str, ...]) -> tuple[JsonObject, ...]:
    if not isinstance(messages, list):
        raise LiveAcceptanceError("MCP canonical messages 为空")
    expected_replies = set(reply_ids)
    found_replies: set[str] = set()
    blocks: list[JsonObject] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant" or message.get("id") not in expected_replies:
            continue
        found_replies.add(str(message["id"]))
        content = message.get("content")
        if not isinstance(content, list) or any(not isinstance(block, dict) for block in content):
            raise LiveAcceptanceError("MCP canonical assistant content 无效")
        blocks.extend(cast(list[JsonObject], content))
    if found_replies != expected_replies:
        raise LiveAcceptanceError("MCP canonical messages 缺少本次 terminal reply")
    return tuple(blocks)


def _tool_output_bytes(block: JsonObject) -> bytes:
    output = block.get("output")
    if isinstance(output, str):
        text = output
    elif isinstance(output, list):
        text_parts: list[str] = []
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "text" or not isinstance(item.get("text"), str):
                raise LiveAcceptanceError("MCP tool result 含非文本或无效内容")
            text_parts.append(item["text"])
        text = "".join(text_parts)
    else:
        raise LiveAcceptanceError("MCP tool result output 无效")
    encoded = text.encode("utf-8")
    if not encoded or len(encoded) > _MAX_TOOL_RESULT_BYTES:
        raise LiveAcceptanceError("MCP tool result 为空或超过验收上限")
    return encoded


def _paired_tool_blocks(
    blocks: tuple[JsonObject, ...],
    expected_names: tuple[str, ...],
) -> Mapping[str, tuple[JsonObject, bytes]]:
    calls: dict[str, JsonObject] = {}
    results: dict[str, JsonObject] = {}
    for block in blocks:
        block_type = block.get("type")
        block_id = block.get("id")
        if block_type not in {"tool_call", "tool_result"}:
            continue
        if not isinstance(block_id, str) or not block_id:
            raise LiveAcceptanceError("MCP canonical tool block 缺少 id")
        target = calls if block_type == "tool_call" else results
        if block_id in target:
            raise LiveAcceptanceError("MCP canonical tool block id 重复")
        target[block_id] = block
    expected_set = set(expected_names)
    call_names = [str(block.get("name") or "") for block in calls.values()]
    if len(call_names) != len(expected_names) or set(call_names) != expected_set or len(set(call_names)) != len(call_names):
        raise LiveAcceptanceError("MCP canonical ToolCall 未精确覆盖四个批准工具")
    paired: dict[str, tuple[JsonObject, bytes]] = {}
    for block_id, call in calls.items():
        result = results.get(block_id)
        name = str(call.get("name") or "")
        if call.get("state") != "finished" or result is None or result.get("name") != name or result.get("state") != "success":
            raise LiveAcceptanceError("MCP canonical ToolCall/ToolResult 未形成成功配对")
        paired[name] = (call, _tool_output_bytes(result))
    if len(results) != len(paired):
        raise LiveAcceptanceError("MCP canonical ToolResult 含未配对结果")
    return paired


def _validate_completion_text(blocks: tuple[JsonObject, ...]) -> None:
    texts = [str(block.get("text") or "").strip() for block in blocks if block.get("type") == "text" and isinstance(block.get("text"), str)]
    if not texts or texts[-1] != MCP_TECHNICAL_COMPLETION_TEXT:
        raise LiveAcceptanceError("MCP Agent 未返回工具完成后的精确终态标记")


def _json_object_bytes(value: bytes, label: str) -> JsonObject:
    try:
        payload = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LiveAcceptanceError(f"{label} 未返回 JSON object") from exc
    if not isinstance(payload, dict):
        raise LiveAcceptanceError(f"{label} 未返回 JSON object")
    return cast(JsonObject, payload)


def _validate_call_inputs(
    paired: Mapping[str, tuple[JsonObject, bytes]],
    expectation: McpExpectation,
) -> None:
    read_name = _prefixed_tool(expectation.server_name, "resource_read")
    for name, (call, _output) in paired.items():
        raw_input = call.get("input")
        if not isinstance(raw_input, str):
            raise LiveAcceptanceError("MCP canonical ToolCall input 无效")
        try:
            input_value = json.loads(raw_input)
        except json.JSONDecodeError as exc:
            raise LiveAcceptanceError("MCP canonical ToolCall input 不是 JSON object") from exc
        if not isinstance(input_value, dict):
            raise LiveAcceptanceError("MCP canonical ToolCall input 不是 JSON object")
        if name == read_name:
            if input_value != {"uri": expectation.read_resource_uri}:
                raise LiveAcceptanceError("MCP resource_read 未绑定批准 URI")
        elif input_value not in ({}, {"cursor": None}):
            raise LiveAcceptanceError("MCP 只读验收工具携带了未批准参数")


def _validated_resource_evidence(
    paired: Mapping[str, tuple[JsonObject, bytes]],
    expectation: McpExpectation,
) -> tuple[tuple[str, ...], tuple[str, ...], int, int, str]:
    prefix = f"mcp__{expectation.server_name}__"
    resources = _json_object_bytes(paired[f"{prefix}resources_list"][1], "MCP resources_list")
    resource_items = resources.get("resources")
    if not isinstance(resource_items, list) or any(not isinstance(item, dict) for item in resource_items):
        raise LiveAcceptanceError("MCP resources_list 返回无效")
    resource_uris = tuple(sorted(str(item.get("uri") or "") for item in resource_items))
    templates = _json_object_bytes(paired[f"{prefix}resource_templates_list"][1], "MCP resource_templates_list")
    template_items = templates.get("resource_templates")
    if not isinstance(template_items, list) or any(not isinstance(item, dict) for item in template_items):
        raise LiveAcceptanceError("MCP resource_templates_list 返回无效")
    template_uris = tuple(sorted(str(item.get("uri_template") or "") for item in template_items))
    if resource_uris != tuple(sorted(expectation.resource_uris)) or resources.get("next_cursor") is not None:
        raise LiveAcceptanceError("MCP resources_list 未精确过滤 Harness allowlist")
    if template_uris != tuple(sorted(expectation.resource_templates)) or templates.get("next_cursor") is not None:
        raise LiveAcceptanceError("MCP resource_templates_list 未精确过滤 Harness allowlist")
    read_result = _json_object_bytes(paired[f"{prefix}resource_read"][1], "MCP resource_read")
    contents = read_result.get("contents")
    if not isinstance(contents, list) or not contents or any(not isinstance(item, dict) for item in contents):
        raise LiveAcceptanceError("MCP resource_read 返回无效")
    if any(item.get("uri") != expectation.read_resource_uri or not isinstance(item.get("text"), str) for item in contents):
        raise LiveAcceptanceError("MCP resource_read 返回了未批准 URI 或非文本内容")
    content_bytes = "".join(str(item["text"]) for item in contents).encode("utf-8")
    if not content_bytes or len(content_bytes) > _MAX_TOOL_RESULT_BYTES:
        raise LiveAcceptanceError("MCP resource_read 正文为空或超过验收上限")
    return resource_uris, template_uris, len(contents), len(content_bytes), hashlib.sha256(content_bytes).hexdigest()


def _validate_sse_tools(
    raw: bytes,
    expected_calls: Mapping[str, str],
    reply_ids: tuple[str, ...],
) -> tuple[str, ...]:
    events = parse_sse_events(raw)
    starts: dict[str, tuple[str, int]] = {}
    call_ends: dict[str, int] = {}
    result_starts: dict[str, tuple[str, int]] = {}
    result_ends: dict[str, tuple[str, int]] = {}
    for index, event in enumerate(events):
        if event.event_type == "TOOL_CALL_START" and event.tool_call_id and event.tool_call_name:
            if event.tool_call_id in starts:
                raise LiveAcceptanceError("MCP SSE TOOL_CALL_START id 重复")
            starts[event.tool_call_id] = (event.tool_call_name, index)
        elif event.event_type == "TOOL_CALL_END" and event.tool_call_id:
            if event.tool_call_id in call_ends:
                raise LiveAcceptanceError("MCP SSE TOOL_CALL_END id 重复")
            call_ends[event.tool_call_id] = index
        elif event.event_type == "TOOL_RESULT_START" and event.tool_call_id and event.tool_call_name:
            if event.tool_call_id in result_starts:
                raise LiveAcceptanceError("MCP SSE TOOL_RESULT_START id 重复")
            result_starts[event.tool_call_id] = (event.tool_call_name, index)
        elif event.event_type == "TOOL_RESULT_END" and event.tool_call_id and event.tool_result_state:
            if event.tool_call_id in result_ends:
                raise LiveAcceptanceError("MCP SSE TOOL_RESULT_END id 重复")
            result_ends[event.tool_call_id] = (event.tool_result_state, index)
        if event.event_type.startswith("TOOL_") and event.reply_id not in reply_ids:
            raise LiveAcceptanceError("MCP SSE tool 事件未绑定 terminal reply")
    names = tuple(name for name, _index in starts.values())
    observed_calls = {name: tool_id for tool_id, (name, _index) in starts.items()}
    if observed_calls != expected_calls or len(observed_calls) != len(starts):
        raise LiveAcceptanceError("MCP SSE 未与 canonical ToolCall 精确对账四个批准工具")
    for tool_id, (name, start_index) in starts.items():
        result_start = result_starts.get(tool_id)
        result_end = result_ends.get(tool_id)
        if (
            call_ends.get(tool_id, -1) <= start_index
            or result_start is None
            or result_start[0] != name
            or result_start[1] <= call_ends[tool_id]
            or result_end is None
            or result_end[0] != "success"
            or result_end[1] <= result_start[1]
        ):
            raise LiveAcceptanceError("MCP SSE ToolCall/ToolResult 生命周期不完整")
    if set(call_ends) != set(starts) or set(result_starts) != set(starts) or set(result_ends) != set(starts):
        raise LiveAcceptanceError("MCP SSE tool 事件存在未配对 id")
    return tuple(sorted(names))


async def validate_canonical_mcp_evidence(
    client: httpx.AsyncClient,
    scenario: Scenario,
    *,
    session_id: str,
    runtime_agent_id: str,
    reply_ids: tuple[str, ...],
    raw_sse: bytes,
    workspace: McpWorkspaceEvidence,
) -> McpAcceptanceEvidence:
    """对真实 canonical ToolCall/ToolResult、资源结果和 SSE 逐项对账。"""

    response = await client.get(
        f"/api/runtime/sessions/{session_id}/messages",
        params={"agent_id": runtime_agent_id, "limit": 200},
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise LiveAcceptanceError("MCP canonical messages 未返回 JSON") from exc
    messages = payload.get("messages") if isinstance(payload, dict) else None
    return validate_canonical_mcp_payload(
        messages,
        scenario,
        reply_ids=reply_ids,
        raw_sse=raw_sse,
        workspace=workspace,
    )


async def validate_mcp_evidence_if_present(
    client: httpx.AsyncClient,
    scenario: Scenario,
    *,
    session_id: str,
    runtime_agent_id: str,
    reply_ids: tuple[str, ...],
    raw_sse: bytes,
    workspace: McpWorkspaceEvidence | None,
) -> McpAcceptanceEvidence | None:
    if workspace is None:
        return None
    return await validate_canonical_mcp_evidence(
        client,
        scenario,
        session_id=session_id,
        runtime_agent_id=runtime_agent_id,
        reply_ids=reply_ids,
        raw_sse=raw_sse,
        workspace=workspace,
    )


def validate_canonical_mcp_payload(
    messages: object,
    scenario: Scenario,
    *,
    reply_ids: tuple[str, ...],
    raw_sse: bytes,
    workspace: McpWorkspaceEvidence,
) -> McpAcceptanceEvidence:
    """对已取得的 canonical message 与原生 SSE 做纯契约校验。"""

    expectation = _required_expectation(scenario)
    blocks = _assistant_blocks(messages, reply_ids)
    paired = _paired_tool_blocks(blocks, MCP_TECHNICAL_TOOL_NAMES)
    _validate_completion_text(blocks)
    _validate_call_inputs(paired, expectation)
    resources, templates, content_count, content_length, content_sha256 = _validated_resource_evidence(paired, expectation)
    expected_calls = {name: str(call["id"]) for name, (call, _output) in paired.items()}
    sse_tool_names = _validate_sse_tools(raw_sse, expected_calls, reply_ids)
    tool_results = tuple(McpToolResultEvidence(name, len(output), hashlib.sha256(output).hexdigest()) for name, (_call, output) in sorted(paired.items()))
    return McpAcceptanceEvidence(
        workspace=workspace,
        resource_uris=resources,
        resource_templates=templates,
        resource_content_count=content_count,
        resource_content_utf8_length=content_length,
        resource_content_sha256=content_sha256,
        tool_results=tool_results,
        sse_tool_names=sse_tool_names,
    )
