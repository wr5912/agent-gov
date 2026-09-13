"""受治理候选只能按 AgentScope Harness 契约收敛式修改。"""

from __future__ import annotations

import hashlib
import json

import pytest
import yaml
from app.runtime.execution_content_guards import ExecutionContentGuardError, guard_execution_write


def _manifest(
    *,
    agent_id: str = "agent-a",
    allowed_tools: list[str] | None = None,
    denied_tools: list[str] | None = None,
    writable_paths: list[str] | None = None,
    permission_mode: str = "default",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "agent": {
            "id": agent_id,
            "runtime": "agentscope",
            "runtime_contract": "agentscope-app/2.0.8",
        },
        "session": {"permission_mode": permission_mode, "cwd": ".", "model_profile": "default"},
        "workspace_policy": {
            "fail_closed": True,
            "immutable_harness": True,
            "allow_for_run": False,
            "allowed_tools": allowed_tools or ["Read(./**)", "Grep"],
            "denied_tools": denied_tools or ["Read(./.env)", "Bash(curl *)"],
            "writable_paths": writable_paths or ["/runtime-data/outputs/agent-a"],
            "immutable_paths": ["AGENT.md", "agent.yaml", "skills/**", "mcp/**", "subagents/**"],
            "denied_read_paths": [".env", "**/.env", "**/*credential*"],
            "allowed_network_domains": ["approved.internal"],
            "sandbox": {
                "enabled": True,
                "fail_if_unavailable": True,
                "allow_unsandboxed_commands": False,
            },
        },
        "runtime_middlewares": [
            {"type": "policy_guard", "phase": "before_tool_call", "fail_closed": True},
        ],
    }


def _yaml(value: object) -> bytes:
    return yaml.safe_dump(value, allow_unicode=True, sort_keys=False).encode()


def _mcp(*, kind: str = "http_mcp") -> dict[str, object]:
    config: dict[str, object]
    if kind == "stdio_mcp":
        config = {"type": kind, "command": "python", "args": ["server.py"]}
    else:
        config = {"type": kind, "url": "${KNOWLEDGE_MCP_URL}"}
    return {
        "schema_version": 1,
        "name": "knowledge",
        "credential_refs": [{"env": "KNOWLEDGE_MCP_URL", "path": "mcp_config.url"}],
        "mcp_config": config,
    }


def test_invalid_manifest_yaml_rejected() -> None:
    with pytest.raises(ExecutionContentGuardError, match="合法 YAML"):
        guard_execution_write(target_path="agent.yaml", new_bytes=b"{bad", original_bytes=None)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(schema_version=2),
        lambda value: value["agent"].update(runtime="other"),
        lambda value: value["agent"].update(runtime_contract="agentscope-app/latest"),
        lambda value: value["workspace_policy"].update(fail_closed=False),
        lambda value: value["workspace_policy"].update(immutable_harness=False),
        lambda value: value["workspace_policy"].update(allow_for_run=True),
    ],
)
def test_fixed_runtime_and_fail_closed_contract_cannot_be_relaxed(mutation) -> None:
    old = _manifest()
    new = _manifest()
    mutation(new)
    with pytest.raises(ExecutionContentGuardError):
        guard_execution_write(target_path="agent.yaml", new_bytes=_yaml(new), original_bytes=_yaml(old))


def test_agent_identity_cannot_change() -> None:
    with pytest.raises(ExecutionContentGuardError, match="不可变身份"):
        guard_execution_write(
            target_path="agent.yaml",
            new_bytes=_yaml(_manifest(agent_id="agent-b")),
            original_bytes=_yaml(_manifest(agent_id="agent-a")),
        )


def test_permission_and_write_scope_may_only_narrow() -> None:
    old = _manifest()
    unsafe = (
        _manifest(allowed_tools=["Read(./**)", "Grep", "Bash(*)"]),
        _manifest(denied_tools=["Read(./.env)"]),
        _manifest(writable_paths=["/runtime-data/outputs/agent-a", "/tmp"]),
    )
    for new in unsafe:
        with pytest.raises(ExecutionContentGuardError):
            guard_execution_write(target_path="agent.yaml", new_bytes=_yaml(new), original_bytes=_yaml(old))

    narrowed = _manifest(
        allowed_tools=["Read(./**)"],
        denied_tools=["Read(./.env)", "Bash(curl *)", "Bash(wget *)"],
        writable_paths=[],
    )
    guard_execution_write(target_path="agent.yaml", new_bytes=_yaml(narrowed), original_bytes=_yaml(old))


