#!/usr/bin/env python3
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("report-template")


def _template_dir() -> Path:
    return Path(os.getenv("REPORT_TEMPLATE_DIR", "/data/business-agents/main-agent/workspace/templates/reports"))


def _output_dir() -> Path:
    p = Path(os.getenv("REPORT_OUTPUT_DIR", "/data/outputs/reports"))
    p.mkdir(parents=True, exist_ok=True)
    return p


@mcp.tool()
def list_report_templates() -> dict[str, Any]:
    """List available report templates."""
    templates = []
    for path in sorted(_template_dir().glob("*.md")):
        templates.append({"name": path.stem, "path": str(path)})
    return {"count": len(templates), "items": templates}


@mcp.tool()
def get_report_template(name: str) -> dict[str, Any]:
    """Get a report template by name."""
    path = _template_dir() / f"{name}.md"
    if not path.exists():
        return {"found": False, "name": name}
    return {"found": True, "name": name, "content": path.read_text(encoding="utf-8")}


@mcp.tool()
def create_report_file(name: str, content: str) -> dict[str, Any]:
    """Create a markdown report file under output directory."""
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name).strip("-") or "report"
    path = _output_dir() / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{safe}.md"
    path.write_text(content, encoding="utf-8")
    return {"created": True, "path": str(path)}


if __name__ == "__main__":
    mcp.run()
