from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .agent_profiles import AgentRuntimeProfile
from .records.json_types import JsonObject


def profile_version_snapshot(profile: AgentRuntimeProfile, *, version_id: str | None = None) -> JsonObject:
    """Return reproducibility metadata for attribution/proposal runtime profiles."""
    return {
        "profile_name": profile.name,
        "profile_role": profile.role,
        "agent_version": version_id or f"{profile.name}-unversioned",
        "claude_md_hash": _hash_file(profile.workspace_dir / "CLAUDE.md"),
        "skills_hash": _hash_tree(profile.workspace_dir / ".claude" / "skills"),
        "mcp_config_hash": _hash_file(profile.mcp_config_path),
        "settings_hash": _hash_file(profile.project_settings_path),
    }


def _hash_file(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_tree(path: Path) -> str | None:
    if not path.exists():
        return None
    entries: list[JsonObject] = []
    for root, dirnames, filenames in os.walk(path, topdown=True, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if name not in {".git", "__pycache__"})
        for filename in sorted(filenames):
            file_path = Path(root) / filename
            if file_path.name.endswith((".pyc", ".pyo")):
                continue
            rel = file_path.relative_to(path).as_posix()
            entries.append({"path": rel, "sha256": _hash_file(file_path)})
    digest = hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()
    return digest
