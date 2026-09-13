from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Literal, cast

import yaml
from agentscope.agent import Agent
from agentscope.message import ToolCallBlock, ToolCallState
from agentscope.permission import PermissionBehavior, PermissionContext, PermissionMode, PermissionRule
from agentscope.state import AgentState
from agentscope.tool import Bash, FunctionTool, Read, ToolBase, Write
from agentscope_runtime.context_registry import RuntimeContext
from agentscope_runtime.policy_middleware import AgentGovPolicyMiddleware
from agentscope_runtime.receipt_middleware import CURRENT_RUNTIME_CONTEXT
from agentscope_runtime.subagent_templates import load_subagent_templates

_BUILTIN_WORKSPACE = Path(__file__).resolve().parents[1] / "docker/runtime-bootstrap/business-agents/security-operations-expert/workspace"


def _runtime_context(role: Literal["root", "worker"]) -> RuntimeContext:
    return RuntimeContext(
        run_id="run-worker-policy",
        session_id=f"{role}-session",
        root_session_id="root-session",
        role=role,
        agent_id="security-operations-expert",
        agent_version_id="version-1",
        runtime_agent_id="runtime-security-operations-expert",
        harness_digest="a" * 64,
        trace_id="1" * 32,
        team_generation=1 if role == "worker" else 0,
    )


def _agent(
    middleware: AgentGovPolicyMiddleware,
    permission_context: PermissionContext,
) -> Agent:
    return Agent(
        "permission-test-agent",
        "Policy integration test",
        cast(Any, object()),
        state=AgentState(permission_context=permission_context),
        middlewares=[middleware],
    )


def _check(
    agent: Agent,
    tool_call: ToolCallBlock,
    tool: ToolBase,
    tool_input: dict[str, Any],
    *,
    role: Literal["root", "worker"],
):
    async def invoke():
        token = CURRENT_RUNTIME_CONTEXT.set(_runtime_context(role))
        try:
            return await agent._check_permission(tool_call, tool, tool_input)  # noqa: SLF001 - pinned AgentScope middleware contract
        finally:
            CURRENT_RUNTIME_CONTEXT.reset(token)

    return asyncio.run(invoke())


