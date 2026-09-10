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
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "agent": {
            "id": agent_id,
            "runtime": "agentscope",
            "runtime_contract": "agentscope-app/2.0.8",
        },
        "workspace_policy": {
            "fail_closed": True,
            "immutable_harness": True,
            "allow_for_run": False,
            "allowed_tools": allowed_tools or ["Read(./**)", "Grep"],
            "denied_tools": denied_tools or ["Read(./.env)", "Bash(curl *)"],
            "writable_paths": writable_paths or ["/runtime-data/outputs/agent-a"],
        },
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
