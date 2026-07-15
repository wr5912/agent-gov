from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from app.runtime.managed_agent_policy import (
    SECURITY_OPERATIONS_EXPERT_AGENT_ID,
    plan_workspace_policy,
    runtime_workspace_policy_violations,
    validate_managed_mcp_content,
)
from scripts.bootstrap_runtime_volume import bootstrap_runtime_volume
from scripts.runtime_template_renderer import build_render_context

REPO_ROOT = Path(__file__).resolve().parents[1]
SEEDS = REPO_ROOT / "docker" / "runtime-volume-seeds"
SEEDED_AGENT_IDS = tuple(sorted(path.name for path in (SEEDS / "data" / "business-agents").iterdir() if (path / "workspace").is_dir()))


def _rendered_runtime(tmp_path: Path) -> Path:
    runtime_root = tmp_path / "runtime"
    result = bootstrap_runtime_volume(
        runtime_root=runtime_root,
        template_dir=SEEDS,
        runtime_volume_mode="local-debug",
        env={},
    )
    assert result["validation_errors"] == []
    return runtime_root


@pytest.mark.parametrize("agent_id", SEEDED_AGENT_IDS)
def test_seeded_business_agent_workspaces_match_managed_policy(tmp_path, agent_id):
    runtime_root = _rendered_runtime(tmp_path)
    workspace = runtime_root / "data" / "business-agents" / agent_id / "workspace"

    assert (
        runtime_workspace_policy_violations(
            workspace=workspace,
            agent_id=agent_id,
            runtime_mode="local-debug",
            env={},
            runtime_root=runtime_root,
        )
        == ()
    )


def test_security_operations_workspace_matches_specialized_managed_policy(tmp_path):
    runtime_root = _rendered_runtime(tmp_path)
    workspace = runtime_root / "data" / "business-agents" / SECURITY_OPERATIONS_EXPERT_AGENT_ID / "workspace"

    assert (
        runtime_workspace_policy_violations(
            workspace=workspace,
            agent_id=SECURITY_OPERATIONS_EXPERT_AGENT_ID,
            runtime_mode="local-debug",
            env={},
            runtime_root=runtime_root,
        )
        == ()
    )

    settings_path = workspace / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["permissions"]["ask"].remove("mcp__sec-ops__soc_api__create")
    settings_path.write_text(json.dumps(settings), encoding="utf-8")

    violations = runtime_workspace_policy_violations(
        workspace=workspace,
        agent_id=SECURITY_OPERATIONS_EXPERT_AGENT_ID,
        runtime_mode="local-debug",
        env={},
        runtime_root=runtime_root,
    )
    assert {item.rule_id for item in violations} == {"managed_policy_drift"}


def test_security_managed_markers_preserve_operator_owned_text(tmp_path):
    template = SEEDS / "data" / "business-agents" / SECURITY_OPERATIONS_EXPERT_AGENT_ID / "workspace"
    workspace = tmp_path / "workspace"
    shutil.copytree(template, workspace)
    claude_path = workspace / "CLAUDE.md"
    current = claude_path.read_text(encoding="utf-8")
    current = current.replace("# 网络安全运营专家智能体", "# 网络安全运营专家智能体\n\n操作员自定义说明。")
    current = current.replace(
        "严禁调用任何 `create*`、`manual`、`execute`、`update*`、`delete*`、`upload*`、`cancel*` 或 `rollback` 工具。",
        "允许调用写工具。",
    )
    claude_path.write_text(current, encoding="utf-8")
    context = build_render_context(mode="local-debug", env={}, runtime_root=tmp_path)

    plan = plan_workspace_policy(
        workspace=workspace,
        agent_id=SECURITY_OPERATIONS_EXPERT_AGENT_ID,
        template_workspace=template,
        render_context=context,
    )
    change = next(item for item in plan.changes if item.path == claude_path)

    assert "操作员自定义说明" in change.content
    assert "严禁调用任何 `create*`、`manual`、`execute`" in change.content


def test_pre_tool_guard_is_managed_from_agent_template(tmp_path):
    template = SEEDS / "data" / "business-agents" / "main-agent" / "workspace"
    workspace = tmp_path / "workspace"
    shutil.copytree(template, workspace)
    hook_path = workspace / "hooks" / "pre_tool_guard.py"
    hook_path.write_text("# stale unsafe hook\n", encoding="utf-8")
    context = build_render_context(mode="local-debug", env={}, runtime_root=tmp_path)

    plan = plan_workspace_policy(
        workspace=workspace,
        agent_id="main-agent",
        template_workspace=template,
        render_context=context,
    )
    change = next(item for item in plan.changes if item.path == hook_path)

    assert change.rule_id == "managed_pre_tool_guard"
    assert change.content == (template / "hooks" / "pre_tool_guard.py").read_text(encoding="utf-8")


def test_missing_pre_tool_guard_is_planned_as_managed_creation(tmp_path):
    template = SEEDS / "data" / "business-agents" / "main-agent" / "workspace"
    workspace = tmp_path / "workspace"
    shutil.copytree(template, workspace)
    hook_path = workspace / "hooks" / "pre_tool_guard.py"
    hook_path.unlink()

    plan = plan_workspace_policy(
        workspace=workspace,
        agent_id="main-agent",
        template_workspace=template,
        render_context=build_render_context(mode="local-debug", env={}, runtime_root=tmp_path),
    )
    change = next(item for item in plan.changes if item.path == hook_path)

    assert change.before_sha256 is None
    assert change.rule_id == "managed_pre_tool_guard"


def test_symlinked_hook_parent_is_rejected_without_reading_external_content(tmp_path):
    template = SEEDS / "data" / "business-agents" / "main-agent" / "workspace"
    workspace = tmp_path / "workspace"
    shutil.copytree(template, workspace)
    shutil.rmtree(workspace / "hooks")
    outside = tmp_path / "outside"
    outside.mkdir()
    external_hook = outside / "pre_tool_guard.py"
    external_hook.write_text((template / "hooks" / "pre_tool_guard.py").read_text(encoding="utf-8"), encoding="utf-8")
    (workspace / "hooks").symlink_to(outside, target_is_directory=True)

    plan = plan_workspace_policy(
        workspace=workspace,
        agent_id="main-agent",
        template_workspace=template,
        render_context=build_render_context(mode="local-debug", env={}, runtime_root=tmp_path),
    )

    assert any(item.path.endswith("hooks/pre_tool_guard.py") and item.rule_id == "unsafe_file_type" for item in plan.violations)
    assert all(item.path != workspace / "hooks" / "pre_tool_guard.py" for item in plan.changes)
    assert external_hook.read_text(encoding="utf-8") == (template / "hooks" / "pre_tool_guard.py").read_text(encoding="utf-8")


def test_mcp_policy_rejects_stdio_and_embedded_credentials(tmp_path):
    content = json.dumps(
        {
            "mcpServers": {
                "local-command": {"type": "stdio", "command": "python", "args": ["server.py"]},
                "credential-url": {"type": "http", "url": "https://user:secret@example.test/mcp"},
            }
        }
    )

    violations = validate_managed_mcp_content(
        content,
        agent_id="custom-agent",
        runtime_mode="local-debug",
        env={},
        runtime_root=tmp_path,
    )

    assert {item.rule_id for item in violations} == {"stdio_mcp_forbidden", "invalid_mcp_url"}
