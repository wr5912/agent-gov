from __future__ import annotations

import re
import stat as _stat
from pathlib import Path
from typing import Optional, cast

import yaml

from .json_types import JsonObject

# markdown 元数据文件（SKILL.md / agent *.md）读取上限：足够任何合法配置，拦截超大/symlink-到-大文件的 DoS。
MAX_METADATA_FILE_BYTES = 1_000_000


def _split_csv(value: object) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return None


def parse_frontmatter_markdown(path: Path) -> tuple[JsonObject, str]:
    return parse_frontmatter_text(path.read_text(encoding="utf-8"))


def parse_frontmatter_text(text: str) -> tuple[JsonObject, str]:
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


def _safe_metadata_text(path: Path) -> Optional[str]:
    """只读普通文件的 markdown 元数据：拒 symlink、拒非普通文件、拒超大、任何异常按单项降级返回 None。"""
    try:
        if path.is_symlink():
            return None
        info = path.stat()  # 已排除 symlink，stat 不会跟随到外部
        if not _stat.S_ISREG(info.st_mode) or info.st_size > MAX_METADATA_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None


def _safe_search_root(root: Path, boundary: Optional[Path]) -> bool:
    """搜索根必须是真实目录、非 symlink；给定 boundary 时其真实路径不得逃出 boundary（防 .claude/skills symlink 逃逸）。"""
    try:
        if root.is_symlink() or not root.is_dir():
            return False
        if boundary is not None and not root.resolve().is_relative_to(boundary.resolve()):
            return False
    except (OSError, ValueError):
        return False
    return True


def discover_agents(workspace_dir: Path, claude_home: Optional[Path] = None) -> list[JsonObject]:
    roots: list[tuple[Path, Optional[Path]]] = [(workspace_dir / ".claude" / "agents", workspace_dir)]
    if claude_home:
        roots.append((claude_home / "agents", None))

    seen: set[str] = set()
    agents: list[JsonObject] = []
    for root, boundary in roots:
        if not _safe_search_root(root, boundary):
            continue
        for path in sorted(root.glob("*.md")):
            text = _safe_metadata_text(path)
            if text is None:
                continue
            meta, body = parse_frontmatter_text(text)
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
    roots: list[tuple[Path, Optional[Path]]] = [(workspace_dir / ".claude" / "skills", workspace_dir)]
    if claude_home:
        roots.append((claude_home / "skills", None))

    seen: set[str] = set()
    skills: list[JsonObject] = []
    for root, boundary in roots:
        if not _safe_search_root(root, boundary):
            continue
        for skill_dir in sorted([p for p in root.iterdir() if p.is_dir()]):
            if skill_dir.is_symlink():
                continue
            text = _safe_metadata_text(skill_dir / "SKILL.md")
            if text is None:
                continue
            meta, _ = parse_frontmatter_text(text)
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
