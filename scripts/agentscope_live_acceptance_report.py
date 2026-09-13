"""AgentScope live 验收的脱敏证据模型与 JSON 边界投影。"""

from __future__ import annotations

from dataclasses import dataclass

from app.runtime.json_types import JsonObject

from scripts.agentscope_live_acceptance_scenarios import GENERIC_RUNTIME_CAPABILITY
from scripts.agentscope_mcp_live_acceptance import McpAcceptanceEvidence


@dataclass(frozen=True)
class BindingEvidence:
    """公开 current 响应确认的不可变发布身份，不是客户端自造的 Agent ID。"""

    governance_agent_id: str
    runtime_agent_id: str
    agent_version_id: str
    harness_digest: str


@dataclass(frozen=True)
class RunEvidence:
    scenario_id: str
    purpose: str
    capability: str
    binding: BindingEvidence
    session_id: str
    run_id: str
    reply_ids: tuple[str, ...]
    trace_id: str
    trace_status: str
    feedback_signal_id: str | None
    sse_event_types: tuple[str, ...]
    operation_replayed: bool
    mcp: McpAcceptanceEvidence | None = None


@dataclass(frozen=True)
class TerminalEvidence:
    """已通过 run/session/reply/trace 身份校验的终态证据。"""

    reply_ids: tuple[str, ...]
    trace_id: str


def build_live_acceptance_summary(
    *,
    acceptance_scope: str,
    excluded_claims: tuple[str, ...],
    scenario_file_sha256: str,
    acceptance_run_id: str,
    configured_concurrency: int,
    max_concurrency_observed: int,
    requested_runs: int,
    requested_capability: str,
    evidence: list[RunEvidence],
) -> JsonObject:
    """投影可公开的身份、状态与摘要；MCP 参数和返回正文不进入报告。"""

    return {
        "schema_version": 1,
        "runtime": "agentscope",
        "acceptance_scope": acceptance_scope,
        "excluded_claims": list(excluded_claims),
        "scenario_file_sha256": scenario_file_sha256,
        "acceptance_run_id": acceptance_run_id,
        "configured_concurrency": configured_concurrency,
        "max_concurrency_observed": max_concurrency_observed,
        "requested_runs": requested_runs,
        "requested_capability": requested_capability,
        "generic_runtime_run_count": sum(item.capability == GENERIC_RUNTIME_CAPABILITY for item in evidence),
        "platform_capability_run_count": sum(item.capability != GENERIC_RUNTIME_CAPABILITY for item in evidence),
        "runs": [
            {
                "scenario_id": item.scenario_id,
                "purpose": item.purpose,
                "capability": item.capability,
                "governance_agent_id": item.binding.governance_agent_id,
                "runtime_agent_id": item.binding.runtime_agent_id,
                "agent_version_id": item.binding.agent_version_id,
                "harness_digest": item.binding.harness_digest,
                "session_id": item.session_id,
                "run_id": item.run_id,
                "reply_ids": list(item.reply_ids),
                "trace_id": item.trace_id,
                "trace_status": item.trace_status,
                "feedback_signal_id": item.feedback_signal_id,
                "sse_event_types": list(item.sse_event_types),
                "operation_replayed": item.operation_replayed,
                "mcp": item.mcp.summary() if item.mcp is not None else None,
            }
            for item in evidence
        ],
    }
