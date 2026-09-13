from __future__ import annotations

from pathlib import Path

import yaml

from app.runtime.json_types import JsonObject

from .store import RuntimeObjectNotFound, RuntimeStateConflict, harness_digest


def agent_payload_from_workspace(workspace: Path, *, display_name: str) -> JsonObject:
    manifest_path = workspace / "agent.yaml"
    instructions_path = workspace / "AGENT.md"
    if not manifest_path.is_file() or not instructions_path.is_file():
        raise RuntimeObjectNotFound("Harness must contain agent.yaml and AGENT.md")
    loaded = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise RuntimeObjectNotFound("agent.yaml must be a mapping")
    agent = loaded.get("agent")
    if not isinstance(agent, dict) or agent.get("runtime") != "agentscope":
        raise RuntimeObjectNotFound("agent.yaml must declare the AgentScope runtime")
    context = loaded.get("context_config")
    react = loaded.get("react_config")
    invite = loaded.get("invite_config")
    system_prompt = instructions_path.read_text(encoding="utf-8")
    subagent_instructions = _subagent_runtime_instructions(workspace)
    if subagent_instructions:
        system_prompt = system_prompt.rstrip() + "\n\n" + subagent_instructions
    request_data: JsonObject = {"name": display_name, "system_prompt": system_prompt}
    if isinstance(context, dict):
        request_data["context_config"] = context
    if isinstance(react, dict):
        request_data["react_config"] = react
    if isinstance(invite, dict):
        request_data["invite_config"] = invite
    return request_data


def _subagent_runtime_instructions(workspace: Path) -> str:
    root = workspace / "subagents"
    if not root.exists():
        return ""
    if root.is_symlink() or not root.is_dir():
        raise RuntimeObjectNotFound("subagents must be a safe directory")
    digest = harness_digest(workspace)
    templates: list[tuple[str, str]] = []
    for path in sorted(root.iterdir()):
        if path.is_symlink() or not path.is_dir() or not (path / "agent.yaml").is_file():
            raise RuntimeObjectNotFound("subagent entry is invalid")
        templates.append((path.name, f"agentgov-{digest}-{path.name}"))
    if not templates:
        return ""
    declarations = "\n".join(f"- `{name}`: `subagent_type={template_type}`" for name, template_type in templates)
    return (
        "## AgentScope 团队委派契约\n\n"
        "需要委派时依次调用 `TeamCreate`、`AgentCreate`、`TeamSay`，结束后调用 `TeamDelete`。"
        "`AgentCreate` 必须使用下列当前 Harness 版本的精确 `subagent_type`，不得使用 `default` 或其他版本：\n"
        f"{declarations}\n"
    )


def session_settings_from_workspace(workspace: Path) -> tuple[str, str, str]:
    """读取只能由已发布 Harness 控制的 Session 字段。"""

    manifest_path = workspace / "agent.yaml"
    try:
        loaded = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeObjectNotFound("agent.yaml is not readable") from exc
    session = loaded.get("session") if isinstance(loaded, dict) else None
    if not isinstance(session, dict):
        raise RuntimeObjectNotFound("agent.yaml must declare session settings")
    permission_mode = session.get("permission_mode")
    if permission_mode not in {"default", "explore", "accept_edits", "dont_ask"}:
        raise RuntimeStateConflict("Harness session.permission_mode is unsupported or unsafe")
    cwd = session.get("cwd", ".")
    if not isinstance(cwd, str) or not cwd.strip():
        raise RuntimeStateConflict("Harness session.cwd must be a non-empty string")
    model_profile = session.get("model_profile", "default")
    if model_profile != "default":
        raise RuntimeStateConflict("Only the governed default model profile is configured")
    return permission_mode, cwd, model_profile
