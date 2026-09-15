from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from agentscope.permission import PermissionBehavior, PermissionContext, PermissionMode, PermissionRule
from agentscope.state import AgentState
from agentscope.tool import Bash, Edit, FunctionTool, Read, ToolBase, Write
from agentscope_runtime.context_registry import RuntimeContext
from agentscope_runtime.policy_middleware import AgentGovPolicyMiddleware
from agentscope_runtime.receipt_middleware import CURRENT_RUNTIME_CONTEXT
from agentscope_runtime.subagent_templates import load_subagent_templates
from app.runtime.agent_profiles import read_requires_human_confirmation
from app.runtime.execution_content_guards import ExecutionContentGuardError, guard_execution_write
from app.runtime.managed_agent_policy import plan_workspace_policy

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from scripts.check_agentscope_cutover import _check_permission_policy  # noqa: E402
from scripts.convert_claude_harness import _workspace_policy  # noqa: E402


@dataclass
class PolicyAgent:
    state: AgentState


def _manifest(
    *,
    allowed_tools: object = None,
    ask_tools: object = None,
    denied_tools: object = None,
) -> dict[str, Any]:
    policy: dict[str, Any] = {
        "fail_closed": True,
        "immutable_harness": True,
        "allow_for_run": False,
        "allowed_tools": [] if allowed_tools is None else allowed_tools,
        "denied_tools": [] if denied_tools is None else denied_tools,
        "denied_read_paths": [".env", "**/.env", "**/*credential*"],
        "immutable_paths": ["AGENT.md", "agent.yaml", "skills/**", "mcp/**", "subagents/**"],
        "writable_paths": ["outputs/**"],
        "allowed_network_domains": [],
        "sandbox": {
            "enabled": True,
            "fail_if_unavailable": True,
            "allow_unsandboxed_commands": False,
        },
    }
    if ask_tools is not None:
        policy["ask_tools"] = ask_tools
    return {
        "schema_version": 1,
        "agent": {
            "id": "policy-agent",
            "runtime": "agentscope",
            "runtime_contract": "agentscope-app/2.0.8",
        },
        "session": {"permission_mode": "default"},
        "runtime_middlewares": [{"type": "policy_guard"}],
        "workspace_policy": policy,
    }


