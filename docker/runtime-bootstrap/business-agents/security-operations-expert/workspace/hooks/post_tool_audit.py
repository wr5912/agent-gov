#!/usr/bin/env python3
"""Claude Code PostToolUse hook: append compact audit records."""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def fail_data_dir_resolution() -> None:
    print(
        "POST_TOOL_AUDIT_DATA_DIR_UNRESOLVED: hook 路径不符合 <runtime>/data/business-agents/<agent_id>/workspace/hooks/，请显式设置 DATA_DIR。",
        file=sys.stderr,
    )
    raise SystemExit(2)


def fail_log_path_resolution() -> None:
    print(
        "POST_TOOL_AUDIT_LOG_PATH_UNAPPROVED: CLAUDE_HOOK_AUDIT_LOG 必须指向批准 DATA_DIR 下的 transcripts/claude-hook-audit.jsonl。",
        file=sys.stderr,
    )
    raise SystemExit(2)


def derive_data_dir(script_path: Path) -> Path:
    hooks_dir = script_path.resolve().parent
    workspace_dir = hooks_dir.parent
    agent_dir = workspace_dir.parent
    business_agents_dir = agent_dir.parent
    data_dir = business_agents_dir.parent
    if not (
        hooks_dir.name == "hooks"
        and workspace_dir.name == "workspace"
        and business_agents_dir.name == "business-agents"
        and data_dir.name == "data"
        and bool(agent_dir.name)
    ):
        fail_data_dir_resolution()
    return data_dir


def resolve_log_path() -> Path:
    explicit_data_dir = os.getenv("DATA_DIR")
    data_dir = Path(explicit_data_dir) if explicit_data_dir else derive_data_dir(Path(__file__))
    approved_log_path = data_dir / "transcripts" / "claude-hook-audit.jsonl"
    explicit_log_path = os.getenv("CLAUDE_HOOK_AUDIT_LOG")
    if explicit_log_path and Path(explicit_log_path).resolve() != approved_log_path.resolve():
        fail_log_path_resolution()
    return approved_log_path


payload = json.load(sys.stdin)
log_path = resolve_log_path()
log_path.parent.mkdir(parents=True, exist_ok=True)

tool_input = payload.get("tool_input")
record = {
    "ts": datetime.now(timezone.utc).isoformat(),
    "session_id": payload.get("session_id"),
    "cwd": payload.get("cwd"),
    "event": payload.get("hook_event_name"),
    "tool_name": payload.get("tool_name"),
    "tool_input_keys": sorted(tool_input) if isinstance(tool_input, dict) else [],
    "has_tool_response": payload.get("tool_response") is not None,
}
with log_path.open("a", encoding="utf-8") as f:
    f.write(json.dumps(record, ensure_ascii=False) + "\n")