def test_ask_rules_are_frozen_because_removal_can_expose_broader_allow() -> None:
    old = _manifest(allowed_tools=["Write(outputs/**)"])
    old["workspace_policy"]["ask_tools"] = ["Write(outputs/review/**)"]
    new = _manifest(allowed_tools=["Write(outputs/**)"])

    with pytest.raises(ExecutionContentGuardError, match="不得扩大工具权限"):
        guard_execution_write(
            target_path="agent.yaml",
            new_bytes=_yaml(new),
            original_bytes=_yaml(old),
        )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value["session"].update(permission_mode="accept_edits"), "permission_mode"),
        (lambda value: value["workspace_policy"].update(denied_read_paths=[".env"]), "拒绝读取"),
        (lambda value: value["workspace_policy"].update(immutable_paths=["AGENT.md"]), "不可变路径"),
        (lambda value: value["session"].update(cwd="/"), "session.cwd"),
        (lambda value: value["session"].update(model_profile="unmanaged"), "model_profile"),
        (
            lambda value: value["workspace_policy"].update(
                allowed_network_domains=["approved.internal", "new.internal"],
            ),
            "网络访问",
        ),
        (lambda value: value["workspace_policy"]["sandbox"].update(enabled=False), "sandbox"),
        (lambda value: value["workspace_policy"]["sandbox"].update(network=True), "sandbox"),
        (
            lambda value: value.update(
                runtime_middlewares=[
                    {"type": "policy_guard", "phase": "before_tool_call", "fail_closed": True},
                    {"type": "tool_audit", "phase": "after_tool_call"},
                ],
            ),
            "middleware",
        ),
    ],
)
def test_runtime_execution_policy_cannot_expand(mutate, message: str) -> None:
    old = _manifest(permission_mode="explore")
    new = _manifest(permission_mode="explore")
    mutate(new)
    with pytest.raises(ExecutionContentGuardError, match=message):
        guard_execution_write(target_path="agent.yaml", new_bytes=_yaml(new), original_bytes=_yaml(old))


def test_runtime_execution_policy_may_only_tighten() -> None:
    old = _manifest(permission_mode="default")
    new = _manifest(permission_mode="explore")
    new["workspace_policy"]["denied_read_paths"].append("private/**")
    new["workspace_policy"]["allowed_network_domains"] = []

    guard_execution_write(target_path="agent.yaml", new_bytes=_yaml(new), original_bytes=_yaml(old))


def test_incomparable_permission_modes_cannot_be_exchanged() -> None:
    for old_mode, new_mode in (("dont_ask", "explore"), ("explore", "dont_ask")):
        with pytest.raises(ExecutionContentGuardError, match="permission_mode"):
            guard_execution_write(
                target_path="agent.yaml",
                new_bytes=_yaml(_manifest(permission_mode=new_mode)),
                original_bytes=_yaml(_manifest(permission_mode=old_mode)),
            )


def test_invalid_or_unreferenced_mcp_config_rejected() -> None:
    with pytest.raises(ExecutionContentGuardError, match="合法 JSON"):
        guard_execution_write(target_path="mcp/knowledge.json", new_bytes=b"{bad", original_bytes=None)
    value = _mcp()
    value.pop("credential_refs")
    with pytest.raises(ExecutionContentGuardError, match="credential_refs"):
        guard_execution_write(target_path="mcp/knowledge.json", new_bytes=json.dumps(value).encode(), original_bytes=None)


def test_http_mcp_with_credential_reference_is_allowed() -> None:
    guard_execution_write(target_path="mcp/knowledge.json", new_bytes=json.dumps(_mcp()).encode(), original_bytes=None)


def test_stdio_mcp_cannot_be_created_or_changed_automatically() -> None:
    value = _mcp(kind="stdio_mcp")
    raw = json.dumps(value).encode()
    with pytest.raises(ExecutionContentGuardError, match="stdio MCP"):
        guard_execution_write(target_path="mcp/local.json", new_bytes=raw, original_bytes=None)
    guard_execution_write(target_path="mcp/local.json", new_bytes=raw, original_bytes=raw)


