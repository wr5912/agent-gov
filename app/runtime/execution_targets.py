from __future__ import annotations

import fnmatch
import hashlib
from pathlib import Path
from typing import Optional

from .workspace_policy import WORKSPACE_EXCLUDED_NAMES, WORKSPACE_EXCLUDED_PATTERNS

MAX_EXECUTION_TARGET_CONTEXT_BYTES = 200_000


class WorkspaceExecutionTargetPolicy:
    """Validates and describes files that execution-optimizer may edit."""

    def __init__(self, workspace_dir: Path) -> None:
        self.workspace_dir = workspace_dir

    def target_allowed(self, target_path: str) -> bool:
        return self.denied_reason(target_path) is None

    def denied_reason(self, target_path: str) -> Optional[str]:
        rel = self.relative_path(target_path)
        if rel is None:
            return "unsafe_target_path"
        if self.rel_excluded(rel):
            return "workspace_excluded_path"
        if not self.target_path(target_path):
            return "target_path_escapes_workspace"
        return None

    def policy_json(self) -> dict[str, object]:
        return {
            "type": "main_workspace_managed_full_with_excludes",
            "workspace_root": str(self.workspace_dir),
            "excluded_names": sorted(WORKSPACE_EXCLUDED_NAMES),
            "excluded_patterns": list(WORKSPACE_EXCLUDED_PATTERNS),
            "max_inline_text_bytes": MAX_EXECUTION_TARGET_CONTEXT_BYTES,
        }

    def file_contexts(self, target_paths: list[str]) -> list[dict[str, object]]:
        return [self.file_context(path) for path in target_paths]

    def file_context(self, target_path: str) -> dict[str, object]:
        context: dict[str, object] = {
            "path": target_path,
            "managed": False,
            "exists": False,
            "type": "missing",
            "size_bytes": None,
            "sha256": None,
            "content_encoding": None,
            "content_text": None,
            "skipped_reason": None,
        }
        denied = self.denied_reason(target_path)
        if denied:
            context["skipped_reason"] = denied
            return context
        context["managed"] = True
        dest = self.target_path(target_path)
        if not dest:
            context["skipped_reason"] = "target_path_escapes_workspace"
            return context
        try:
            stat = dest.lstat()
        except FileNotFoundError:
            return context
        except OSError as exc:
            context["skipped_reason"] = f"stat_failed:{exc.__class__.__name__}"
            return context
        context["exists"] = True
        if dest.is_symlink():
            context["type"] = "symlink"
            context["size_bytes"] = len(str(dest.readlink()))
            context["skipped_reason"] = "symlink_target_not_auto_editable"
            return context
        if dest.is_dir():
            context["type"] = "dir"
            context["size_bytes"] = 0
            context["skipped_reason"] = "directory_target_not_auto_editable"
            return context
        if not dest.is_file():
            context["type"] = "other"
            context["size_bytes"] = stat.st_size
            context["skipped_reason"] = "special_file_not_auto_editable"
            return context
        context["type"] = "file"
        context["size_bytes"] = stat.st_size
        try:
            data = dest.read_bytes()
        except OSError as exc:
            context["skipped_reason"] = f"read_failed:{exc.__class__.__name__}"
            return context
        context["sha256"] = hashlib.sha256(data).hexdigest()
        if len(data) > MAX_EXECUTION_TARGET_CONTEXT_BYTES:
            context["skipped_reason"] = "file_too_large_for_inline_context"
            return context
        if b"\x00" in data:
            context["skipped_reason"] = "binary_file_not_auto_editable"
            return context
        try:
            context["content_text"] = data.decode("utf-8")
        except UnicodeDecodeError:
            context["skipped_reason"] = "non_utf8_file_not_auto_editable"
            return context
        context["content_encoding"] = "utf-8"
        return context

    def relative_path(self, target_path: str) -> Optional[Path]:
        if not isinstance(target_path, str):
            return None
        raw = target_path.strip()
        if not raw or "\\" in raw:
            return None
        rel = Path(raw)
        if rel.is_absolute() or rel == Path(".") or ".." in rel.parts:
            return None
        return rel

    def rel_excluded(self, rel: Path) -> bool:
        parts = rel.parts
        if any(part in WORKSPACE_EXCLUDED_NAMES for part in parts):
            return True
        name = rel.name
        return any(fnmatch.fnmatch(name, pattern) for pattern in WORKSPACE_EXCLUDED_PATTERNS)

    def target_path(self, target_path: str) -> Optional[Path]:
        rel = self.relative_path(target_path)
        if rel is None:
            return None
        base = self.workspace_dir.resolve()
        dest = (base / rel).resolve(strict=False)
        if base != dest and base not in dest.parents:
            return None
        return dest
