"""真实 AgentScope 验收场景文件的仓库外边界与类型校验。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Set
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from scripts.container_acceptance_inputs import AcceptanceError as LiveAcceptanceError

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
SCENARIO_SCHEMA_PATH: Final = REPO_ROOT / "config/live_acceptance_scenario.schema.json"
GENERIC_RUNTIME_CAPABILITY: Final = "generic_runtime"
MCP_READONLY_CAPABILITY: Final = "mcp_readonly"
MCP_TECHNICAL_SERVER_NAME: Final = "sec-ops"
MCP_TECHNICAL_RAW_TOOL_NAME: Final = "soc_api__dashboard_summary_api_v1_dashboard_summary_get"
MCP_TECHNICAL_RESOURCE_URI: Final = "openapi://soc_api/health"
MCP_TECHNICAL_RESOURCE_TEMPLATE: Final = "openapi://soc_api/api/external/detection-findings/{finding_id}/analysis-result"
MCP_TECHNICAL_COMPLETION_TEXT: Final = "MCP_READONLY_ACCEPTANCE_COMPLETE"
MCP_TECHNICAL_FACADE_TOOL_NAMES: Final = (
    f"mcp__{MCP_TECHNICAL_SERVER_NAME}__resources_list",
    f"mcp__{MCP_TECHNICAL_SERVER_NAME}__resource_templates_list",
    f"mcp__{MCP_TECHNICAL_SERVER_NAME}__resource_read",
)
MCP_TECHNICAL_TOOL_NAMES: Final = (
    *MCP_TECHNICAL_FACADE_TOOL_NAMES,
    f"mcp__{MCP_TECHNICAL_SERVER_NAME}__{MCP_TECHNICAL_RAW_TOOL_NAME}",
)
MCP_TECHNICAL_UNAPPROVED_RAW_TOOLS: Final = frozenset(
    {
        "soc_api__create_ai_scenario_api_v1_ai_scenarios_post",
        "soc_api__list_alerts_api_v1_alerts_get",
        "soc_api__list_assets_api_v1_assets_get",
        "soc_api__list_detection_findings_api_external_detection_findings_get",
        "soc_api__list_events_api_v1_events_get",
        "soc_api__list_incidents_api_v1_incidents_get",
        "soc_api__list_indicators_api_v1_indicators_get",
        "soc_api__list_vulnerabilities_api_v1_vulnerabilities_get",
    },
)
RUNTIME_VERIFIED_CAPABILITIES: Final = frozenset(
    {GENERIC_RUNTIME_CAPABILITY, MCP_READONLY_CAPABILITY, "runtime_cancel", "idempotent_retry"},
)


@dataclass(frozen=True)
class ScenarioAcceptance:
    allowed_target_paths: tuple[str, ...]
    required_test_literals: tuple[str, ...]
    required_code_fragments: tuple[str, ...]


@dataclass(frozen=True)
class McpExpectation:
    server_name: str
    allowed_tool_names: tuple[str, ...]
    unapproved_tool_names: tuple[str, ...]
    resource_uris: tuple[str, ...]
    resource_templates: tuple[str, ...]
    read_resource_uri: str


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    purpose: str
    capability: str
    input_text: str
    feedback_comment: str | None
    source_ref: str
    reviewed_by: str
    reviewed_at: str
    acceptance: ScenarioAcceptance | None
    mcp_expectation: McpExpectation | None


@dataclass(frozen=True)
class ReviewedScenarioSet:
    agent_id: str
    scenarios: tuple[Scenario, ...]
    sha256: str


@dataclass(frozen=True)
class SseEventEvidence:
    event_type: str
    reply_id: str | None
    text_delta: str | None
    tool_call_id: str | None
    tool_call_name: str | None
    tool_result_state: str | None


@dataclass(frozen=True)
class ScenarioSchemaContract:
    root_required: frozenset[str]
    scenario_required: frozenset[str]
    scenario_allowed: frozenset[str]
    purposes: frozenset[str]
    acceptance_allowed: frozenset[str]
    mcp_expectation_allowed: frozenset[str]
    capability_by_purpose: Mapping[str, str]


def _is_inside_repo(path: Path) -> bool:
    try:
        path.relative_to(REPO_ROOT)
    except ValueError:
        return False
    return True


def _validated_reviewed_at(value: object, index: int) -> str:
    reviewed_at = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LiveAcceptanceError(f"场景 {index} reviewed_at 必须是 ISO-8601 时间") from exc
    if parsed.tzinfo is None:
        raise LiveAcceptanceError(f"场景 {index} reviewed_at 必须包含时区")
    return reviewed_at


def _string_array(value: object, *, label: str, required: bool) -> tuple[str, ...]:
    if value is None and not required:
        return ()
    if not isinstance(value, list) or (required and not value):
        raise LiveAcceptanceError(f"{label} 必须是{'非空' if required else ''} string array")
    normalized = tuple(str(item).strip() for item in value)
    if any(not item for item in normalized) or len(set(normalized)) != len(normalized):
        raise LiveAcceptanceError(f"{label} 不能包含空值或重复值")
    return normalized


def _scenario_schema_contract() -> ScenarioSchemaContract:
    try:
        schema = json.loads(SCENARIO_SCHEMA_PATH.read_text(encoding="utf-8"))
        scenario_schema = schema["$defs"]["scenario"]
        acceptance_schema = schema["$defs"]["acceptance"]
        mcp_expectation_schema = schema["$defs"]["mcp_expectation"]
        purposes = frozenset(scenario_schema["properties"]["purpose"]["enum"])
        capabilities = frozenset(scenario_schema["properties"]["capability"]["enum"])
        capability_by_purpose = scenario_schema["x-agentgov-capability-by-purpose"]
        if (
            not isinstance(capability_by_purpose, dict)
            or frozenset(capability_by_purpose) != purposes
            or frozenset(capability_by_purpose.values()) != capabilities
        ):
            raise ValueError("purpose/capability mapping mismatch")
        return ScenarioSchemaContract(
            root_required=frozenset(schema["required"]),
            scenario_required=frozenset(scenario_schema["required"]),
            scenario_allowed=frozenset(scenario_schema["properties"]),
            purposes=purposes,
            acceptance_allowed=frozenset(acceptance_schema["properties"]),
            mcp_expectation_allowed=frozenset(mcp_expectation_schema["properties"]),
            capability_by_purpose=capability_by_purpose,
        )
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise LiveAcceptanceError("仓库中的正式验收场景 schema 无效") from exc


def _scenario_acceptance(
    value: object,
    *,
    index: int,
    allowed_fields: Set[str],
) -> ScenarioAcceptance | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - allowed_fields:
        raise LiveAcceptanceError(f"场景 {index} acceptance 字段不符合正式验收 schema")
    return ScenarioAcceptance(
        allowed_target_paths=_string_array(
            value.get("allowed_target_paths"),
            label=f"场景 {index} acceptance.allowed_target_paths",
            required=True,
        ),
        required_test_literals=_string_array(
            value.get("required_test_literals"),
            label=f"场景 {index} acceptance.required_test_literals",
            required=False,
        ),
        required_code_fragments=_string_array(
            value.get("required_code_fragments"),
            label=f"场景 {index} acceptance.required_code_fragments",
            required=False,
        ),
    )


def _mcp_expectation(
    value: object,
    *,
    index: int,
    allowed_fields: Set[str],
) -> McpExpectation | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != allowed_fields:
        raise LiveAcceptanceError(f"场景 {index} mcp_expectation 字段不符合正式验收 schema")
    expectation = McpExpectation(
        server_name=str(value.get("server_name") or "").strip(),
        allowed_tool_names=_string_array(
            value.get("allowed_tool_names"),
            label=f"场景 {index} mcp_expectation.allowed_tool_names",
            required=True,
        ),
        unapproved_tool_names=_string_array(
            value.get("unapproved_tool_names"),
            label=f"场景 {index} mcp_expectation.unapproved_tool_names",
            required=True,
        ),
        resource_uris=_string_array(
            value.get("resource_uris"),
            label=f"场景 {index} mcp_expectation.resource_uris",
            required=True,
        ),
        resource_templates=_string_array(
            value.get("resource_templates"),
            label=f"场景 {index} mcp_expectation.resource_templates",
            required=True,
        ),
        read_resource_uri=str(value.get("read_resource_uri") or "").strip(),
    )
    expected_contract = (
        MCP_TECHNICAL_SERVER_NAME,
        (MCP_TECHNICAL_RAW_TOOL_NAME,),
        MCP_TECHNICAL_UNAPPROVED_RAW_TOOLS,
        (MCP_TECHNICAL_RESOURCE_URI,),
        (MCP_TECHNICAL_RESOURCE_TEMPLATE,),
        MCP_TECHNICAL_RESOURCE_URI,
    )
    actual_contract = (
        expectation.server_name,
        expectation.allowed_tool_names,
        frozenset(expectation.unapproved_tool_names),
        expectation.resource_uris,
        expectation.resource_templates,
        expectation.read_resource_uri,
    )
    if actual_contract != expected_contract:
        raise LiveAcceptanceError(f"场景 {index} mcp_expectation 必须精确匹配平台只读 MCP 验收契约")
    return expectation


def _parse_scenario(
    value: object,
    *,
    index: int,
    required_fields: Set[str],
    allowed_fields: Set[str],
    purposes: frozenset[str],
    acceptance_fields: frozenset[str],
    mcp_expectation_fields: frozenset[str],
    capability_by_purpose: Mapping[str, str],
) -> Scenario:
    if not isinstance(value, dict):
        raise LiveAcceptanceError(f"场景 {index} 必须是 JSON object")
    if set(value) - allowed_fields or required_fields - set(value):
        raise LiveAcceptanceError(f"场景 {index} 字段不符合正式验收 schema")
    scenario_id = str(value.get("scenario_id") or "").strip()
    purpose = str(value.get("purpose") or "").strip()
    capability = str(value.get("capability") or "").strip()
    input_text = str(value.get("input") or "").strip()
    feedback_value = value.get("feedback_comment")
    feedback = str(feedback_value).strip() if feedback_value is not None else None
    source_ref = str(value.get("source_ref") or "").strip()
    reviewed_by = str(value.get("reviewed_by") or "").strip()
    reviewed_at = _validated_reviewed_at(value.get("reviewed_at"), index)
    if not scenario_id or not input_text or not source_ref or not reviewed_by:
        raise LiveAcceptanceError(f"场景 {index} 存在空的必填字段")
    if purpose not in purposes:
        raise LiveAcceptanceError(f"场景 {index} purpose 不受支持")
    if capability != capability_by_purpose[purpose]:
        raise LiveAcceptanceError(f"场景 {index} capability 与 purpose 不匹配")
    acceptance = _scenario_acceptance(value.get("acceptance"), index=index, allowed_fields=acceptance_fields)
    mcp_expectation = _mcp_expectation(
        value.get("mcp_expectation"),
        index=index,
        allowed_fields=mcp_expectation_fields,
    )
    if purpose == "improvement" and (not feedback or acceptance is None or not acceptance.required_test_literals):
        raise LiveAcceptanceError(f"场景 {index} 的 improvement 场景必须提供 feedback_comment、非空 acceptance.allowed_target_paths 与 required_test_literals")
    if (purpose == "mcp_readonly") != (mcp_expectation is not None):
        raise LiveAcceptanceError(f"场景 {index} 只有 mcp_readonly 必须且可以提供 mcp_expectation")
    return Scenario(
        scenario_id,
        purpose,
        capability,
        input_text,
        feedback,
        source_ref,
        reviewed_by,
        reviewed_at,
        acceptance,
        mcp_expectation,
    )


def load_scenarios(path: Path, *, expected_agent_id: str) -> ReviewedScenarioSet:
    resolved = path.resolve()
    if _is_inside_repo(resolved):
        raise LiveAcceptanceError("真实验收场景文件必须位于源码仓库外")
    try:
        raw = resolved.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise LiveAcceptanceError("场景文件必须是可读 JSON") from exc
    contract = _scenario_schema_contract()
    if not isinstance(payload, dict) or set(payload) != contract.root_required:
        raise LiveAcceptanceError("场景文件必须只包含 agent_id 与 scenarios")
    agent_id = str(payload.get("agent_id") or "").strip()
    if not agent_id or agent_id != expected_agent_id:
        raise LiveAcceptanceError("场景文件 agent_id 必须与正式验收 Agent 完全一致")
    items = payload.get("scenarios")
    if not isinstance(items, list) or not items:
        raise LiveAcceptanceError("场景文件 scenarios 必须是非空 JSON array")
    scenarios = tuple(
        _parse_scenario(
            item,
            index=index,
            required_fields=contract.scenario_required,
            allowed_fields=contract.scenario_allowed,
            purposes=contract.purposes,
            acceptance_fields=contract.acceptance_allowed,
            mcp_expectation_fields=contract.mcp_expectation_allowed,
            capability_by_purpose=contract.capability_by_purpose,
        )
        for index, item in enumerate(items)
    )
    if len({item.scenario_id for item in scenarios}) != len(scenarios):
        raise LiveAcceptanceError("场景 scenario_id 必须唯一")
    if len({" ".join(item.input_text.split()) for item in scenarios}) != len(scenarios):
        raise LiveAcceptanceError("场景 input 必须实质不同，不能重复同一输入")
    return ReviewedScenarioSet(agent_id, scenarios, hashlib.sha256(raw).hexdigest())


def select_scenarios(
    scenarios: tuple[Scenario, ...],
    runs: int,
    concurrency: int,
    *,
    capability: str = GENERIC_RUNTIME_CAPABILITY,
) -> tuple[Scenario, ...]:
    if runs < 1:
        raise LiveAcceptanceError("--runs 必须大于零")
    if concurrency < 1 or concurrency > runs:
        raise LiveAcceptanceError("--concurrency 必须在 1 与 --runs 之间")
    if capability not in RUNTIME_VERIFIED_CAPABILITIES:
        raise LiveAcceptanceError(f"capability {capability} 尚无精确 Runtime 验证器，不能用普通非空回复充当平台能力证据")
    matching = tuple(item for item in scenarios if item.capability == capability)
    if runs > len(matching):
        raise LiveAcceptanceError(
            f"capability {capability} 要求 {runs} 次实质不同 run，但场景文件只有 {len(matching)} 条；不同 capability 不得混算，且不得循环复制场景制造配额证据"
        )
    return matching[:runs]


def parse_sse_events(raw: bytes) -> tuple[SseEventEvidence, ...]:
    """解析已完整分隔的 SSE data frame；非法 data 不得被忽略。"""

    normalized = raw.replace(b"\r\n", b"\n")
    frames = normalized.split(b"\n\n")
    if not normalized.endswith(b"\n\n"):
        frames.pop()
    events: list[SseEventEvidence] = []
    for index, frame in enumerate(frames, start=1):
        data_lines = [b"" if line == b"data" else line[5:].lstrip() for line in frame.split(b"\n") if line == b"data" or line.startswith(b"data:")]
        if not data_lines:
            continue
        data = b"\n".join(data_lines)
        try:
            payload = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LiveAcceptanceError(f"SSE data frame {index} 不是合法 JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("type"), str) or not payload["type"].strip():
            raise LiveAcceptanceError(f"SSE data frame {index} 缺少非空 type")
        reply_value = payload.get("reply_id")
        reply_id = reply_value.strip() if isinstance(reply_value, str) else None
        delta_value = payload.get("delta")
        text_delta = delta_value if isinstance(delta_value, str) else None
        tool_call_id_value = payload.get("tool_call_id")
        tool_call_id = tool_call_id_value.strip() if isinstance(tool_call_id_value, str) else None
        tool_call_name_value = payload.get("tool_call_name")
        tool_call_name = tool_call_name_value.strip() if isinstance(tool_call_name_value, str) else None
        state_value = payload.get("state")
        tool_result_state = state_value.strip() if isinstance(state_value, str) else None
        events.append(
            SseEventEvidence(
                payload["type"].strip(),
                reply_id,
                text_delta,
                tool_call_id,
                tool_call_name,
                tool_result_state,
            ),
        )
    return tuple(events)


def parse_sse_event_types(raw: bytes) -> tuple[str, ...]:
    return tuple(event.event_type for event in parse_sse_events(raw))


def has_nonempty_sse_text(raw: bytes) -> bool:
    return any(event.event_type == "TEXT_BLOCK_DELTA" and bool(event.text_delta and event.text_delta.strip()) for event in parse_sse_events(raw))


def validate_sse_evidence(
    raw: bytes,
    *,
    purpose: str,
    terminal_reply_ids: tuple[str, ...],
) -> tuple[str, ...]:
    """按场景语义校验 SSE reply 链，并与持久终态精确对账。"""

    events = parse_sse_events(raw)
    if not events:
        if purpose == "early_cancel" and not terminal_reply_ids:
            return ()
        raise LiveAcceptanceError(f"{purpose} 场景 SSE 未观测到 AgentScope 原生事件")

    active_reply_id: str | None = None
    active_has_text = False
    observed_nonempty_text = False
    completed_reply_ids: list[str] = []
    for event in events:
        if event.event_type == "REPLY_START":
            if not event.reply_id:
                raise LiveAcceptanceError("SSE REPLY_START 缺少非空 reply_id")
            if active_reply_id not in {None, event.reply_id}:
                raise LiveAcceptanceError("SSE 在前一 reply 终态前启动了不同 reply_id")
            if active_reply_id is None:
                active_reply_id = event.reply_id
                active_has_text = False
        elif event.event_type == "TEXT_BLOCK_DELTA":
            if not event.reply_id or event.reply_id != active_reply_id:
                raise LiveAcceptanceError("SSE TEXT_BLOCK_DELTA 未按 REPLY_START 的 reply_id 到达")
            if event.text_delta is None:
                raise LiveAcceptanceError("SSE TEXT_BLOCK_DELTA 缺少 string delta")
            if event.text_delta.strip():
                active_has_text = True
                observed_nonempty_text = True
        elif event.event_type == "REPLY_END":
            if not event.reply_id or event.reply_id != active_reply_id:
                raise LiveAcceptanceError("SSE REPLY_END 未按 REPLY_START 的 reply_id 到达")
            if purpose in {"success", "retry", "mcp_readonly"} and not active_has_text:
                raise LiveAcceptanceError("SSE 成功 reply 在 REPLY_END 前没有非空 TEXT_BLOCK_DELTA")
            completed_reply_ids.append(event.reply_id)
            active_reply_id = None
            active_has_text = False

    if purpose in {"success", "retry", "mcp_readonly"}:
        if active_reply_id is not None:
            raise LiveAcceptanceError("SSE 成功 reply 缺少 REPLY_END")
        if not observed_nonempty_text or not completed_reply_ids:
            raise LiveAcceptanceError("SSE 成功链缺少 REPLY_START、非空 TEXT_BLOCK_DELTA 或 REPLY_END")
    elif purpose == "partial_cancel" and not observed_nonempty_text:
        raise LiveAcceptanceError("partial_cancel 场景 SSE 没有非空文本增量")
    elif purpose not in {"early_cancel", "partial_cancel"}:
        raise LiveAcceptanceError(f"场景 purpose {purpose} 没有 SSE 验收契约")

    observed_terminal = tuple(completed_reply_ids)
    if observed_terminal != terminal_reply_ids:
        raise LiveAcceptanceError("SSE REPLY_END reply_id 与持久终态 reply_ids 不精确一致")
    return tuple(event.event_type for event in events)


def _parse_server_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise LiveAcceptanceError(f"并发证据缺少服务端 {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LiveAcceptanceError(f"并发证据的服务端 {field} 无效") from exc
    if parsed.tzinfo is None:
        raise LiveAcceptanceError(f"并发证据的服务端 {field} 缺少时区")
    return parsed


def observed_run_concurrency(runs: tuple[Mapping[str, object], ...]) -> int:
    """计算服务端确认的真实 run 生命周期重叠数；客户端 task 数不能作为证据。"""

    terminal_statuses = frozenset({"succeeded", "failed", "cancelled", "interrupted"})
    events: list[tuple[datetime, int]] = []
    for run in runs:
        if run.get("status") not in terminal_statuses:
            raise LiveAcceptanceError("并发证据包含非终态 run")
        started_at = _parse_server_timestamp(run.get("started_at"), "started_at")
        completed_at = _parse_server_timestamp(run.get("completed_at"), "completed_at")
        if completed_at <= started_at:
            raise LiveAcceptanceError("并发证据的 completed_at 必须晚于 started_at")
        events.extend(((started_at, 1), (completed_at, -1)))
    active = 0
    peak = 0
    for _timestamp, delta in sorted(events, key=lambda item: (item[0], item[1])):
        active += delta
        if active < 0:
            raise LiveAcceptanceError("并发证据时间线无效")
        peak = max(peak, active)
    if active != 0:
        raise LiveAcceptanceError("并发证据时间线未闭合")
    return peak


def validate_evidence_identities(
    *,
    expected_runs: int,
    configured_concurrency: int,
    max_concurrency_observed: int,
    scenario_ids: tuple[str, ...],
    session_ids: tuple[str, ...],
    run_ids: tuple[str, ...],
    trace_ids: tuple[str, ...],
    reply_ids: tuple[str, ...],
    expected_capability: str,
    capabilities: tuple[str, ...],
) -> None:
    if len(capabilities) != expected_runs or any(item != expected_capability for item in capabilities):
        raise LiveAcceptanceError("验收汇总的 capability 数量不足或混入其他能力场景")
    for label, values in (
        ("scenario_id", scenario_ids),
        ("session_id", session_ids),
        ("run_id", run_ids),
        ("trace_id", trace_ids),
    ):
        if len(values) != expected_runs or len(set(values)) != expected_runs:
            raise LiveAcceptanceError(f"验收汇总中 {label} 数量不足或跨场景重复")
    if len(reply_ids) != len(set(reply_ids)):
        raise LiveAcceptanceError("验收汇总中 reply_id 跨场景重复")
    expected_peak = min(expected_runs, configured_concurrency)
    if max_concurrency_observed != expected_peak:
        raise LiveAcceptanceError(f"服务端 run 生命周期重叠未达到配置峰值 {expected_peak}")
