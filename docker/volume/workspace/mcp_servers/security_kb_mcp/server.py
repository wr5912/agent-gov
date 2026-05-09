#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("security-kb")


def _load_kb() -> list[dict[str, Any]]:
    path = Path(os.getenv("SECURITY_KB_FILE", "/workspace/mcp_servers/security_kb_mcp/kb.yaml"))
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {"items": []}
    return data.get("items", [])


@mcp.tool()
def search_kb(query: str, tags: list[str] | None = None, limit: int = 10) -> dict[str, Any]:
    """Search internal security knowledge base."""
    q = (query or "").lower()
    tag_set = {t.lower() for t in tags or []}
    results = []
    for item in _load_kb():
        haystack = " ".join([
            str(item.get("id", "")), str(item.get("title", "")),
            " ".join(item.get("tags", [])), str(item.get("content", ""))
        ]).lower()
        item_tags = {str(t).lower() for t in item.get("tags", [])}
        if q and q not in haystack:
            continue
        if tag_set and not tag_set.intersection(item_tags):
            continue
        results.append(item)
    return {"query": query, "count": len(results[:limit]), "items": results[:limit]}


@mcp.tool()
def get_kb_item(item_id: str) -> dict[str, Any]:
    """Get one knowledge item."""
    for item in _load_kb():
        if item.get("id") == item_id:
            return {"found": True, "item": item}
    return {"found": False, "item_id": item_id}


if __name__ == "__main__":
    mcp.run()
