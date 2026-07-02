"""受治理 apply 写入结构化配置文件的安全护栏：

Phase 2 让优化能改的目标从仅 CLAUDE.md 拓宽到 .claude/settings.json 和 .mcp.json 后，写入必须过：
1. JSON 合法性——settings.json / .mcp.json 写入结果必须能解析，避免损坏业务 Agent 配置；
2. 权限升级防护——settings.json 的 permissions 不得新增危险 allow（Bash(*) 等无约束高危工具授权），
   也不得删除既有 deny（deny 单调，只能加不能减）。
护栏在 governed apply 内生效（worktree→change set），违规即抛错→abandon change set→回退启发式。
"""

from __future__ import annotations

import json

import pytest

from app.runtime.execution_content_guards import ExecutionContentGuardError, guard_execution_write


def _b(obj) -> bytes:
    return json.dumps(obj).encode("utf-8")


# ---- JSON 合法性 ----

def test_invalid_mcp_json_rejected():
    with pytest.raises(ExecutionContentGuardError):
        guard_execution_write(target_path=".mcp.json", new_bytes=b"{not json", original_bytes=None)


def test_valid_mcp_json_ok():
    guard_execution_write(target_path=".mcp.json", new_bytes=_b({"mcpServers": {"kb": {"type": "http"}}}), original_bytes=None)


def test_invalid_settings_json_rejected():
    with pytest.raises(ExecutionContentGuardError):
        guard_execution_write(target_path=".claude/settings.json", new_bytes=b"{oops", original_bytes=None)


# ---- 权限升级防护 ----

_BASE = {"permissions": {"allow": ["Read(./docs/**)"], "ask": ["Bash(git status)"], "deny": ["Read(/**/.env)", "Write(/etc/**)"]}}


def test_adding_dangerous_allow_rejected():
    new = {"permissions": {"allow": ["Read(./docs/**)", "Bash(*)"], "ask": [], "deny": ["Read(/**/.env)", "Write(/etc/**)"]}}
    with pytest.raises(ExecutionContentGuardError):
        guard_execution_write(target_path=".claude/settings.json", new_bytes=_b(new), original_bytes=_b(_BASE))


def test_adding_unrestricted_write_allow_rejected():
    new = {"permissions": {"allow": ["Write(/**)"], "deny": ["Read(/**/.env)", "Write(/etc/**)"]}}
    with pytest.raises(ExecutionContentGuardError):
        guard_execution_write(target_path=".claude/settings.json", new_bytes=_b(new), original_bytes=_b(_BASE))


def test_removing_deny_rejected():
    new = {"permissions": {"allow": ["Read(./docs/**)"], "deny": ["Read(/**/.env)"]}}  # 删了 Write(/etc/**)
    with pytest.raises(ExecutionContentGuardError):
        guard_execution_write(target_path=".claude/settings.json", new_bytes=_b(new), original_bytes=_b(_BASE))


def test_wildcard_mcp_allow_rejected():
    new = {"permissions": {"allow": ["mcp__*__*"], "deny": ["Read(/**/.env)", "Write(/etc/**)"]}}
    with pytest.raises(ExecutionContentGuardError):
        guard_execution_write(target_path=".claude/settings.json", new_bytes=_b(new), original_bytes=_b(_BASE))


def test_benign_settings_edit_ok():
    # 新增受限 allow + 保留全部 deny + 加一条 deny：允许
    new = {"permissions": {"allow": ["Read(./docs/**)", "Grep"], "deny": ["Read(/**/.env)", "Write(/etc/**)", "Bash(rm *)"]}}
    guard_execution_write(target_path=".claude/settings.json", new_bytes=_b(new), original_bytes=_b(_BASE))


def test_new_settings_from_scratch_dangerous_rejected():
    new = {"permissions": {"allow": ["Bash(*)"]}}
    with pytest.raises(ExecutionContentGuardError):
        guard_execution_write(target_path=".claude/settings.json", new_bytes=_b(new), original_bytes=None)


def test_new_settings_from_scratch_safe_ok():
    new = {"permissions": {"allow": ["Read(./**)"], "deny": ["Read(/**/.env)"]}}
    guard_execution_write(target_path=".claude/settings.json", new_bytes=_b(new), original_bytes=None)


# ---- 非结构化文件不拦 ----

def test_claude_md_not_guarded():
    guard_execution_write(target_path="CLAUDE.md", new_bytes="任意 prompt 文本，可含 Bash(*) 字样也不拦".encode(), original_bytes=b"old")


def test_skill_md_not_guarded():
    guard_execution_write(target_path=".claude/skills/alert-triage/SKILL.md", new_bytes=b"---\nname: x\n---\n body", original_bytes=None)


# ---- 与受治理 applier 集成：护栏在落盘前拦截 ----

def _write_settings(ws, perms):
    import hashlib

    settings = ws / ".claude" / "settings.json"
    settings.write_text(json.dumps({"permissions": perms}), encoding="utf-8")
    return settings, hashlib.sha256(settings.read_bytes()).hexdigest()


def test_applier_rejects_settings_escalation_without_writing(tmp_path):
    from app.runtime.execution_targets import WorkspaceExecutionTargetPolicy
    from app.services.workspace_execution_applier import WorkspaceExecutionApplier

    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    settings, sha = _write_settings(ws, {"allow": ["Read(./**)"], "deny": ["Read(/**/.env)"]})
    ops = [
        {
            "operation": "replace_file",
            "path": ".claude/settings.json",
            "expected_sha256": sha,
            "content": json.dumps({"permissions": {"allow": ["Read(./**)", "Bash(*)"], "deny": ["Read(/**/.env)"]}}),
        }
    ]
    with pytest.raises(ExecutionContentGuardError):
        WorkspaceExecutionApplier().apply_execution_operations(
            ops, workspace_dir=ws, target_policy=WorkspaceExecutionTargetPolicy(ws), content_guard=guard_execution_write
        )
    assert "Bash(*)" not in settings.read_text(encoding="utf-8")  # 护栏在写前拦截，未落盘


def test_applier_allows_benign_settings_edit(tmp_path):
    from app.runtime.execution_targets import WorkspaceExecutionTargetPolicy
    from app.services.workspace_execution_applier import WorkspaceExecutionApplier

    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    settings, sha = _write_settings(ws, {"allow": ["Read(./**)"], "deny": ["Read(/**/.env)"]})
    ops = [
        {
            "operation": "replace_file",
            "path": ".claude/settings.json",
            "expected_sha256": sha,
            "content": json.dumps({"permissions": {"allow": ["Read(./**)", "Grep"], "deny": ["Read(/**/.env)"]}}),
        }
    ]
    WorkspaceExecutionApplier().apply_execution_operations(
        ops, workspace_dir=ws, target_policy=WorkspaceExecutionTargetPolicy(ws), content_guard=guard_execution_write
    )
    assert "Grep" in settings.read_text(encoding="utf-8")  # 受限新增 allow 正常落盘
