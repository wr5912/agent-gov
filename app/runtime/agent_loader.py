"""只读发现 AgentScope Harness 中的 Skill 与私有 subagent。"""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Optional, cast

import yaml

from .json_types import JsonObject

MAX_METADATA_FILE_BYTES = 1_000_000


def parse_frontmatter_markdown(path: Path) -> tuple[JsonObject, str]:
    return parse_frontmatter_text(path.read_text(encoding="utf-8"))


def parse_frontmatter_text(text: str) -> tuple[JsonObject, str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    loaded = yaml.safe_load(text[4:end]) or {}
    metadata = loaded if isinstance(loaded, dict) else {}
    return cast(JsonObject, metadata), text[end + 5 :].strip()


def _safe_metadata_text(path: Path, *, boundary: Path) -> Optional[str]:
    try:
        if path.is_symlink() or not path.resolve().is_relative_to(boundary.resolve()):
            return None
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_METADATA_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError):
        return None


def discover_agents(workspace_dir: Path, _runtime_home: Optional[Path] = None) -> list[JsonObject]:
    root = workspace_dir / "subagents"
    if root.is_symlink() or not root.is_dir():
        return []
    items: list[JsonObject] = []
    for directory in sorted(path for path in root.iterdir() if path.is_dir() and not path.is_symlink()):
        manifest_path = directory / "agent.yaml"
        prompt_path = directory / "AGENT.md"
        manifest_text = _safe_metadata_text(manifest_path, boundary=workspace_dir)
        prompt = _safe_metadata_text(prompt_path, boundary=workspace_dir)
        if manifest_text is None or prompt is None:
            continue
        try:
            manifest = yaml.safe_load(manifest_text) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(manifest, dict):
            continue
        agent = manifest.get("agent")
        agent = agent if isinstance(agent, dict) else {}
        policy = manifest.get("workspace_policy")
        policy = policy if isinstance(policy, dict) else {}
        items.append(
            {
                "name": str(agent.get("name") or agent.get("id") or directory.name),
                "path": str(directory),
                "description": agent.get("description"),
                "model": None,
                "tools": [str(value) for value in policy.get("allowed_tools", []) if isinstance(value, str)],
                "skills": [],
                "frontmatter": manifest,
                "prompt": prompt,
            }
        )
    return items


def discover_skills(workspace_dir: Path, _runtime_home: Optional[Path] = None) -> list[JsonObject]:
    root = workspace_dir / "skills"
    if root.is_symlink() or not root.is_dir():
        return []
    items: list[JsonObject] = []
    for directory in sorted(path for path in root.iterdir() if path.is_dir() and not path.is_symlink()):
        skill_file = directory / "SKILL.md"
        text = _safe_metadata_text(skill_file, boundary=workspace_dir)
        if text is None:
            continue
        metadata, _ = parse_frontmatter_text(text)
        items.append(
            {
                "name": str(metadata.get("name") or directory.name),
                "path": str(directory),
                "description": metadata.get("description"),
                "frontmatter": metadata,
            }
        )
    return items