def _write_workspace(root: Path, manifest: dict[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "AGENT.md").write_text("# Policy agent\n", encoding="utf-8")
    (root / "agent.yaml").write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _agent(mode: PermissionMode, *, allow_rules: dict[str, list[PermissionRule]] | None = None) -> PolicyAgent:
    return PolicyAgent(
        state=AgentState(
            permission_context=PermissionContext(
                mode=mode,
                allow_rules=allow_rules or {},
            ),
        ),
    )


def _decide(
    middleware: AgentGovPolicyMiddleware,
    agent: Any,
    tool: ToolBase,
    tool_input: dict[str, Any],
    *,
    runtime_context: RuntimeContext | None = None,
):
    async def unexpected_next_handler(**_: object):
        raise AssertionError("AgentGov policy must produce the final permission decision")

    async def invoke():
        token = CURRENT_RUNTIME_CONTEXT.set(runtime_context)
        try:
            return await middleware.on_check_permission(
                agent,
                {"tool": tool, "tool_input": tool_input},
                unexpected_next_handler,
            )
        finally:
            CURRENT_RUNTIME_CONTEXT.reset(token)

    return asyncio.run(invoke())


def _runtime_context(run_id: str) -> RuntimeContext:
    return RuntimeContext(
        run_id=run_id,
        session_id="session-1",
        root_session_id="session-1",
        role="root",
        agent_id="policy-agent",
        agent_version_id="version-1",
        runtime_agent_id="runtime-policy-agent",
        harness_digest="a" * 64,
        trace_id="1" * 32,
        team_generation=0,
    )


def test_ask_precedes_broader_static_allow_and_suggestion_is_bounded(tmp_path: Path) -> None:
    _write_workspace(
        tmp_path,
        _manifest(
            allowed_tools=["Write(outputs/**)"],
            ask_tools=["Write(outputs/review/**)"],
        ),
    )
    decision = _decide(
        AgentGovPolicyMiddleware(tmp_path),
        _agent(PermissionMode.DEFAULT),
        Write(),
        {"file_path": "outputs/review/finding.md", "content": "review"},
    )

    assert decision.behavior is PermissionBehavior.ASK
    assert decision.decision_reason == "workspace_policy.ask_tools"
    assert [rule.model_dump(mode="json") for rule in decision.suggested_rules or []] == [
        {
            "tool_name": "Write",
            "rule_content": "outputs/review/**",
            "behavior": "allow",
            "source": "workspace_policy.ask_tools",
        },
    ]


def test_explicit_deny_and_hard_guard_precede_ask(tmp_path: Path) -> None:
    _write_workspace(
        tmp_path,
        _manifest(
            ask_tools=["Write(outputs/review/**)", "Write(agent.yaml)"],
            denied_tools=["Write(outputs/review/blocked/**)"],
        ),
    )
    middleware = AgentGovPolicyMiddleware(tmp_path)
    run_rule = PermissionRule(
        tool_name="Write",
        rule_content="outputs/review/blocked/**",
        behavior=PermissionBehavior.ALLOW,
        source="agentgov-run:run-1",
    )
    agent = _agent(PermissionMode.DEFAULT, allow_rules={"Write": [run_rule]})

    explicit = _decide(
        middleware,
        agent,
        Write(),
        {"file_path": "outputs/review/blocked/finding.md", "content": "blocked"},
        runtime_context=_runtime_context("run-1"),
    )
    protected = _decide(
        middleware,
        agent,
        Write(),
        {"file_path": "agent.yaml", "content": "unsafe"},
    )

    assert explicit.behavior is PermissionBehavior.DENY
    assert explicit.decision_reason == "Denied by AgentGov Harness policy"
    assert protected.behavior is PermissionBehavior.DENY
    assert protected.decision_reason == "AgentGov Harness assets are immutable"


def test_unlisted_tool_remains_denied_when_ask_tools_is_omitted(tmp_path: Path) -> None:
    _write_workspace(tmp_path, _manifest())

    def catalog_lookup(query: str) -> str:
        return query

    decision = _decide(
        AgentGovPolicyMiddleware(tmp_path),
        _agent(PermissionMode.DEFAULT),
        FunctionTool(catalog_lookup),
        {"query": "finding"},
    )

    assert decision.behavior is PermissionBehavior.DENY
    assert decision.suggested_rules is None


@pytest.mark.parametrize("file_path", ["references/guide.md", "./references/guide.md", "outputs/../references/guide.md"])
def test_reference_assets_are_readable_but_never_writable_with_broad_allow(tmp_path: Path, file_path: str) -> None:
    manifest = _manifest(allowed_tools=["Read(./references/**)", "Write", "Edit"], ask_tools=["Write(outputs/**)"])
    manifest["workspace_policy"]["writable_paths"] = ["**"]
    manifest["workspace_policy"]["immutable_paths"].append("references/**")
    _write_workspace(tmp_path, manifest)
    reference = tmp_path / "references/guide.md"
    reference.parent.mkdir()
    reference.write_text("固定版本参考资料", encoding="utf-8")
    middleware = AgentGovPolicyMiddleware(tmp_path)
    agent = _agent(PermissionMode.DEFAULT)
    assert _decide(middleware, agent, Read(), {"file_path": file_path}).behavior is PermissionBehavior.ALLOW
    for tool in (Write(), Edit()):
        decision = _decide(middleware, agent, tool, {"file_path": file_path, "content": "must not overwrite"})
        assert decision.behavior is PermissionBehavior.DENY
        assert decision.decision_reason == "AgentGov Harness assets are immutable"
    assert _decide(middleware, agent, Write(), {"file_path": "outputs/summary.md", "content": "requires confirmation"}).behavior is PermissionBehavior.ASK


def test_missing_permission_context_fails_closed_even_for_static_allow(tmp_path: Path) -> None:
    _write_workspace(tmp_path, _manifest(allowed_tools=["Read(outputs/**)"]))
    decision = _decide(
        AgentGovPolicyMiddleware(tmp_path),
        object(),
        Read(),
        {"file_path": "outputs/finding.md"},
    )
    assert decision.behavior is PermissionBehavior.DENY
    assert decision.decision_reason == "AgentGov Runtime received an invalid permission mode"


def test_run_scoped_allow_is_exact_and_expires_when_run_changes(tmp_path: Path) -> None:
    _write_workspace(tmp_path, _manifest(ask_tools=["Write(outputs/review/**)"]))
    run_rule = PermissionRule(
        tool_name="Write",
        rule_content="outputs/review/approved.md",
        behavior=PermissionBehavior.ALLOW,
        source="agentgov-run:run-1",
    )
    agent = _agent(PermissionMode.DEFAULT, allow_rules={"Write": [run_rule]})
    middleware = AgentGovPolicyMiddleware(tmp_path)
    tool = Write()

    approved = _decide(
        middleware,
        agent,
        tool,
        {"file_path": "outputs/review/approved.md", "content": "approved"},
        runtime_context=_runtime_context("run-1"),
    )
    sibling = _decide(
        middleware,
        agent,
        tool,
        {"file_path": "outputs/review/other.md", "content": "other"},
        runtime_context=_runtime_context("run-1"),
    )
    next_run = _decide(
        middleware,
        agent,
        tool,
        {"file_path": "outputs/review/approved.md", "content": "again"},
        runtime_context=_runtime_context("run-2"),
    )

    assert approved.behavior is PermissionBehavior.ALLOW
    assert approved.decision_reason == "agentgov.allow_for_run"
    assert sibling.behavior is PermissionBehavior.ASK
    assert next_run.behavior is PermissionBehavior.ASK
    assert agent.state.permission_context.allow_rules == {}


@pytest.mark.parametrize("tool", [Read(), Write()])
def test_run_scoped_path_rule_rejects_traversal_to_sibling(tmp_path: Path, tool: ToolBase) -> None:
    tool_name = tool.name
    _write_workspace(tmp_path, _manifest(ask_tools=[f"{tool_name}(outputs/review/**)"]))
    run_rule = PermissionRule(
        tool_name=tool_name,
        rule_content="outputs/review/**",
        behavior=PermissionBehavior.ALLOW,
        source="agentgov-run:run-1",
    )
    middleware = AgentGovPolicyMiddleware(tmp_path)
    agent = _agent(PermissionMode.DEFAULT, allow_rules={tool_name: [run_rule]})

    nested = _decide(
        middleware,
        agent,
        tool,
        {"file_path": "outputs/review/nested/finding.md", "content": "ok"},
        runtime_context=_runtime_context("run-1"),
    )
    traversal = _decide(
        middleware,
        agent,
        tool,
        {"file_path": "outputs/review/../other.md", "content": "escape"},
        runtime_context=_runtime_context("run-1"),
    )

    assert nested.behavior is PermissionBehavior.ALLOW
    assert nested.decision_reason == "agentgov.allow_for_run"
    assert traversal.behavior is not PermissionBehavior.ALLOW


@pytest.mark.parametrize("tool", [Read(), Write(), Edit()])
@pytest.mark.parametrize("behavior", [PermissionBehavior.DENY, PermissionBehavior.ASK])
@pytest.mark.parametrize("path_form", ["relative", "dot", "parent", "absolute", "link"])
def test_static_file_rules_match_the_resolved_target(
    tmp_path: Path,
    tool: ToolBase,
    behavior: PermissionBehavior,
    path_form: str,
) -> None:
    narrow_rule = f"{tool.name}(outputs/review/**)"
    _write_workspace(
        tmp_path,
        _manifest(
            allowed_tools=[f"{tool.name}(outputs/**)"],
            ask_tools=[narrow_rule] if behavior is PermissionBehavior.ASK else [],
            denied_tools=[narrow_rule] if behavior is PermissionBehavior.DENY else [],
        ),
    )
    (tmp_path / "outputs/review").mkdir(parents=True)
    (tmp_path / "outputs/ordinary").mkdir()
    (tmp_path / "outputs/alias").symlink_to("review", target_is_directory=True)
    paths = {
        "relative": "outputs/review/finding.md",
        "dot": "./outputs/review/finding.md",
        "parent": "outputs/ordinary/../review/finding.md",
        "absolute": str(tmp_path / "outputs/ordinary/../review/finding.md"),
        "link": "outputs/alias/finding.md",
    }
    tool_input = {"file_path": paths[path_form], "content": "review"}

    decision = _decide(AgentGovPolicyMiddleware(tmp_path), _agent(PermissionMode.DEFAULT), tool, tool_input)

    assert decision.behavior is behavior
    assert tool_input["file_path"] == paths[path_form]


@pytest.mark.parametrize("absolute_rule", [False, True])
def test_resolved_parent_path_remains_allowed_within_its_run_scope(tmp_path: Path, absolute_rule: bool) -> None:
    path_rule = str(tmp_path / "outputs/review/**") if absolute_rule else "outputs/review/**"
    _write_workspace(tmp_path, _manifest(ask_tools=[f"Write({path_rule})"]))
    (tmp_path / "outputs/review/nested").mkdir(parents=True)
    middleware = AgentGovPolicyMiddleware(tmp_path)
    tool_input = {"file_path": "outputs/review/nested/../finding.md", "content": "review"}
    agent = _agent(PermissionMode.DEFAULT)
    assert _decide(middleware, agent, Write(), tool_input).behavior is PermissionBehavior.ASK
    agent.state.permission_context.allow_rules = {
        "Write": [PermissionRule(tool_name="Write", rule_content=path_rule, behavior=PermissionBehavior.ALLOW, source="agentgov-run:run-1")],
    }

    decision = _decide(middleware, agent, Write(), tool_input, runtime_context=_runtime_context("run-1"))

    assert decision.behavior is PermissionBehavior.ALLOW
    assert decision.decision_reason == "agentgov.allow_for_run"


def test_static_allow_accepts_a_parent_path_resolving_inside_its_scope(tmp_path: Path) -> None:
    _write_workspace(tmp_path, _manifest(allowed_tools=["Write(outputs/**)"]))
    (tmp_path / "ordinary").mkdir()
    (tmp_path / "outputs").mkdir()

    decision = _decide(
        AgentGovPolicyMiddleware(tmp_path),
        _agent(PermissionMode.DEFAULT),
        Write(),
        {"file_path": "ordinary/../outputs/finding.md", "content": "review"},
    )

    assert decision.behavior is PermissionBehavior.ALLOW


@pytest.mark.parametrize("tool", [Read(), Write(), Edit()])
def test_worker_deny_matches_resolved_target_before_broader_allow(tmp_path: Path, tool: ToolBase) -> None:
    _write_workspace(tmp_path, _manifest(allowed_tools=[f"{tool.name}(outputs/**)"]))
    (tmp_path / "outputs/review").mkdir(parents=True)
    (tmp_path / "outputs/ordinary").mkdir()
    agent = _agent(PermissionMode.DEFAULT)
    agent.state.permission_context.allow_rules = {
        tool.name: [PermissionRule(tool_name=tool.name, rule_content="outputs/**", behavior=PermissionBehavior.ALLOW, source="agentgov-subagent:worker")],
    }
    agent.state.permission_context.deny_rules = {
        tool.name: [PermissionRule(tool_name=tool.name, rule_content="outputs/review/**", behavior=PermissionBehavior.DENY, source="agentgov-subagent:worker")],
    }

    decision = _decide(
        AgentGovPolicyMiddleware(tmp_path),
        agent,
        tool,
        {"file_path": "outputs/ordinary/../review/finding.md", "content": "review"},
        runtime_context=_runtime_context("run-1").model_copy(update={"role": "worker"}),
    )

    assert decision.behavior is PermissionBehavior.DENY


@pytest.mark.parametrize("raw_path", [None, "", "outputs/\0file"])
def test_invalid_file_path_is_denied_without_permission_error(tmp_path: Path, raw_path: str | None) -> None:
    _write_workspace(tmp_path, _manifest(allowed_tools=["Write(outputs/**)"]))
    decision = _decide(AgentGovPolicyMiddleware(tmp_path), _agent(PermissionMode.DEFAULT), Write(), {"file_path": raw_path})
    assert decision.behavior is PermissionBehavior.DENY


def test_run_scoped_bash_rule_is_never_honored(tmp_path: Path) -> None:
    _write_workspace(tmp_path, _manifest(ask_tools=["Bash(*a*)"]))
    run_rule = PermissionRule(
        tool_name="Bash",
        rule_content="*a*",
        behavior=PermissionBehavior.ALLOW,
        source="agentgov-run:run-1",
    )
    agent = _agent(PermissionMode.DEFAULT, allow_rules={"Bash": [run_rule]})
    middleware = AgentGovPolicyMiddleware(tmp_path)

    for command in ("date", "mkdir outputs/data"):
        decision = _decide(
            middleware,
            agent,
            Bash(),
            {"command": command},
            runtime_context=_runtime_context("run-1"),
        )
        assert decision.behavior is not PermissionBehavior.ALLOW


@pytest.mark.parametrize(
    "mode",
    [PermissionMode.DONT_ASK, PermissionMode.EXPLORE, PermissionMode.BYPASS],
)
def test_noninteractive_or_restricted_modes_do_not_turn_ask_into_write_access(
    tmp_path: Path,
    mode: PermissionMode,
) -> None:
    _write_workspace(tmp_path, _manifest(ask_tools=["Write(outputs/review/**)"]))
    decision = _decide(
        AgentGovPolicyMiddleware(tmp_path),
        _agent(mode),
        Write(),
        {"file_path": "outputs/review/finding.md", "content": "review"},
    )
    assert decision.behavior is PermissionBehavior.DENY


def test_explore_mode_can_ask_only_for_a_read_only_invocation(tmp_path: Path) -> None:
    _write_workspace(tmp_path, _manifest(ask_tools=["Read(outputs/review/**)"]))
    decision = _decide(
        AgentGovPolicyMiddleware(tmp_path),
        _agent(PermissionMode.EXPLORE),
        Read(),
        {"file_path": "outputs/review/finding.md"},
    )
    assert decision.behavior is PermissionBehavior.ASK


@pytest.mark.parametrize(
    "ask_tools, allowed_tools, denied_tools, error",
    [
        ("Write(outputs/**)", [], [], "must be a string list"),
        (["mcp__sec_ops__*(*)"], [], [], "cannot wildcard MCP tools"),
        (["Write(outputs/**)"], ["Write(outputs/**)"], [], "conflicting rules"),
        (["Write(outputs/**)"], [], ["Write(outputs/**)"], "conflicting rules"),
        (["Write(outputs/**)", "Write(outputs/**)"], [], [], "duplicate rules"),
    ],
)
def test_malformed_or_conflicting_ask_policy_fails_closed(
    tmp_path: Path,
    ask_tools: object,
    allowed_tools: object,
    denied_tools: object,
    error: str,
) -> None:
    _write_workspace(
        tmp_path,
        _manifest(
            allowed_tools=allowed_tools,
            ask_tools=ask_tools,
            denied_tools=denied_tools,
        ),
    )
    with pytest.raises(ValueError, match=error):
        AgentGovPolicyMiddleware(tmp_path)


def test_managed_policy_validates_ask_schema_and_conflicts(tmp_path: Path) -> None:
    compatible = tmp_path / "compatible"
    _write_workspace(compatible, _manifest())
    assert plan_workspace_policy(workspace=compatible, agent_id="policy-agent").violations == ()

    unsafe = tmp_path / "unsafe"
    _write_workspace(
        unsafe,
        _manifest(
            allowed_tools=["Write(outputs/**)"],
            ask_tools=["Write(outputs/**)"],
        ),
    )
    rule_ids = {item.rule_id for item in plan_workspace_policy(workspace=unsafe, agent_id="policy-agent").violations}
    assert "conflicting_tool_policy" in rule_ids

    wildcard = tmp_path / "wildcard"
    _write_workspace(wildcard, _manifest(ask_tools=["mcp__sec_ops__*(*)"]))
    rule_ids = {item.rule_id for item in plan_workspace_policy(workspace=wildcard, agent_id="policy-agent").violations}
    assert "wildcard_mcp_permission_forbidden" in rule_ids


def test_subagent_nonempty_ask_policy_is_rejected_by_runtime_and_managed_policy(tmp_path: Path) -> None:
    _write_workspace(tmp_path, _manifest())
    subagent = tmp_path / "subagents" / "reviewer"
    subagent.mkdir(parents=True)
    (subagent / "AGENT.md").write_text("# Reviewer\n", encoding="utf-8")
    (subagent / "agent.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "agent": {
                    "id": "reviewer",
                    "description": "Review findings",
                    "runtime": "agentscope",
                    "runtime_contract": "agentscope-app/2.0.8",
                    "system_prompt": "AGENT.md",
                },
                "context_config": {},
                "react_config": {},
                "invite_config": {"invitable": False},
                "session": {"permission_mode": "dont_ask"},
                "workspace_policy": {
                    "fail_closed": True,
                    "allowed_tools": ["TeamSay"],
                    "ask_tools": ["ReviewAction"],
                    "denied_tools": [],
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported"):
        load_subagent_templates(tmp_path, "a" * 64)
    rule_ids = {item.rule_id for item in plan_workspace_policy(workspace=tmp_path, agent_id="policy-agent").violations}
    assert "subagent_ask_unsupported" in rule_ids


def test_cutover_checker_reuses_permission_policy_for_top_level_and_subagent(tmp_path: Path) -> None:
    top_level = _check_permission_policy(
        tmp_path,
        tmp_path / "agent.yaml",
        {
            "allowed_tools": [],
            "ask_tools": ["mcp__sec_ops__*(*)"],
            "denied_tools": [],
        },
    )
    subagent = _check_permission_policy(
        tmp_path,
        tmp_path / "subagents/reviewer/agent.yaml",
        {
            "allowed_tools": [],
            "ask_tools": ["ReviewAction"],
            "denied_tools": [],
        },
        subagent=True,
    )
    assert [item.code for item in top_level] == ["tool_permission_policy"]
    assert [item.code for item in subagent] == ["subagent_ask_unsupported"]


def test_automatic_manifest_improvement_cannot_add_ask_permission() -> None:
    old = _manifest()
    new = _manifest(ask_tools=["Write(outputs/review/**)"])
    with pytest.raises(ExecutionContentGuardError, match="不得扩大工具权限"):
        guard_execution_write(
            target_path="agent.yaml",
            new_bytes=yaml.safe_dump(new).encode(),
            original_bytes=yaml.safe_dump(old).encode(),
        )


def test_converter_emits_explicit_reviewed_ask_rules() -> None:
    sandbox = {
        "enabled": True,
        "failIfUnavailable": True,
        "filesystem": {"allowWrite": ["outputs/**"], "denyRead": [".env"]},
        "network": {"allowedDomains": []},
    }
    converted = _workspace_policy(
        {"allow": [], "ask": ["Write(outputs/review/**)"], "deny": []},
        sandbox,
        kind="business",
        agent_id="policy-agent",
        has_subagents=False,
    )
    empty = _workspace_policy(
        {"allow": [], "deny": []},
        sandbox,
        kind="business",
        agent_id="policy-agent",
        has_subagents=False,
    )

    assert converted["ask_tools"] == ["Write(outputs/review/**)"]
    assert converted["guard"]["mode"] == "deny_ask"
    assert empty["ask_tools"] == []
    assert empty["guard"]["mode"] == "deny_only"


def test_hitl_display_requires_interactive_mode_and_nonempty_valid_ask_policy(tmp_path: Path) -> None:
    manifest = _manifest()
    _write_workspace(tmp_path, manifest)
    assert read_requires_human_confirmation(tmp_path) is False

    manifest["workspace_policy"]["ask_tools"] = ["ReviewAction"]
    _write_workspace(tmp_path, manifest)
    assert read_requires_human_confirmation(tmp_path) is True

    manifest["session"]["permission_mode"] = "dont_ask"
    _write_workspace(tmp_path, manifest)
    assert read_requires_human_confirmation(tmp_path) is False

    manifest["session"]["permission_mode"] = "default"
    manifest["workspace_policy"]["ask_tools"] = ["mcp__sec_ops__*(*)"]
    _write_workspace(tmp_path, manifest)
    assert read_requires_human_confirmation(tmp_path) is False
