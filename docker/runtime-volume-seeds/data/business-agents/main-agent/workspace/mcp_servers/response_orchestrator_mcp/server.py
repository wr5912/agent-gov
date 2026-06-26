#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("response-orchestrator")


def _plan_dir() -> Path:
    p = Path(os.getenv("RESPONSE_PLAN_DIR", "/data/outputs/response-plans"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _execution_enabled() -> bool:
    return os.getenv("RESPONSE_EXECUTION_ENABLED", "false").lower() in {"1", "true", "yes", "on"}


@mcp.tool()
def create_response_plan(
    incident_id: str,
    action_type: Literal["isolate_host", "block_ip", "block_domain", "block_hash", "disable_user", "kill_process", "custom"],
    targets: list[str],
    evidence: list[str],
    rollback_plan: str,
    validation_plan: str,
    risk_level: Literal["low", "medium", "high"] = "medium",
) -> dict[str, Any]:
    """Create a response plan file. Does not execute anything."""
    plan_id = f"RP-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"
    plan = {
        "plan_id": plan_id,
        "incident_id": incident_id,
        "action_type": action_type,
        "targets": targets,
        "evidence": evidence,
        "risk_level": risk_level,
        "rollback_plan": rollback_plan,
        "validation_plan": validation_plan,
        "status": "planned",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "requires_user_confirmation": True,
    }
    path = _plan_dir() / f"{plan_id}.json"
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"created": True, "plan_id": plan_id, "path": str(path), "plan": plan}


@mcp.tool()
def dry_run_action(plan_id: str) -> dict[str, Any]:
    """Validate and simulate a response action. Does not execute anything."""
    path = _plan_dir() / f"{plan_id}.json"
    if not path.exists():
        return {"ok": False, "error": "plan_not_found", "plan_id": plan_id}
    plan = json.loads(path.read_text(encoding="utf-8"))
    checks = [
        {"name": "has_targets", "passed": bool(plan.get("targets"))},
        {"name": "has_evidence", "passed": bool(plan.get("evidence"))},
        {"name": "has_rollback", "passed": bool(plan.get("rollback_plan"))},
        {"name": "has_validation", "passed": bool(plan.get("validation_plan"))},
    ]
    return {"ok": all(c["passed"] for c in checks), "mode": "dry-run", "plan_id": plan_id, "checks": checks, "would_execute": plan.get("action_type"), "targets": plan.get("targets")}


@mcp.tool()
def execute_approved_action(plan_id: str, approval_id: str, approver: str) -> dict[str, Any]:
    """Execute an approved action. Disabled by default; wire this to SOAR/EDR in production."""
    if not _execution_enabled():
        return {
            "executed": False,
            "reason": "execution_disabled",
            "message": "RESPONSE_EXECUTION_ENABLED=false。当前环境只允许生成计划和 dry-run。"
        }
    path = _plan_dir() / f"{plan_id}.json"
    if not path.exists():
        return {"executed": False, "error": "plan_not_found", "plan_id": plan_id}
    plan = json.loads(path.read_text(encoding="utf-8"))
    # TODO: Replace this section with SOAR/EDR/firewall integration.
    execution_record = {
        "executed": True,
        "plan_id": plan_id,
        "approval_id": approval_id,
        "approver": approver,
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "action_type": plan.get("action_type"),
        "targets": plan.get("targets"),
        "provider": "placeholder"
    }
    return execution_record


if __name__ == "__main__":
    mcp.run()
