from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

CONTAINER_DEFAULT_MCP_SERVER_URL = "http://host.docker.internal:58001/mcp"
LOCAL_DEBUG_DEFAULT_MCP_SERVER_URL = "http://localhost:58001/mcp"
UNRESOLVED_TEMPLATE_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::-[^}]*)?\}")

_PROFILE_WORKSPACE_DEFAULTS = {
    "main-workspace": ("MAIN_WORKSPACE_DIR", "/main-workspace"),
    "attribution-analyzer-workspace": ("ATTRIBUTION_ANALYZER_WORKSPACE_DIR", "/attribution-analyzer-workspace"),
    "proposal-generator-workspace": ("PROPOSAL_GENERATOR_WORKSPACE_DIR", "/proposal-generator-workspace"),
    "execution-optimizer-workspace": ("EXECUTION_OPTIMIZER_WORKSPACE_DIR", "/execution-optimizer-workspace"),
    "eval-case-governor-workspace": ("EVAL_CASE_GOVERNOR_WORKSPACE_DIR", "/eval-case-governor-workspace"),
    "regression-impact-analyzer-workspace": ("REGRESSION_IMPACT_ANALYZER_WORKSPACE_DIR", "/regression-impact-analyzer-workspace"),
}
_PROFILE_CLAUDE_ROOT_DEFAULTS = {
    "main": ("MAIN_CLAUDE_ROOT", "/claude-roots/main"),
    "attribution-analyzer": ("ATTRIBUTION_ANALYZER_CLAUDE_ROOT", "/claude-roots/attribution-analyzer"),
    "proposal-generator": ("PROPOSAL_GENERATOR_CLAUDE_ROOT", "/claude-roots/proposal-generator"),
    "execution-optimizer": ("EXECUTION_OPTIMIZER_CLAUDE_ROOT", "/claude-roots/execution-optimizer"),
    "eval-case-governor": ("EVAL_CASE_GOVERNOR_CLAUDE_ROOT", "/claude-roots/eval-case-governor"),
    "regression-impact-analyzer": ("REGRESSION_IMPACT_ANALYZER_CLAUDE_ROOT", "/claude-roots/regression-impact-analyzer"),
}
_MANAGED_ACTIVE_FILENAMES = {".mcp.json", "agent.yaml"}
_MANAGED_JSON_FILENAMES = {".mcp.json", "settings.json"}
_TEMPLATE_TEXT_FILENAMES = {".mcp.json", ".worktreeinclude", ".gitignore", "requirements.txt"}
_TEMPLATE_TEXT_SUFFIXES = {
    "",
    ".example",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".txt",
    ".yaml",
    ".yml",
}


@dataclass(frozen=True)
class RuntimeTemplateRenderContext:
    mode: str
    runtime_root: Path
    data_dir: Path
    mcp_server_url: str
    allowed_network_domains: tuple[str, ...]
    container_path_map: Mapping[str, Path]


def build_render_context(*, mode: str, env: Mapping[str, str], runtime_root: Path) -> RuntimeTemplateRenderContext:
    normalized = mode.strip()
    if normalized not in {"container", "local-debug"}:
        raise ValueError(f"Unsupported runtime template mode={normalized!r}")

    container_path_map: dict[str, Path] = {}
    for workspace_name, (env_name, default_path) in _PROFILE_WORKSPACE_DEFAULTS.items():
        default = Path(default_path) if normalized == "container" else runtime_root / workspace_name
        container_path_map[default_path] = _path_from_env(env, env_name, default)
    for claude_root_name, (env_name, default_path) in _PROFILE_CLAUDE_ROOT_DEFAULTS.items():
        default = Path(default_path) if normalized == "container" else runtime_root / "claude-roots" / claude_root_name
        container_path_map[default_path] = _path_from_env(env, env_name, default)

    data_default = runtime_root / "data" if normalized == "local-debug" else Path("/data")
    data_dir = _path_from_env(env, "DATA_DIR", data_default)
    container_path_map["/data"] = data_dir

    default_mcp_url = LOCAL_DEBUG_DEFAULT_MCP_SERVER_URL if normalized == "local-debug" else CONTAINER_DEFAULT_MCP_SERVER_URL
    mcp_server_url = env.get("MCP_SERVER_URL") or default_mcp_url
    domains = _allowed_network_domains(normalized, env)
    return RuntimeTemplateRenderContext(
        mode=normalized,
        runtime_root=runtime_root,
        data_dir=data_dir,
        mcp_server_url=mcp_server_url,
        allowed_network_domains=domains,
        container_path_map=container_path_map,
    )