def test_markdown_harness_content_is_not_parsed_as_structured_config() -> None:
    for path in ("AGENT.md", "skills/triage/SKILL.md", "subagents/reviewer/AGENT.md"):
        guard_execution_write(target_path=path, new_bytes=b"# prompt with Bash(*) text", original_bytes=b"old")


def test_applier_rejects_manifest_escalation_before_writing(tmp_path) -> None:
    from app.runtime.execution_targets import WorkspaceExecutionTargetPolicy
    from app.services.workspace_execution_applier import WorkspaceExecutionApplier

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = _yaml(_manifest())
    target = workspace / "agent.yaml"
    target.write_bytes(original)
    unsafe = _yaml(_manifest(allowed_tools=["Read(./**)", "Grep", "Bash(*)"]))
    operation = {
        "operation": "replace_file",
        "path": "agent.yaml",
        "expected_sha256": hashlib.sha256(original).hexdigest(),
        "content": unsafe.decode(),
    }
    with pytest.raises(ExecutionContentGuardError):
        WorkspaceExecutionApplier().apply_execution_operations(
            [operation],
            workspace_dir=workspace,
            target_policy=WorkspaceExecutionTargetPolicy(workspace),
            content_guard=guard_execution_write,
        )
    assert target.read_bytes() == original


def test_applier_allowlist_rejects_off_contract_targets(tmp_path) -> None:
    from app.runtime.execution_targets import WorkspaceExecutionTargetPolicy
    from app.services.workspace_execution_applier import WorkspaceExecutionApplier, WorkspaceExecutionApplyError

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    applier = WorkspaceExecutionApplier()
    allowed = {"AGENT.md", "agent.yaml", "skills/triage/SKILL.md", "mcp/knowledge.json"}
    for path in (".env", "hooks/pre.py", "runtime-state.json", "subagents/unreviewed/AGENT.md"):
        with pytest.raises(WorkspaceExecutionApplyError):
            applier.apply_execution_operations(
                [{"operation": "create_file", "path": path, "content": "x"}],
                workspace_dir=workspace,
                target_policy=WorkspaceExecutionTargetPolicy(workspace),
                allowed_targets=allowed,
            )
        assert not (workspace / path).exists()


def test_applier_allows_explicit_agent_prompt_target(tmp_path) -> None:
    from app.runtime.execution_targets import WorkspaceExecutionTargetPolicy
    from app.services.workspace_execution_applier import WorkspaceExecutionApplier

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    WorkspaceExecutionApplier().apply_execution_operations(
        [{"operation": "create_file", "path": "AGENT.md", "content": "# 系统 prompt"}],
        workspace_dir=workspace,
        target_policy=WorkspaceExecutionTargetPolicy(workspace),
        allowed_targets={"AGENT.md"},
    )
    assert (workspace / "AGENT.md").read_text(encoding="utf-8") == "# 系统 prompt"


def test_applier_rejects_truncated_skill_replacement(tmp_path) -> None:
    from app.runtime.execution_targets import WorkspaceExecutionTargetPolicy
    from app.services.workspace_execution_applier import WorkspaceExecutionApplier, WorkspaceExecutionApplyError

    workspace = tmp_path / "workspace"
    skill = workspace / "skills" / "triage" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    original = "\n".join(["---", "name: triage", "---", *[f"## Step {index}" for index in range(1, 12)]]) + "\n"
    skill.write_text(original, encoding="utf-8")
    operation = {
        "operation": "replace_file",
        "path": "skills/triage/SKILL.md",
        "expected_sha256": hashlib.sha256(original.encode()).hexdigest(),
        "content": "---\nname: triage\n---\n",
    }
    with pytest.raises(WorkspaceExecutionApplyError, match="discard too much existing Markdown"):
        WorkspaceExecutionApplier().apply_execution_operations(
            [operation],
            workspace_dir=workspace,
            target_policy=WorkspaceExecutionTargetPolicy(workspace),
            allowed_targets={"skills/triage/SKILL.md"},
        )
    assert skill.read_text(encoding="utf-8") == original
