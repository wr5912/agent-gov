#!/usr/bin/env python3
"""Claude Code PostToolUse hook: append compact audit records."""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

payload = json.load(sys.stdin)
data_dir = Path(os.getenv("DATA_DIR", "/data"))
log_path = Path(os.getenv("CLAUDE_HOOK_AUDIT_LOG", str(data_dir / "transcripts" / "claude-hook-audit.jsonl")))
log_path.parent.mkdir(parents=True, exist_ok=True)

record = {
    "ts": datetime.now(timezone.utc).isoformat(),
    "session_id": payload.get("session_id"),
    "cwd": payload.get("cwd"),
    "event": payload.get("hook_event_name"),
    "tool_name": payload.get("tool_name"),
    "tool_input_keys": sorted(list((payload.get("tool_input") or {}).keys())) if isinstance(payload.get("tool_input"), dict) else [],
    "has_tool_response": payload.get("tool_response") is not None,
}
with log_path.open("a", encoding="utf-8") as f:
    f.write(json.dumps(record, ensure_ascii=False) + "\n")
