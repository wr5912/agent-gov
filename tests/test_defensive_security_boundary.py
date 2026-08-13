from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECK = REPO_ROOT / "scripts/check_defensive_security_boundary.py"
ACCEPTANCE_GUARD = REPO_ROOT / ".codex/hooks/container_acceptance_guard.py"
GOVERNOR_ALLOWED_RULES = [
    "Skill",
    "Read(/**/business-agents/*/workspace/CLAUDE.md)",
    "Read(/**/business-agents/*/workspace/.claude/skills/*/SKILL.md)",
    "Read(/**/business-agents/*/workspace/.claude/agents/*.md)",
    "Read(/**/business-agents/*/workspace/.claude/rules/*.md)",
    "Read(/**/business-agents/*/workspace/.claude/commands/*.md)",
]
GOVERNOR_DENIES = [
    "Write",
    "Edit",
    "NotebookEdit",
    "Bash",
    "WebFetch",
    "WebSearch",
    "mcp__*",
    "Read(/**/.env)",
    "Read(/**/.env.*)",
    "Read(/**/secrets/**)",
    "Read(/**/.mcp.json)",
    "Read(/**/.mcp.*.json)",
    "Read(/**/.claude/settings.json)",
    "Read(/**/.claude/settings.*.json)",
    "Read(/**/claude-root/**)",
    "Read(/**/claude-roots/**)",
    "Read(/**/.git/**)",
]
AGENT_DENIES = [
    "Read",
    "Glob",
    "Grep",
    "Bash",
    "WebFetch",
    "WebSearch",
    "mcp__*",
    "Read(./.env)",
    "Read(./.env.*)",
    "Read(./secrets/**)",
    "Read(./.mcp.json)",
    "Read(./.mcp.*.json)",
    "Read(./.claude/settings.json)",
    "Read(./.claude/settings.*.json)",
]


def _module():
    spec = importlib.util.spec_from_file_location("check_defensive_security_boundary", CHECK)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _acceptance_guard_module():
    spec = importlib.util.spec_from_file_location("container_acceptance_guard", ACCEPTANCE_GUARD)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_governor(root: Path) -> None:
    workspace = root / "docker/runtime-bootstrap/governor-workspace"
    settings = {
        "permissions": {
            "defaultMode": "dontAsk",
            "disableBypassPermissionsMode": "disable",
            "allow": GOVERNOR_ALLOWED_RULES,
            "ask": [],
            "deny": GOVERNOR_DENIES,
        },
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "autoAllowBashIfSandboxed": False,
            "allowUnsandboxedCommands": False,
            "filesystem": {"allowWrite": []},
            "network": {"allowedDomains": [], "deniedDomains": ["*"]},
        },
    }
    (workspace / ".claude/skills/read-business-agent-config").mkdir(parents=True)
    (workspace / ".claude/settings.json").write_text(json.dumps(settings), encoding="utf-8")
    (workspace / ".mcp.json").write_text('{"mcpServers": {}}\n', encoding="utf-8")
    (workspace / "CLAUDE.md").write_text(
        "只消费后端提供的脱敏 typed context。\n不使用 WebFetch、WebSearch、Bash、MCP。\n",
        encoding="utf-8",
    )
    (workspace / ".claude/skills/read-business-agent-config/SKILL.md").write_text(
        "---\nallowed-tools:\n  - Read\n---\n不得用 Read、Glob 或 Grep 在文件系统中寻找同名文件。\n",
        encoding="utf-8",
    )


def _write_workspace(root: Path, *, allow: list[str], deny: list[str], mcp: dict[str, object]) -> None:
    _write_governor(root)
    workspace = root / "docker/runtime-bootstrap/business-agents/defensive-agent/workspace"
    settings = {
        "permissions": {
            "defaultMode": "dontAsk",
            "disableBypassPermissionsMode": "disable",
            "allow": allow,
            "ask": [],
            "deny": deny,
        },
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Edit|Write|NotebookEdit",
                    "hooks": [
                        {
                            "type": "command",
                            "command": 'python "$CLAUDE_PROJECT_DIR/hooks/pre_tool_guard.py"',
                            "timeout": 10,
                        }
                    ],
                }
            ],
            "SessionStart": [
                {
                    "matcher": "startup|resume",
                    "hooks": [
                        {
                            "type": "command",
                            "command": 'python "$CLAUDE_PROJECT_DIR/hooks/session_start.py"',
                            "timeout": 5,
                        }
                    ],
                }
            ],
        },
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "autoAllowBashIfSandboxed": False,
            "allowUnsandboxedCommands": False,
            "filesystem": {
                "allowWrite": ["/data/outputs/defensive-agent"],
                "denyRead": ["./**", "/data/uploads/**", "/data/business-agents/defensive-agent/claude-root/**"],
            },
            "network": {"allowedDomains": [], "deniedDomains": ["*"]},
        },
    }
    (workspace / ".claude").mkdir(parents=True)
    (workspace / ".claude/settings.json").write_text(json.dumps(settings), encoding="utf-8")
    (workspace / ".mcp.json").write_text(json.dumps({"mcpServers": mcp}), encoding="utf-8")
    hooks_dir = workspace / "hooks"
    hooks_dir.mkdir()
    source_root = REPO_ROOT / "docker/runtime-bootstrap/business-agents/security-operations-expert/workspace/hooks"
    for hook_name in ("pre_tool_guard.py", "session_start.py"):
        (hooks_dir / hook_name).write_bytes((source_root / hook_name).read_bytes())
    (workspace / "CLAUDE.md").write_text(
        "仅限已授权环境的防御性安全运营。\n本 Agent 在整个在线流程中始终是只读候选提供者。\n",
        encoding="utf-8",
    )
    (workspace / "agent.yaml").write_text(
        "agent:\n"
        "  id: defensive-agent\n"
        "approval_policy:\n"
        "  agent_soc_side_effects: deny\n"
        "  proposal_side_effects: deny\n"
        "  lifecycle_worker_owns_system_changes: true\n"
        "  agent_monitors_execution: false\n"
        "  agent_closes_response_case: false\n"
        "mcp:\n"
        "  status: disabled_pending_exact_capability_contract\n"
        "  servers: {}\n",
        encoding="utf-8",
    )


