#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

WORKSPACE_DIR = Path(os.getenv("CLAUDE_WORKSPACE", str(Path(__file__).resolve().parents[2])))

try:
    from mcp.server.fastmcp import FastMCP
except Exception as exc:  # pragma: no cover
    raise SystemExit(f"Missing dependency: install packages from {WORKSPACE_DIR / 'mcp_servers' / 'requirements.txt'}") from exc

mcp = FastMCP("soc-data")


def _load_alerts() -> list[dict[str, Any]]:
    sample = Path(os.getenv("SOC_SAMPLE_DATA", str(WORKSPACE_DIR / "mcp_servers" / "soc_data_mcp" / "sample_alerts.json")))
    if sample.exists():
        return json.loads(sample.read_text(encoding="utf-8"))
    return []


@mcp.tool()
def query_alerts(query: str = "", time_range: str = "24h", severity: str | None = None, limit: int = 20) -> dict[str, Any]:
    """Query SOC alerts. Replace this stub with SIEM/EDR API integration."""
    alerts = _load_alerts()
    q = (query or "").lower()
    if severity:
        alerts = [a for a in alerts if a.get("severity") == severity]
    if q:
        alerts = [a for a in alerts if q in json.dumps(a, ensure_ascii=False).lower()]
    return {
        "source": "sample" if not os.getenv("SOC_API_TOKEN") else "api-placeholder",
        "time_range": time_range,
        "query": query,
        "count": len(alerts[:limit]),
        "items": alerts[:limit],
        "note": "当前为示例实现。接入生产系统时请替换 _load_alerts/query 函数。",
    }


@mcp.tool()
def get_alert(alert_id: str) -> dict[str, Any]:
    """Get one alert by alert_id."""
    for alert in _load_alerts():
        if alert.get("alert_id") == alert_id:
            return {"found": True, "alert": alert}
    return {"found": False, "alert_id": alert_id}


@mcp.tool()
def summarize_alerts(time_range: str = "24h") -> dict[str, Any]:
    """Return alert summary grouped by severity and rule."""
    alerts = _load_alerts()
    by_severity: dict[str, int] = {}
    by_rule: dict[str, int] = {}
    for a in alerts:
        by_severity[a.get("severity", "unknown")] = by_severity.get(a.get("severity", "unknown"), 0) + 1
        by_rule[a.get("rule_name", "unknown")] = by_rule.get(a.get("rule_name", "unknown"), 0) + 1
    return {"time_range": time_range, "total": len(alerts), "by_severity": by_severity, "by_rule": by_rule}


@mcp.tool()
def query_process_activity(hostname: str | None = None, process_name: str | None = None, time_range: str = "24h", limit: int = 50) -> dict[str, Any]:
    """Query process activities. Stub returns process slices from sample alerts."""
    rows = []
    for a in _load_alerts():
        host = a.get("host", {})
        proc = a.get("process")
        if not proc:
            continue
        if hostname and hostname.lower() not in host.get("hostname", "").lower():
            continue
        if process_name and process_name.lower() not in proc.get("image", "").lower():
            continue
        rows.append({"timestamp": a.get("timestamp"), "host": host, "process": proc, "alert_id": a.get("alert_id")})
    return {"time_range": time_range, "count": len(rows[:limit]), "items": rows[:limit]}


@mcp.tool()
def query_network_activity(ip: str | None = None, port: int | None = None, time_range: str = "24h", limit: int = 50) -> dict[str, Any]:
    """Query network activities. Stub returns network slices from sample alerts."""
    rows = []
    for a in _load_alerts():
        net = a.get("network")
        if not net:
            continue
        if ip and ip not in (net.get("dst_ip"), a.get("host", {}).get("ip")):
            continue
        if port and port != net.get("dst_port"):
            continue
        rows.append({"timestamp": a.get("timestamp"), "host": a.get("host"), "network": net, "process": a.get("process"), "alert_id": a.get("alert_id")})
    return {"time_range": time_range, "count": len(rows[:limit]), "items": rows[:limit]}


if __name__ == "__main__":
    mcp.run()
