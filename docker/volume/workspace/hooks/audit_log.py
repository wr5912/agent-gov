#!/usr/bin/env python3
import json
import pathlib
import sys
from datetime import datetime, timezone

payload = json.load(sys.stdin)
log_path = pathlib.Path("/data/audit.log")
log_path.parent.mkdir(parents=True, exist_ok=True)
record = {
    "ts": datetime.now(timezone.utc).isoformat(),
    "hook_event_name": payload.get("hook_event_name"),
    "tool_name": payload.get("tool_name"),
    "cwd": payload.get("cwd"),
    "session_id": payload.get("session_id"),
    "agent_type": payload.get("agent_type"),
}
with log_path.open("a", encoding="utf-8") as f:
    f.write(json.dumps(record, ensure_ascii=False) + "\n")
print("{}")