def _write_policy_workspace(root: Path) -> None:
    root.mkdir(exist_ok=True)
    (root / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "session": {"permission_mode": "default"},
                "workspace_policy": {
                    "fail_closed": True,
                    "immutable_harness": True,
                    "allowed_tools": ["Read(outputs/**)"],
                    "ask_tools": ["Write(outputs/**)"],
                    "denied_tools": ["Write(outputs/blocked/**)"],
                    "immutable_paths": ["AGENT.md", "agent.yaml", "skills/**", "mcp/**", "subagents/**"],
                    "writable_paths": ["outputs/**"],
                    "denied_read_paths": [".env"],
                    "allowed_network_domains": [],
                    "sandbox": {
                        "enabled": True,
                        "fail_if_unavailable": True,
                        "allow_unsandboxed_commands": False,
                    },
                },
                "runtime_middlewares": [{"type": "policy_guard"}],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_confirmed_tool_call_uses_agentscope_core_after_agentgov_safety_gates(
    tmp_path: Path,
) -> None:
    _write_policy_workspace(tmp_path)
    middleware = AgentGovPolicyMiddleware(tmp_path)
    agent = _agent(middleware, PermissionContext(mode=PermissionMode.DEFAULT))
    write = Write()

    pending = _check(
        agent,
        ToolCallBlock(id="pending", name="Write", input='{"file_path":"outputs/report.md","content":"ok"}'),
        write,
        {"file_path": "outputs/report.md", "content": "ok"},
        role="root",
    )
    confirmed = _check(
        agent,
        ToolCallBlock(
            id="confirmed",
            name="Write",
            input='{"file_path":"outputs/report.md","content":"ok"}',
            state=ToolCallState.ALLOWED,
        ),
        write,
        {"file_path": "outputs/report.md", "content": "ok"},
        role="root",
    )

    assert pending.behavior is PermissionBehavior.ASK
    assert confirmed.behavior is PermissionBehavior.ALLOW
    assert confirmed.message == "Already allowed by user confirmation."


def test_confirmed_tool_call_cannot_bypass_agentgov_safety_gates(tmp_path: Path) -> None:
    _write_policy_workspace(tmp_path)
    middleware = AgentGovPolicyMiddleware(tmp_path)
    agent = _agent(middleware, PermissionContext(mode=PermissionMode.DEFAULT))
    write = Write()

    protected = _check(
        agent,
        ToolCallBlock(id="protected", name="Write", input='{"file_path":"agent.yaml","content":"unsafe"}', state=ToolCallState.ALLOWED),
        write,
        {"file_path": "agent.yaml", "content": "unsafe"},
        role="root",
    )
    denied = _check(
        agent,
        ToolCallBlock(
            id="denied",
            name="Write",
            input='{"file_path":"outputs/blocked/report.md","content":"unsafe"}',
            state=ToolCallState.ALLOWED,
        ),
        write,
        {"file_path": "outputs/blocked/report.md", "content": "unsafe"},
        role="root",
    )
    explore = _check(
        _agent(middleware, PermissionContext(mode=PermissionMode.EXPLORE)),
        ToolCallBlock(
            id="explore",
            name="Write",
            input='{"file_path":"outputs/report.md","content":"unsafe"}',
            state=ToolCallState.ALLOWED,
        ),
        write,
        {"file_path": "outputs/report.md", "content": "unsafe"},
        role="root",
    )
    bypass = _check(
        _agent(middleware, PermissionContext(mode=PermissionMode.BYPASS)),
        ToolCallBlock(
            id="bypass",
            name="Write",
            input='{"file_path":"outputs/report.md","content":"unsafe"}',
            state=ToolCallState.ALLOWED,
        ),
        write,
        {"file_path": "outputs/report.md", "content": "unsafe"},
        role="root",
    )

    assert protected.behavior is PermissionBehavior.DENY
    assert protected.decision_reason == "AgentGov Harness assets are immutable"
    assert denied.behavior is PermissionBehavior.DENY
    assert denied.decision_reason == "Denied by AgentGov Harness policy"
    assert explore.behavior is PermissionBehavior.DENY
    assert explore.decision_reason == "AgentGov Runtime explore mode is read-only"
    assert bypass.behavior is PermissionBehavior.DENY
    assert bypass.decision_reason == "AgentGov Runtime forbids bypass permission mode"


def test_real_builtin_template_allows_worker_baseline_and_declared_tools_only() -> None:
    templates = load_subagent_templates(_BUILTIN_WORKSPACE, "a" * 64)
    template = templates["agentgov-" + "a" * 64 + "-response-playbook-builder"]
    middleware = AgentGovPolicyMiddleware(
        _BUILTIN_WORKSPACE,
        tool_workdir="/workspace",
        environ={"SEC_OPS_MCP_URL": "http://host.docker.internal:58001/mcp"},
    )
    agent = _agent(middleware, template.permission_context.model_copy(deep=True))

    def team_say(to: str, content: str) -> str:
        return f"{to}:{content}"

    team_say_tool = FunctionTool(team_say, name="TeamSay")

    report = _check(
        agent,
        ToolCallBlock(id="report", name="TeamSay", input='{"to":"leader","content":"done"}'),
        team_say_tool,
        {"to": "leader", "content": "done"},
        role="worker",
    )
    declared = _check(
        agent,
        ToolCallBlock(id="read", name="Read", input='{"file_path":"/workspace/data/alerts.json"}'),
        Read(),
        {"file_path": "/workspace/data/alerts.json"},
        role="worker",
    )
    inherited = _check(
        agent,
        ToolCallBlock(id="bash", name="Bash", input='{"command":"date"}', state=ToolCallState.ALLOWED),
        Bash(),
        {"command": "date"},
        role="worker",
    )

    assert report.behavior is PermissionBehavior.ALLOW
    assert report.decision_reason == "agentgov.worker.minimum_tools"
    assert declared.behavior is PermissionBehavior.ALLOW
    assert declared.decision_reason == "agentgov.subagent.allowed_tools"
    assert inherited.behavior is PermissionBehavior.DENY
    assert inherited.decision_reason == "Tool is not explicitly allowed for the current AgentGov subagent"


def test_empty_worker_context_gets_only_teamsay_not_root_permissions() -> None:
    middleware = AgentGovPolicyMiddleware(
        _BUILTIN_WORKSPACE,
        tool_workdir="/workspace",
        environ={"SEC_OPS_MCP_URL": "http://host.docker.internal:58001/mcp"},
    )
    agent = _agent(middleware, PermissionContext(mode=PermissionMode.DEFAULT))

    decision = _check(
        agent,
        ToolCallBlock(id="read", name="Read", input='{"file_path":"/workspace/data/alerts.json"}'),
        Read(),
        {"file_path": "/workspace/data/alerts.json"},
        role="worker",
    )

    assert decision.behavior is PermissionBehavior.DENY
    assert decision.decision_reason == "Tool is not explicitly allowed for the current AgentGov subagent"


def test_worker_mcp_wildcard_deny_precedes_exact_declared_allow(tmp_path: Path) -> None:
    _write_policy_workspace(tmp_path)
    middleware = AgentGovPolicyMiddleware(tmp_path)
    tool_name = "mcp__sec_ops__create_case"
    source = "agentgov-subagent:" + "a" * 64 + ":worker"
    context = PermissionContext(
        mode=PermissionMode.DONT_ASK,
        allow_rules={
            tool_name: [
                PermissionRule(
                    tool_name=tool_name,
                    rule_content=None,
                    behavior=PermissionBehavior.ALLOW,
                    source=source,
                ),
            ],
        },
        deny_rules={
            "mcp__sec_ops__create*": [
                PermissionRule(
                    tool_name="mcp__sec_ops__create*",
                    rule_content=None,
                    behavior=PermissionBehavior.DENY,
                    source=source,
                ),
            ],
        },
    )
    agent = _agent(middleware, context)

    def create_case(case_id: str) -> str:
        return case_id

    tool = FunctionTool(create_case, name=tool_name)
    decision = _check(
        agent,
        ToolCallBlock(id="mcp", name=tool_name, input=json.dumps({"case_id": "case-1"})),
        tool,
        {"case_id": "case-1"},
        role="worker",
    )

    assert decision.behavior is PermissionBehavior.DENY
    assert decision.decision_reason == "Denied by the current AgentGov subagent policy"
