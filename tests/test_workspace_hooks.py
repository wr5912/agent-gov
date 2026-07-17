import json
import shutil
import subprocess
import sys
from pathlib import Path


def test_post_tool_audit_honors_data_dir_env(tmp_path):
    data_dir = tmp_path / "data"
    script = "docker/runtime-volume-seeds/data/business-agents/main-agent/workspace/hooks/post_tool_audit.py"
    payload = {
        "session_id": "sess-test",
        "cwd": "/data/business-agents/main-agent/workspace",
        "hook_event_name": "PostToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": "CLAUDE.md"},
        "duration_ms": 12,
    }

    result = subprocess.run(
        [sys.executable, script],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env={"DATA_DIR": str(data_dir)},
        check=False,
    )

    log_path = data_dir / "transcripts" / "claude-hook-audit.jsonl"
    assert result.returncode == 0, result.stderr
    assert log_path.exists()
    record = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert record["session_id"] == "sess-test"
    assert record["tool_name"] == "Read"


def test_post_tool_audit_derives_local_debug_data_dir_from_workspace(tmp_path):
    source = Path("docker/runtime-volume-seeds/data/business-agents/main-agent/workspace/hooks/post_tool_audit.py")
    workspace = tmp_path / "runtime" / "data" / "business-agents" / "main-agent" / "workspace"
    script = workspace / "hooks" / "post_tool_audit.py"
    script.parent.mkdir(parents=True)
    shutil.copy2(source, script)

    result = subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(
            {
                "session_id": "sess-local-debug",
                "cwd": str(workspace),
                "hook_event_name": "PostToolUse",
                "tool_name": "Read",
                "tool_input": {},
            }
        ),
        text=True,
        capture_output=True,
        env={},
        check=False,
    )

    log_path = tmp_path / "runtime" / "data" / "transcripts" / "claude-hook-audit.jsonl"
    assert result.returncode == 0, result.stderr
    assert json.loads(log_path.read_text(encoding="utf-8"))["session_id"] == "sess-local-debug"
