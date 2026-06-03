import json
import subprocess
import sys


def test_post_tool_audit_uses_data_dir_fallback(tmp_path):
    data_dir = tmp_path / "data"
    script = "docker/volume/main-workspace/hooks/post_tool_audit.py"
    payload = {
        "session_id": "sess-test",
        "cwd": "/main-workspace",
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
