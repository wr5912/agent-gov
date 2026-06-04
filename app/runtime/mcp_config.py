from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, cast

from .json_types import JsonObject

_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


@dataclass(frozen=True)
class McpConfigResolution:
    path: Path
    source: str


class McpConfigError(RuntimeError):
    """Raised when the selected MCP config cannot be passed to Claude Code."""


def resolve_main_mcp_config_path(workspace_dir: Path, explicit_path: Path | None) -> McpConfigResolution:
    if explicit_path is not None:
        path = _expand_path(explicit_path, os.environ)
        mapped_path = _map_to_workspace_path(path, workspace_dir)
        if mapped_path is not None:
            return McpConfigResolution(path=mapped_path, source="explicit_env_workspace_mount")
        return McpConfigResolution(path=path, source="explicit_env")
    local_path = workspace_dir / ".mcp.local.json"
    if local_path.exists():
        return McpConfigResolution(path=local_path, source="workspace_local")
    return McpConfigResolution(path=workspace_dir / ".mcp.json", source="workspace_template")


def filtered_mcp_servers(
    config_path: Path,
    allowed_names: tuple[str, ...],
    env: Mapping[str, str] | None = None,
) -> JsonObject | None:
    env = env or os.environ
    config_path = _expand_path(config_path, env)
    unresolved_path_vars = _PLACEHOLDER_RE.findall(str(config_path))
    if unresolved_path_vars:
        placeholders = ", ".join(sorted({match[0] for match in unresolved_path_vars}))
        raise McpConfigError(f"MCP config path {config_path} contains unresolved placeholder(s): {placeholders}")
    servers = _load_servers(config_path)
    if servers is None:
        return None
    allowed = set(allowed_names)
    if not allowed:
        return {}
    filtered = {name: config for name, config in servers.items() if name in allowed and isinstance(config, dict)}
    expanded = _expand_placeholders(filtered, env)
    unresolved = _find_unresolved_placeholders(expanded)
    if unresolved:
        placeholders = ", ".join(sorted({str(item["placeholder"]) for item in unresolved}))
        raise McpConfigError(f"MCP config {config_path} contains unresolved placeholder(s): {placeholders}")
    return cast(JsonObject, expanded)


def build_mcp_config_summary(
    config_path: Path,
    allowed_names: tuple[str, ...],
    env: Mapping[str, str] | None = None,
) -> JsonObject:
    env = env or os.environ
    config_path = _expand_path(config_path, env)
    summary: JsonObject = {
        "path": str(config_path),
        "exists": config_path.exists(),
        "allowed_servers": list(allowed_names),
        "selected_servers": [],
        "unresolved_placeholders": [],
        "server_summaries": [],
        "error": None,
    }
    try:
        summary["path_unresolved_placeholders"] = sorted({match[0] for match in _PLACEHOLDER_RE.findall(str(config_path))})
        servers = _load_servers(config_path)
        if servers is None:
            return cast(JsonObject, summary)
        allowed = set(allowed_names)
        selected = {name: config for name, config in servers.items() if name in allowed and isinstance(config, dict)}
        expanded = _expand_placeholders(selected, env)
        summary["selected_servers"] = sorted(selected)
        summary["unresolved_placeholders"] = _find_unresolved_placeholders(expanded)
        summary["server_summaries"] = [
            {
                "name": name,
                "type": config.get("type") if isinstance(config, dict) else None,
                "url_present": bool(config.get("url")) if isinstance(config, dict) else False,
                "url_has_placeholder": bool(_PLACEHOLDER_RE.search(str(config.get("url", ""))))
                if isinstance(config, dict)
                else False,
            }
            for name, config in sorted(selected.items())
        ]
    except Exception as exc:  # pragma: no cover - defensive evidence capture
        summary["error"] = f"{exc.__class__.__name__}: {exc}"
    return cast(JsonObject, summary)


def _expand_path(path: Path, env: Mapping[str, str]) -> Path:
    expanded = _expand_placeholders(str(path), env)
    if not isinstance(expanded, str):
        expanded = str(path)
    return Path(os.path.expanduser(os.path.expandvars(expanded)))


def _map_to_workspace_path(path: Path, workspace_dir: Path) -> Path | None:
    if path.exists():
        return None
    parts = path.parts
    if "main-workspace" not in parts:
        return None
    index = parts.index("main-workspace")
    relative = Path(*parts[index + 1 :]) if index + 1 < len(parts) else Path()
    candidate = workspace_dir / relative
    return candidate if candidate.exists() else None


def _load_servers(config_path: Path) -> JsonObject | None:
    if not config_path.exists():
        return None
    loaded = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        return {}
    servers = loaded.get("mcpServers") if isinstance(loaded.get("mcpServers"), dict) else loaded
    if not isinstance(servers, dict):
        return {}
    return cast(JsonObject, servers)


def _expand_placeholders(value: Any, env: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return _PLACEHOLDER_RE.sub(lambda match: _replacement_value(match, env), value)
    if isinstance(value, list):
        return [_expand_placeholders(item, env) for item in value]
    if isinstance(value, dict):
        return {key: _expand_placeholders(item, env) for key, item in value.items()}
    return value


def _replacement_value(match: re.Match[str], env: Mapping[str, str]) -> str:
    value = env.get(match.group(1))
    if value:
        return value
    default = match.group(2)
    if default is not None:
        return default
    return match.group(0)


def _find_unresolved_placeholders(value: Any, path: str = "$") -> list[JsonObject]:
    if isinstance(value, str):
        return [
            {"path": path, "placeholder": match.group(1)}
            for match in _PLACEHOLDER_RE.finditer(value)
        ]
    if isinstance(value, list):
        return [
            item
            for index, child in enumerate(value)
            for item in _find_unresolved_placeholders(child, f"{path}[{index}]")
        ]
    if isinstance(value, dict):
        return [
            item
            for key, child in value.items()
            for item in _find_unresolved_placeholders(child, f"{path}.{key}")
        ]
    return []
