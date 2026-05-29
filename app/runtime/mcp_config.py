from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def filtered_mcp_servers(config_path: Path, allowed_names: tuple[str, ...]) -> dict[str, Any] | None:
    if not config_path.exists():
        return None
    loaded = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        return {}
    servers = loaded.get("mcpServers") if isinstance(loaded.get("mcpServers"), dict) else loaded
    if not isinstance(servers, dict):
        return {}
    allowed = set(allowed_names)
    if not allowed:
        return {}
    return {name: config for name, config in servers.items() if name in allowed and isinstance(config, dict)}