def _copy_security_operations_workspace(root: Path) -> Path:
    _write_governor(root)
    source = REPO_ROOT / "docker/runtime-bootstrap/business-agents/security-operations-expert/workspace"
    workspace = root / "docker/runtime-bootstrap/business-agents/security-operations-expert/workspace"
    workspace.parent.mkdir(parents=True)
    shutil.copytree(source, workspace, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    return workspace


def _mutate_security_allow(workspace: Path, mutation: Callable[[list[str]], None]) -> None:
    path = workspace / ".claude/settings.json"
    settings = json.loads(path.read_text(encoding="utf-8"))
    mutation(settings["permissions"]["allow"])
    path.write_text(json.dumps(settings), encoding="utf-8")


def test_current_built_in_agents_keep_defensive_boundary() -> None:
    module = _module()

    issues, count = module.collect_issues(REPO_ROOT)

    assert issues == []
    assert count >= 1


@pytest.mark.parametrize(
    "command",
    (
        "python scripts/run_container_acceptance.py --profile core -- verify",
        ".venv/bin/python -m scripts.run_container_acceptance --profile core -- verify",
        "python -I -m scripts.run_agent_test_container_e2e",
        "uv run -m scripts.verify_speech_summary_container",
    ),
)
def test_acceptance_guard_blocks_direct_runner_and_private_python_modules(command: str) -> None:
    guard = _acceptance_guard_module()

    assert guard.bypass_reason(command) is not None


@pytest.mark.parametrize(
    "command",
    (
        "make container-core-smoke",
        "make container-workspace-pytest-test",
        "python -c 'print(1)'",
        "python -c 'scripts/run_container_acceptance.py'",
        "python -m pytest tests/test_runtime.py",
        "uv run -m pytest tests/test_runtime.py",
    ),
)
def test_acceptance_guard_keeps_public_make_and_host_python_available(command: str) -> None:
    guard = _acceptance_guard_module()

    assert guard.bypass_reason(command) is None


def test_security_operations_allowlist_rejects_unknown_and_unapproved_tasks(tmp_path: Path) -> None:
    module = _module()
    workspace = _copy_security_operations_workspace(tmp_path)
    _mutate_security_allow(
        workspace,
        lambda allow: allow.extend(["Task(response-playbook-summarizer)", "Task(unknown-reviewer)"]),
    )

    issues, _ = module.collect_issues(tmp_path)

    messages = [issue.message for issue in issues]
    assert messages.count("Task permission references an unapproved subagent") == 2
    assert "security-operations-expert permissions.allow must match the reviewed exact set" in messages


def test_security_operations_allowlist_rejects_missing_or_unscoped_task_permission(tmp_path: Path) -> None:
    module = _module()
    workspace = _copy_security_operations_workspace(tmp_path)

    def replace_reviewed_task(allow: list[str]) -> None:
        allow.remove("Task(response-playbook-builder)")
        allow.append("Task")

    _mutate_security_allow(workspace, replace_reviewed_task)

    issues, _ = module.collect_issues(tmp_path)

    messages = [issue.message for issue in issues]
    assert "Task permission must name one reviewed subagent exactly" in messages
    assert "reviewed Task permission is missing" in messages
    assert "security-operations-expert permissions.allow must match the reviewed exact set" in messages


def test_security_operations_task_permission_requires_a_real_reviewed_asset(tmp_path: Path) -> None:
    module = _module()
    workspace = _copy_security_operations_workspace(tmp_path)
    (workspace / ".claude/agents/response-playbook-builder.md").unlink()

    issues, _ = module.collect_issues(tmp_path)

    assert "reviewed Task agent must be a real tracked asset" in [issue.message for issue in issues]


def test_security_operations_task_permission_requires_tools_empty(tmp_path: Path) -> None:
    module = _module()
    workspace = _copy_security_operations_workspace(tmp_path)
    path = workspace / ".claude/agents/response-playbook-planning.md"
    text = path.read_text(encoding="utf-8").replace("tools: []", "tools:\n  - Skill")
    path.write_text(text, encoding="utf-8")

    issues, _ = module.collect_issues(tmp_path)

    assert "reviewed Task agent must declare tools: []" in [issue.message for issue in issues]


def test_boundary_rejects_shell_permission_and_local_mcp_command(tmp_path: Path) -> None:
    module = _module()
    _write_workspace(
        tmp_path,
        allow=["Bash(tool *)"],
        deny=[],
        mcp={"facts": {"command": "local-tool", "args": []}},
    )

    issues, count = module.collect_issues(tmp_path)

    assert count == 1
    messages = [issue.message for issue in issues]
    assert "built-in business Agent must not allow Bash" in messages
    assert "built-in business Agent must deny Bash" in messages
    assert any("must not execute a local command" in message for message in messages)


def test_boundary_accepts_agent_with_mcp_and_raw_reads_disabled(tmp_path: Path) -> None:
    module = _module()
    _write_workspace(
        tmp_path,
        allow=[
            "Write(/data/outputs/defensive-agent/**)",
        ],
        deny=AGENT_DENIES,
        mcp={},
    )

    issues, count = module.collect_issues(tmp_path)

    assert count == 1
    assert issues == []


def test_boundary_rejects_missing_workspace_and_non_read_only_asset_tool(tmp_path: Path) -> None:
    module = _module()
    missing = tmp_path / "docker/runtime-bootstrap/business-agents/missing-agent"
    missing.mkdir(parents=True)
    _write_workspace(
        tmp_path,
        allow=["Skill"],
        deny=AGENT_DENIES,
        mcp={},
    )
    skill = tmp_path / "docker/runtime-bootstrap/business-agents/defensive-agent/workspace/.claude/skills/review/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: review\nallowed-tools:\n  - Write\n---\n", encoding="utf-8")

    issues, count = module.collect_issues(tmp_path)

    assert count == 2
    messages = [issue.message for issue in issues]
    assert "built-in Agent must contain a real workspace directory" in messages
    assert "asset tool must be read-only or a platform planning primitive" in messages


