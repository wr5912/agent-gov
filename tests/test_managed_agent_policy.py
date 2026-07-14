from __future__ import annotations

import json
import shutil
from pathlib import Path

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
