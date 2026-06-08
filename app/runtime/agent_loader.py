from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, cast

import yaml

from .json_types import JsonObject


def _split_csv(value: object) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return None


def parse_frontmatter_markdown(path: Path) -> tuple[JsonObject, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}, text
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, flags=re.DOTALL)
    if not match:
        return {}, text
    raw_meta, body = match.groups()
    meta = yaml.safe_load(raw_meta) or {}
    if not isinstance(meta, dict):
        meta = {}
    return cast(JsonObject, meta), body.strip()


def discover_agents(workspace_dir: Path, claude_home: Optional[Path] = None) -> list[JsonObject]:
    roots = [workspace_dir / ".claude" / "agents"]
    if claude_home:
        roots.append(claude_home / "agents")

    seen: set[str] = set()
    agents: list[JsonObject] = []
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.glob("*.md")):
            meta, body = parse_frontmatter_markdown(path)
            name = str(meta.get("name") or path.stem)
            if name in seen:
                continue
            seen.add(name)
            agents.append(
                {
                    "name": name,
                    "path": str(path),
                    "description": meta.get("description"),
                    "model": meta.get("model"),
                    "tools": _split_csv(meta.get("tools")) or [],
                    "skills": _split_csv(meta.get("skills")) or [],
                    "frontmatter": meta,
                    "prompt": body,
                }
            )
    return agents


def discover_skills(workspace_dir: Path, claude_home: Optional[Path] = None) -> list[JsonObject]:
    roots = [workspace_dir / ".claude" / "skills"]
    if claude_home:
        roots.append(claude_home / "skills")

    seen: set[str] = set()
    skills: list[JsonObject] = []
    for root in roots:
        if not root.exists():
            continue
        for skill_dir in sorted([p for p in root.iterdir() if p.is_dir()]):
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue
            meta, _ = parse_frontmatter_markdown(skill_file)
            name = str(meta.get("name") or skill_dir.name)
            if name in seen:
                continue
            seen.add(name)
            skills.append(
                {
                    "name": name,
                    "path": str(skill_dir),
                    "description": meta.get("description"),
                    "frontmatter": meta,
                }
            )
    return skills