def test_boundary_rejects_governor_network_or_sensitive_read(tmp_path: Path) -> None:
    module = _module()
    _write_workspace(
        tmp_path,
        allow=["Skill"],
        deny=AGENT_DENIES,
        mcp={},
    )
    path = tmp_path / "docker/runtime-bootstrap/governor-workspace/.claude/settings.json"
    settings = json.loads(path.read_text(encoding="utf-8"))
    settings["permissions"]["allow"].extend(["WebFetch", "Read(/**/.env)"])
    settings["permissions"]["ask"].append("mcp__future__*")
    settings["permissions"]["deny"].remove("WebFetch")
    settings["permissions"]["deny"].remove("mcp__*")
    path.write_text(json.dumps(settings), encoding="utf-8")
    mcp_path = tmp_path / "docker/runtime-bootstrap/governor-workspace/.mcp.json"
    mcp_path.write_text('{"mcpServers":{"future":{}}}', encoding="utf-8")

    issues, _ = module.collect_issues(tmp_path)

    messages = [issue.message for issue in issues]
    assert "Governor allowlist must contain only non-sensitive instruction assets" in messages
    assert "Governor ask rules must remain empty" in messages
    assert "Governor must deny writes, network, credentials, and runtime metadata" in messages
    assert "Governor MCP discovery must remain empty" in messages


def test_boundary_rejects_broad_reads_and_mcp_wildcard(tmp_path: Path) -> None:
    module = _module()
    _write_workspace(
        tmp_path,
        allow=["Read(./**)", "mcp__facts__records__get_*"],
        deny=AGENT_DENIES,
        mcp={},
    )

    issues, _ = module.collect_issues(tmp_path)

    messages = [issue.message for issue in issues]
    assert "built-in business Agent must not allow Read" in messages
    assert "MCP permission must remain disabled until an exact capability contract is active" in messages


def test_boundary_rejects_unapproved_hook_entrypoint_or_process_client(tmp_path: Path) -> None:
    module = _module()
    _write_workspace(tmp_path, allow=["Skill"], deny=AGENT_DENIES, mcp={})
    workspace = tmp_path / "docker/runtime-bootstrap/business-agents/defensive-agent/workspace"
    settings_path = workspace / ".claude/settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["hooks"]["PostToolUse"] = []
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    (workspace / "hooks/pre_tool_guard.py").write_text("import subprocess\n", encoding="utf-8")

    issues, _ = module.collect_issues(tmp_path)

    messages = [issue.message for issue in issues]
    assert "hooks must contain only approved local governance entrypoints" in messages
    assert "approved hook source digest changed and requires boundary review" in messages
    assert "hook source must not import process or network clients" in messages
