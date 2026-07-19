from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
HOOK = WORKSPACE / "hooks" / "pre_tool_guard.py"


def _run_hook(payload: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def _decision(result: subprocess.CompletedProcess[str]) -> str | None:
    if not result.stdout.strip():
        return None
    return json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"]


def test_mcp_tools_continue_to_claude_native_permission_flow() -> None:
    for tool in (
        "mcp__sec-ops__soc_api__create",
        "mcp__sec-ops__soc_api__manual",
        "mcp__sec-ops-data__query_alerts",
    ):
        result = _run_hook({"tool_name": tool, "tool_input": {}})
        assert result.returncode == 0
        assert _decision(result) is None


def test_destructive_bash_is_denied() -> None:
    for command in ("rm -rf /", "systemctl restart nginx", "kubectl delete pod api", "terraform apply"):
        result = _run_hook({"tool_name": "Bash", "tool_input": {"command": command}})
        assert result.returncode == 0
        assert _decision(result) == "deny"


def test_invalid_hook_input_fails_closed() -> None:
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input="not-json",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "failed closed" in result.stderr


def test_post_tool_audit_honors_data_dir(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    result = subprocess.run(
        [sys.executable, str(WORKSPACE / "hooks" / "post_tool_audit.py")],
        input=json.dumps(
            {
                "session_id": "sess-test",
                "cwd": str(WORKSPACE),
                "hook_event_name": "PostToolUse",
                "tool_name": "Read",
                "tool_input": {"file_path": "CLAUDE.md"},
            }
        ),
        capture_output=True,
        text=True,
        env={"DATA_DIR": str(data_dir)},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    record = json.loads((data_dir / "transcripts" / "claude-hook-audit.jsonl").read_text(encoding="utf-8"))
    assert record["session_id"] == "sess-test"
    assert record["tool_name"] == "Read"


def test_post_tool_audit_derives_data_dir_from_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "runtime" / "data" / "business-agents" / "imported-agent" / "workspace"
    script = workspace / "hooks" / "post_tool_audit.py"
    script.parent.mkdir(parents=True)
    shutil.copy2(WORKSPACE / "hooks" / "post_tool_audit.py", script)
    result = subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps({"session_id": "sess-local", "tool_name": "Read", "tool_input": {}}),
        capture_output=True,
        text=True,
        env={},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    log_path = tmp_path / "runtime" / "data" / "transcripts" / "claude-hook-audit.jsonl"
    assert json.loads(log_path.read_text(encoding="utf-8"))["session_id"] == "sess-local"
