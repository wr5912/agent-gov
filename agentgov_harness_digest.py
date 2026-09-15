"""AgentGov Harness 的无自引用、跨进程 canonical content digest。"""

from __future__ import annotations

import hashlib
import json
import stat
from fnmatch import fnmatch
from pathlib import Path

import yaml

HARNESS_CONTENT_ROOTS = ("agent.yaml", "AGENT.md", "skills", "mcp", "subagents", "tests", "references")
HARNESS_EXCLUDED_NAMES = frozenset(
    {
        ".cache",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "dist",
        "node_modules",
    },
)
HARNESS_EXCLUDED_PATTERNS = ("*.pyc", "*.pyo")


def harness_content_digest(workspace: Path) -> str:
    """摘要完整 Harness；仅从 agent.yaml 投影中删除自引用字段。"""

    digest = hashlib.sha256()
    for relative in HARNESS_CONTENT_ROOTS:
        target = workspace / relative
        if target.is_symlink():
            raise ValueError(f"Harness entry must not be a symlink: {target}")
        if not target.exists():
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if relative == "agent.yaml":
            nested = _manifest_digest(target)
        else:
            nested = _file_digest(target) if target.is_file() else _tree_digest(target)
        digest.update(nested.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _manifest_digest(path: Path) -> str:
    _require_regular(path)
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"Harness agent.yaml is invalid: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Harness agent.yaml must be an object: {path}")
    harness = payload.get("harness")
    if isinstance(harness, dict):
        harness = dict(harness)
        harness.pop("content_digest", None)
        payload = dict(payload)
        payload["harness"] = harness
    try:
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Harness agent.yaml must contain JSON-compatible values: {path}") from exc
    return hashlib.sha256(canonical).hexdigest()


def _file_digest(path: Path) -> str:
    _require_regular(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_digest(root: Path) -> str:
    _require_directory(root)
    digest = hashlib.sha256()
    entries = sorted(root.rglob("*"))
    for path in entries:
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise ValueError(f"Harness contains a non-regular entry: {path}")
        relative = path.relative_to(root)
        if any(part in HARNESS_EXCLUDED_NAMES for part in relative.parts) or any(fnmatch(path.name, pattern) for pattern in HARNESS_EXCLUDED_PATTERNS):
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _require_regular(path: Path) -> None:
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError(f"Harness entry must be a regular file: {path}")


def _require_directory(path: Path) -> None:
    if path.is_symlink() or not stat.S_ISDIR(path.stat().st_mode):
        raise ValueError(f"Harness entry must be a real directory: {path}")