def is_managed_active_config(rel_path: Path) -> bool:
    if rel_path.name in _MANAGED_ACTIVE_FILENAMES:
        return True
    return len(rel_path.parts) >= 2 and rel_path.parts[-2:] == (".claude", "settings.json")


def is_template_managed_text_file(rel_path: Path) -> bool:
    return rel_path.name in _TEMPLATE_TEXT_FILENAMES or rel_path.suffix in _TEMPLATE_TEXT_SUFFIXES or rel_path.name.endswith(".example")


def render_template_file(text: str, *, rel_path: Path, context: RuntimeTemplateRenderContext) -> str:
    if not is_template_managed_text_file(rel_path):
        return text
    if rel_path.name in _MANAGED_JSON_FILENAMES:
        loaded = json.loads(text)
        rendered = _render_json_value(loaded, rel_path=rel_path, context=context)
        return json.dumps(rendered, ensure_ascii=False, indent=2) + "\n"
    return _replace_container_paths(text, context).replace("${MCP_SERVER_URL}", context.mcp_server_url)


def validate_rendered_config(text: str, *, rel_path: Path, context: RuntimeTemplateRenderContext) -> list[str]:
    if not is_template_managed_text_file(rel_path):
        return []
    errors: list[str] = []
    if is_managed_active_config(rel_path):
        unresolved = sorted(set(UNRESOLVED_TEMPLATE_RE.findall(text)))
        if unresolved:
            errors.append(f"{rel_path.as_posix()} contains unresolved template placeholder(s): {', '.join(unresolved)}")
    if context.mode == "local-debug":
        for container_path in _container_path_markers():
            if _contains_container_path_marker(text, container_path):
                errors.append(f"{rel_path.as_posix()} contains container-only path {container_path!r} in local-debug mode")
                break
    if context.mode == "container" and "/tmp/local-debug-volume-agent-runtime" in text:
        errors.append(f"{rel_path.as_posix()} contains local-debug runtime path in container mode")
    return errors


def _render_json_value(value: Any, *, rel_path: Path, context: RuntimeTemplateRenderContext) -> Any:
    if isinstance(value, str):
        return _replace_container_paths(value, context).replace("${MCP_SERVER_URL}", context.mcp_server_url)
    if isinstance(value, list):
        if (
            rel_path.name == "settings.json"
            and value
            and all(isinstance(item, str) for item in value)
            and set(value) <= {"${SERVICE_HOST}", "${INTERNAL_DOMAIN}"}
        ):
            return list(context.allowed_network_domains)
        return [_render_json_value(item, rel_path=rel_path, context=context) for item in value]
    if isinstance(value, dict):
        return {key: _render_json_value(item, rel_path=rel_path, context=context) for key, item in value.items()}
    return value


def _replace_container_paths(text: str, context: RuntimeTemplateRenderContext) -> str:
    rendered = text
    for container_path, runtime_path in sorted(context.container_path_map.items(), key=lambda item: len(item[0]), reverse=True):
        rendered = rendered.replace(container_path, runtime_path.as_posix())
    return rendered


def _path_from_env(env: Mapping[str, str], name: str, default: Path) -> Path:
    raw = env.get(name)
    return Path(raw).expanduser() if raw else default


def _allowed_network_domains(mode: str, env: Mapping[str, str]) -> tuple[str, ...]:
    raw = env.get("CLAUDE_ALLOWED_NETWORK_DOMAINS")
    if raw:
        return tuple(item.strip() for item in raw.split(",") if item.strip())
    if mode == "local-debug":
        return ("localhost", "127.0.0.1", "host.docker.internal", "*.internal", "*.corp")
    return ("localhost", "host.docker.internal", "*.internal", "*.corp")


def _container_path_markers() -> tuple[str, ...]:
    return cast(
        tuple[str, ...],
        tuple(
            sorted(
                [
                    "/main-workspace",
                    "/attribution-analyzer-workspace",
                    "/proposal-generator-workspace",
                    "/execution-optimizer-workspace",
                    "/eval-case-governor-workspace",
                    "/regression-impact-analyzer-workspace",
                    "/claude-roots",
                    "/data",
                ],
                key=len,
                reverse=True,
            )
        ),
    )


def _contains_container_path_marker(text: str, marker: str) -> bool:
    pattern = rf"(?<![A-Za-z0-9_.-]){re.escape(marker)}(?=$|[\s/)\",\]])"
    return re.search(pattern, text) is not None
