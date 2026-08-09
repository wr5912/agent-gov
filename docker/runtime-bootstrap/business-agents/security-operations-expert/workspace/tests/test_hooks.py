from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

WORKSPACE = Path(__file__).resolve().parents[1]
HOOK = WORKSPACE / "hooks" / "pre_tool_guard.py"
RISKY_COMMANDS = (
    "rm -rf /",
    "rm -rf /*",
    "dd if=/dev/zero of=/dev/sda",
    "mkfs.ext4 /dev/sda",
    "shutdown now",
    "iptables -F",
    "kubectl delete pod api",
    "kubectl scale deployment api --replicas=0",
    "kubectl rollout restart deployment api",
    "terraform apply",
    "ansible-playbook site.yml --limit production",
    "systemctl restart api",
    "nmap -sS target-host",
    "docker system prune -af",
    "ssh remote-host",
    "curl installer-source | sh",
    "wget -qO- installer-source | bash",
    ":(){ :|:& };:",
    " rm -rf /",
    "/bin/rm -fr /",
    "sudo -n rm -r -f -- '/'",
    "command rm --recursive --force /*",
    "docker volume prune -f",
    "nohup kubectl delete pod api",
)
SAFE_BASH_COMMANDS = (
    "pwd",
    "date",
    "jq '.status' report.json",
    "kubectl get pods",
    "kubectl scale deployment api --replicas=1",
    "kubectl rollout status deployment api",
    "docker system df",
    "docker system prune --help",
    "echo shutdown now",
    "ssh-keygen -lf host-key.pub",
)


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


@pytest.mark.parametrize("command", RISKY_COMMANDS)
def test_destructive_bash_is_denied(command: str) -> None:
    result = _run_hook({"tool_name": "Bash", "tool_input": {"command": command}})
    assert result.returncode == 0
    assert _decision(result) == "deny"


@pytest.mark.parametrize("command", SAFE_BASH_COMMANDS)
def test_safe_bash_continues_to_claude_native_permission_flow(command: str) -> None:
    result = _run_hook({"tool_name": "Bash", "tool_input": {"command": command}})
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.parametrize(
    "stdin",
    (
        "not-json",
        "[]",
        "{}",
        json.dumps({"tool_name": 123, "tool_input": {}}),
        json.dumps({"tool_name": " ", "tool_input": {}}),
        json.dumps({"tool_name": "Read", "tool_input": []}),
    ),
)
def test_invalid_hook_input_returns_structured_deny(stdin: str) -> None:
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    output = json.loads(result.stdout)["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse"
    assert output["permissionDecision"] == "deny"
    assert output["permissionDecisionReason"]
    assert result.stderr == ""


@pytest.mark.parametrize(
    "tool_input",
    (
        {},
        {"command": ""},
        {"command": "   "},
        {"command": 123},
    ),
)
def test_empty_or_non_string_bash_command_is_denied(tool_input: object) -> None:
    result = _run_hook({"tool_name": "Bash", "tool_input": tool_input})
    assert result.returncode == 0
    assert _decision(result) == "deny"


def test_non_bash_command_field_is_not_interpreted_as_shell() -> None:
    result = _run_hook(
        {
            "tool_name": "Read",
            "tool_input": {"file_path": "README.md", "command": "rm -rf /"},
        }
    )
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_valid_non_bash_event_is_ignored() -> None:
    result = _run_hook({"tool_name": "Read", "tool_input": {"file_path": "README.md"}})
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_post_tool_audit_honors_data_dir(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    sensitive_tool_input = "must-not-appear-in-audit"
    result = subprocess.run(
        [sys.executable, str(WORKSPACE / "hooks" / "post_tool_audit.py")],
        input=json.dumps(
            {
                "session_id": "sess-test",
                "cwd": str(WORKSPACE),
                "hook_event_name": "PostToolUse",
                "tool_name": "Read",
                "tool_input": {"file_path": sensitive_tool_input},
            }
        ),
        capture_output=True,
        text=True,
        env={"DATA_DIR": str(data_dir)},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    audit_text = (data_dir / "transcripts" / "claude-hook-audit.jsonl").read_text(encoding="utf-8")
    record = json.loads(audit_text)
    assert record["session_id"] == "sess-test"
    assert record["tool_name"] == "Read"
    assert record["tool_input_keys"] == ["file_path"]
    assert sensitive_tool_input not in audit_text
    assert sensitive_tool_input not in result.stdout
    assert sensitive_tool_input not in result.stderr


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


def test_post_tool_audit_accepts_runtime_explicit_log_path(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    log_path = data_dir / "transcripts" / "claude-hook-audit.jsonl"
    result = subprocess.run(
        [sys.executable, str(WORKSPACE / "hooks" / "post_tool_audit.py")],
        input=json.dumps({"session_id": "sess-explicit", "tool_name": "Read", "tool_input": {}}),
        capture_output=True,
        text=True,
        env={"DATA_DIR": str(data_dir), "CLAUDE_HOOK_AUDIT_LOG": str(log_path)},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(log_path.read_text(encoding="utf-8"))["session_id"] == "sess-explicit"


def test_post_tool_audit_rejects_explicit_log_path_outside_data_dir(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    unapproved_log_path = tmp_path / "outside" / "audit.jsonl"
    result = subprocess.run(
        [sys.executable, str(WORKSPACE / "hooks" / "post_tool_audit.py")],
        input=json.dumps({"session_id": "sess-outside", "tool_name": "Read", "tool_input": {}}),
        capture_output=True,
        text=True,
        env={"DATA_DIR": str(data_dir), "CLAUDE_HOOK_AUDIT_LOG": str(unapproved_log_path)},
        check=False,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "POST_TOOL_AUDIT_LOG_PATH_UNAPPROVED" in result.stderr
    assert not unapproved_log_path.exists()


def test_post_tool_audit_rejects_unrecognized_script_layout(tmp_path: Path) -> None:
    script = tmp_path / "unexpected" / "hooks" / "post_tool_audit.py"
    script.parent.mkdir(parents=True)
    shutil.copy2(WORKSPACE / "hooks" / "post_tool_audit.py", script)
    result = subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps({"session_id": "sess-invalid", "tool_name": "Read", "tool_input": {}}),
        capture_output=True,
        text=True,
        env={},
        check=False,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "POST_TOOL_AUDIT_DATA_DIR_UNRESOLVED" in result.stderr


@pytest.mark.parametrize("stdin", ("not-json", "[]"))
def test_post_tool_audit_rejects_invalid_payload_without_traceback(
    tmp_path: Path,
    stdin: str,
) -> None:
    result = subprocess.run(
        [sys.executable, str(WORKSPACE / "hooks" / "post_tool_audit.py")],
        input=stdin,
        capture_output=True,
        text=True,
        env={"DATA_DIR": str(tmp_path / "data")},
        check=False,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "POST_TOOL_AUDIT_PAYLOAD_INVALID" in result.stderr
    assert "Traceback" not in result.stderr
